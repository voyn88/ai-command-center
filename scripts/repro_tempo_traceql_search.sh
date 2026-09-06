#!/usr/bin/env bash
# Reproduces VOYN-W0-AICC-SRV-08-TEMPO-SEARCH: single-binary Tempo 2.6 reports
# a completed local block on disk (meta.json, data.parquet) but TraceQL search
# returns zero results and its own request log shows `total_blocks=0`, while
# `GET /flush` (204) does not help.
#
# This is not an AICC-hosted service today: there is no Tempo deployment in
# this repo. The script is the standalone, runnable evidence backing
# docs/operations/TEMPO_TRACEQL_SEARCH_ROOT_CAUSE.md — download a real Tempo
# 2.6.1 binary, drive it through the healthy case and the failure case, and
# print the exact log lines that pin the cause.
#
# Usage: scripts/repro_tempo_traceql_search.sh [--keep]
#   --keep   leave WORKDIR and the running Tempo process behind for manual
#            follow-up instead of stopping/cleaning at exit.
set -euo pipefail

TEMPO_VERSION="2.6.1"
WORKDIR="${WORKDIR:-$(mktemp -d /tmp/tempo-traceql-repro.XXXXXX)}"
HTTP_PORT="${HTTP_PORT:-3200}"
OTLP_HTTP_PORT="${OTLP_HTTP_PORT:-4318}"
KEEP=0
[[ "${1:-}" == "--keep" ]] && KEEP=1

TEMPO_BIN="${TEMPO_BIN:-}"
TEMPO_PID=""

log()  { printf '\n\033[1;34m== %s ==\033[0m\n' "$*"; }
note() { printf '%s\n' "$*"; }

cleanup() {
  if [[ -n "$TEMPO_PID" ]] && kill -0 "$TEMPO_PID" 2>/dev/null; then
    kill -9 "$TEMPO_PID" 2>/dev/null || true
  fi
  if [[ "$KEEP" -eq 0 ]]; then
    rm -rf "$WORKDIR"
  else
    note "workdir kept at: $WORKDIR"
  fi
}
trap cleanup EXIT

wait_http() {
  # Single-binary startup (ring join, WAL replay) commonly takes 15-20s
  # before /ready stops returning 503.
  local url="$1" tries=60
  until curl -fs -o /dev/null "$url" 2>/dev/null; do
    tries=$((tries - 1))
    [[ "$tries" -le 0 ]] && { echo "timed out waiting for $url" >&2; exit 1; }
    sleep 1
  done
}

send_trace() {
  local trace_id="$1" pr="$2"
  local span_id now
  span_id=$(python3 -c "import secrets;print(secrets.token_hex(8))")
  now=$(date +%s%N)
  python3 - "$trace_id" "$span_id" "$now" "$pr" <<'PY' | curl -s -o /dev/null -w 'send -> HTTP %{http_code}\n' -X POST \
      "http://localhost:${OTLP_HTTP_PORT}/v1/traces" -H 'Content-Type: application/json' --data @-
import json, sys
trace_id, span_id, now, pr = sys.argv[1:5]
print(json.dumps({
    "resourceSpans": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "voyn-repro"}},
            {"key": "voyn.pr_number", "value": {"stringValue": pr}},
        ]},
        "scopeSpans": [{
            "scope": {"name": "repro"},
            "spans": [{
                "traceId": trace_id, "spanId": span_id, "name": "repro-span",
                "kind": 1, "startTimeUnixNano": now, "endTimeUnixNano": now,
            }],
        }],
    }],
}))
PY
}

search() {
  curl -s "http://localhost:${HTTP_PORT}/api/search?q=$1"
}

log "1. Fetching Tempo ${TEMPO_VERSION} (single binary release)"
mkdir -p "$WORKDIR"
if [[ -z "$TEMPO_BIN" ]]; then
  curl -sL --max-time 120 -o "$WORKDIR/tempo.tar.gz" \
    "https://github.com/grafana/tempo/releases/download/v${TEMPO_VERSION}/tempo_${TEMPO_VERSION}_linux_amd64.tar.gz"
  tar xzf "$WORKDIR/tempo.tar.gz" -C "$WORKDIR" tempo
  TEMPO_BIN="$WORKDIR/tempo"
fi
"$TEMPO_BIN" --version

log "2. Starting single-binary Tempo with local backend"
mkdir -p "$WORKDIR/data/traces" "$WORKDIR/data/wal"
cat > "$WORKDIR/tempo.yaml" <<EOF
server:
  http_listen_port: ${HTTP_PORT}
distributor:
  receivers:
    otlp:
      protocols:
        http:
        grpc:
ingester:
  max_block_duration: 5s
  max_block_bytes: 1000000
  complete_block_timeout: 5s
compactor:
  compaction:
    block_retention: 1h
storage:
  trace:
    backend: local
    local:
      path: ${WORKDIR}/data/traces
    wal:
      path: ${WORKDIR}/data/wal
    blocklist_poll: 5s
EOF
"$TEMPO_BIN" -config.file="$WORKDIR/tempo.yaml" > "$WORKDIR/tempo.log" 2>&1 &
TEMPO_PID=$!
wait_http "http://localhost:${HTTP_PORT}/ready"

log "3. Sending a trace for PR 284 and forcing a block flush"
TRACE_ID=$(python3 -c "import secrets;print(secrets.token_hex(16))")
note "trace_id=$TRACE_ID"
send_trace "$TRACE_ID" 284
sleep 12
curl -s -o /dev/null -w 'GET /flush -> HTTP %{http_code}\n' "http://localhost:${HTTP_PORT}/flush"
sleep 8

BLOCK_DIR=$(find "$WORKDIR/data/traces/single-tenant" -mindepth 1 -maxdepth 1 -type d | head -1)
BLOCK_ID=$(basename "$BLOCK_DIR")
note "completed block: $BLOCK_ID"

log "4. Baseline: the block's meta.json has no 'version' key at all"
note "$(cat "$BLOCK_DIR/meta.json")"
note "-- a naive 'jq .version meta.json' returns null on this HEALTHY block too:"
note "   jq .version -> $(python3 -c "import json;print(json.load(open('$BLOCK_DIR/meta.json')).get('version'))")"
note "   (Tempo 2.6's on-disk field is named \"format\", not \"version\" -- see"
note "   tempodb/backend/block_meta.go: 'Version string \`json:\"format\"\`'."
note "   Grepping meta.json for \"version\" is a false alarm by itself; it proves"
note "   nothing about block health.)"

log "5. Baseline TraceQL search WORKS (this is the healthy control)"
RESP=$(search '%7Bresource.voyn.pr_number%3D%22284%22%7D')
note "$RESP" | python3 -m json.tool
MATCHED=$(python3 -c "import json,sys;d=json.loads('''$RESP''');print(len(d.get('traces',[])))")
note "traces matched: $MATCHED"
[[ "$MATCHED" == "1" ]] || { echo "expected the baseline search to find the trace" >&2; exit 1; }

log "6. Injecting the real defect: a second block with an EMPTY format field"
note "This simulates a block that predates the deployed encoding registry (see"
note "grafana/tempo#4612, 'vParquet is not a valid block version') or was left"
note "behind by an interrupted/partial write -- either way, an on-disk block"
note "whose format string the running Tempo does not recognize."
BAD_ID=$(python3 -c "import uuid;print(uuid.uuid4())")
BAD_DIR="$WORKDIR/data/traces/single-tenant/$BAD_ID"
cp -r "$BLOCK_DIR" "$BAD_DIR"
python3 - "$BAD_DIR/meta.json" "$BAD_ID" <<'PY'
import json, sys
path, block_id = sys.argv[1], sys.argv[2]
meta = json.load(open(path))
meta["blockID"] = block_id
meta["format"] = ""
json.dump(meta, open(path, "w"))
PY
note "$(cat "$BAD_DIR/meta.json")"

log "7. Restarting Tempo so the poller picks up the bad block from the backend"
kill -9 "$TEMPO_PID" 2>/dev/null || true
sleep 1
"$TEMPO_BIN" -config.file="$WORKDIR/tempo.yaml" > "$WORKDIR/tempo2.log" 2>&1 &
TEMPO_PID=$!
wait_http "http://localhost:${HTTP_PORT}/ready"
sleep 6

log "8. The block index still counts BOTH blocks..."
curl -s "http://localhost:${HTTP_PORT}/metrics" | grep tempodb_blocklist_length

log "9. ...but the same search that worked in step 5 now reports total_blocks=0"
RESP=$(search '%7Bresource.voyn.pr_number%3D%22284%22%7D')
note "$RESP"
grep -F 'total_blocks=0' "$WORKDIR/tempo2.log" | tail -1

log "10. GET /flush (204) does not fix it -- it only cuts new WAL data"
curl -s -o /dev/null -w 'GET /flush -> HTTP %{http_code}\n' "http://localhost:${HTTP_PORT}/flush"
RESP=$(search '%7Bresource.voyn.pr_number%3D%22284%22%7D')
MATCHED=$(python3 -c "import json,sys;d=json.loads('''$RESP''');print(len(d.get('traces',[])))")
note "traces matched after /flush: $MATCHED"

log "11. Root cause: trace-by-id on the SAME tenant now surfaces the real error"
note "(TraceQL search swallows the per-block error and just reports"
note "total_blocks=0 with HTTP 200; trace-by-id propagates it as a 500.)"
curl -s -o /tmp/traceid_resp.$$ -w 'GET /api/traces/<id> -> HTTP %{http_code}\n' \
  "http://localhost:${HTTP_PORT}/api/traces/${TRACE_ID}"
cat /tmp/traceid_resp.$$; rm -f /tmp/traceid_resp.$$
grep -F 'is not a valid block version' "$WORKDIR/tempo2.log" | tail -1

log "12. Fix: quarantine the bad block, then the tenant is searchable again"
mv "$BAD_DIR" "$WORKDIR/quarantined-$BAD_ID"
sleep 6
RESP=$(search '%7Bresource.voyn.pr_number%3D%22284%22%7D')
MATCHED=$(python3 -c "import json,sys;d=json.loads('''$RESP''');print(len(d.get('traces',[])))")
note "traces matched after quarantine: $MATCHED"
[[ "$MATCHED" == "1" ]] || { echo "expected search to recover after quarantining the bad block" >&2; exit 1; }

log "Reproduction complete: root cause confirmed, fix verified."
