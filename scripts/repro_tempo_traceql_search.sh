#!/usr/bin/env bash
# Reproduces VOYN-W0-AICC-SRV-08-TEMPO-SEARCH: single-binary Tempo 2.6 reports
# a completed local block on disk (meta.json, data.parquet) but TraceQL search
# returns zero results and its own request log shows `total_blocks=0`, while
# `GET /api/traces/<id>` (trace-by-id) for the exact same trace succeeds, and
# `GET /flush` (204) does not help.
#
# This is not an AICC-hosted service today: there is no Tempo deployment in
# this repo. The script is the standalone, runnable evidence backing
# docs/operations/TEMPO_TRACEQL_SEARCH_ROOT_CAUSE.md -- download a real Tempo
# 2.6.1 binary, drive it through the failure case with an intentionally
# mismatched config, confirm the exact defect, then apply the one-line config
# fix and confirm recovery on the SAME on-disk block.
#
# Usage: scripts/repro_tempo_traceql_search.sh [--keep]
#   --keep   leave WORKDIR behind for manual follow-up instead of deleting it
#            at exit. The Tempo process is always stopped at exit either way.
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

start_tempo() {
  local config="$1" logfile="$2"
  "$TEMPO_BIN" -config.file="$config" > "$logfile" 2>&1 &
  TEMPO_PID=$!
  wait_http "http://localhost:${HTTP_PORT}/ready"
}

stop_tempo() {
  if [[ -n "$TEMPO_PID" ]] && kill -0 "$TEMPO_PID" 2>/dev/null; then
    kill -9 "$TEMPO_PID" 2>/dev/null || true
    wait "$TEMPO_PID" 2>/dev/null || true
  fi
  TEMPO_PID=""
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

# TraceQL search always takes an explicit start/end window in this repro.
# Tempo's own docs (see step 8) show why a bare `q=` without a range is a
# separate footgun -- observed here as range_seconds=0 matching nothing -- so
# omitting the range would confound the config defect this script targets.
search() {
  local q="$1" start="$2" end="$3"
  curl -s "http://localhost:${HTTP_PORT}/api/search?q=${q}&start=${start}&end=${end}"
}

matched_count() {
  python3 -c "import json,sys;d=json.load(sys.stdin);print(len(d.get('traces',[])))"
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

log "2. Starting single-binary Tempo with the DEFECTIVE config"
note "ingester.complete_block_timeout is set to 5s (a short value, typical of"
note "a deployment tuned to keep local disk usage/WAL replay time down)."
note "query_frontend.search.query_backend_after is left UNSET, i.e. Tempo's"
note "documented default of 15m. That pairing is the defect: it is never"
note "valid to have complete_block_timeout < query_backend_after (see step 8)."
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
start_tempo "$WORKDIR/tempo.yaml" "$WORKDIR/tempo.log"

log "3. Sending a trace for PR 284 and forcing a block flush"
TRACE_ID=$(python3 -c "import secrets;print(secrets.token_hex(16))")
note "trace_id=$TRACE_ID"
send_trace "$TRACE_ID" 284
sleep 12
curl -s -o /dev/null -w 'GET /flush -> HTTP %{http_code}\n' "http://localhost:${HTTP_PORT}/flush"
sleep 8

BLOCK_DIR=$(find "$WORKDIR/data/traces/single-tenant" -mindepth 1 -maxdepth 1 -type d | head -1)
BLOCK_ID=$(basename "$BLOCK_DIR")
note "completed block on disk: $BLOCK_ID"
ls "$BLOCK_DIR"

log "4. meta.json 'version' is a false lead, not the defect"
note "$(cat "$BLOCK_DIR/meta.json")"
note "-- a naive 'jq .version meta.json' returns null on this block, exactly"
note "   as reported. But Tempo 2.6's on-disk field is named \"format\", not"
note "   \"version\" (tempodb/backend/block_meta.go: 'Version string"
note "   \`json:\"format\"\`'). This block's format is a healthy vParquet4."
note "   Grepping meta.json for \"version\" proves nothing about block health;"
note "   every block, healthy or not, reports null there."
python3 -c "import json;print('format field ->', json.load(open('$BLOCK_DIR/meta.json'))['format'])"

log "5. Trace-by-id for this trace WORKS right now"
TID_CODE=$(curl -s -o /tmp/repro_traceid.$$ -w '%{http_code}' "http://localhost:${HTTP_PORT}/api/traces/${TRACE_ID}")
note "GET /api/traces/<id> -> HTTP ${TID_CODE}"
BYTES=$(wc -c < /tmp/repro_traceid.$$)
rm -f /tmp/repro_traceid.$$
note "response bytes: $BYTES"
[[ "$TID_CODE" == "200" && "$BYTES" -gt 0 ]] || { echo "expected trace-by-id to succeed" >&2; exit 1; }

log "6. TraceQL search over an explicit range covering the trace returns ZERO blocks"
NOW=$(date +%s)
START=$((NOW - 3600))
END=$((NOW + 60))
RESP=$(search '%7Bresource.voyn.pr_number%3D%22284%22%7D' "$START" "$END")
note "$RESP"
grep -F 'total_blocks=0' "$WORKDIR/tempo.log" | tail -1
MATCHED=$(printf '%s' "$RESP" | matched_count)
note "traces matched: $MATCHED (expected 0 -- this is the bug)"
[[ "$MATCHED" == "0" ]] || { echo "expected the defective config to fail this search" >&2; exit 1; }

log "7. GET /flush (204) does not help -- it only cuts new WAL data"
curl -s -o /dev/null -w 'GET /flush -> HTTP %{http_code}\n' "http://localhost:${HTTP_PORT}/flush"
RESP=$(search '%7Bresource.voyn.pr_number%3D%22284%22%7D' "$START" "$END")
MATCHED=$(printf '%s' "$RESP" | matched_count)
note "traces matched after /flush: $MATCHED (still 0)"

log "8. Root cause: query_backend_after (15m default) outlives complete_block_timeout (5s)"
curl -s "http://localhost:${HTTP_PORT}/status/config" > "$WORKDIR/effective-config.yaml"
note "effective ingester.complete_block_timeout:"
grep -A1 'complete_block_timeout' "$WORKDIR/effective-config.yaml" | head -2
note "effective query_frontend.search.query_backend_after:"
grep -A1 '^\s*query_backend_after' "$WORKDIR/effective-config.yaml" | head -2
note ""
note "Tempo's own docs (configuration/_index.md) define the contract:"
note "  'Time ranges before query_ingesters_until will be searched in the"
note "   ingesters only. Time ranges after query_backend_after will be"
note "   searched in the backend/object storage only.'"
note "The ingester purges its local copy of a flushed block after"
note "complete_block_timeout (here: 5s). The query-frontend search sharder"
note "does not even generate a backend job for a time range younger than"
note "query_backend_after (here: the 15m default). Any block older than 5s"
note "and younger than 15m is a dead zone: gone from the ingester, not yet"
note "eligible on the backend path. TraceQL search silently reports"
note "total_blocks=0 with HTTP 200 for that dead zone; it is not an error,"
note "so nothing surfaces in alerting. Trace-by-id is unaffected because it"
note "walks the full backend blocklist regardless of block age -- which is"
note "exactly why it works while search does not."

log "9. Fix: query_backend_after must be <= complete_block_timeout"
note "Restarting Tempo against the SAME on-disk block with"
note "query_frontend.search.query_backend_after lowered to 5s (matching"
note "complete_block_timeout) instead of touching any data."
stop_tempo
cp "$WORKDIR/tempo.yaml" "$WORKDIR/tempo.fixed.yaml"
cat >> "$WORKDIR/tempo.fixed.yaml" <<'EOF'
query_frontend:
  search:
    query_backend_after: 5s
EOF
start_tempo "$WORKDIR/tempo.fixed.yaml" "$WORKDIR/tempo.fixed.log"
sleep 6

RESP=$(search '%7Bresource.voyn.pr_number%3D%22284%22%7D' "$START" "$END")
note "$RESP"
MATCHED=$(printf '%s' "$RESP" | matched_count)
note "traces matched after the fix: $MATCHED"
[[ "$MATCHED" == "1" ]] || { echo "expected the fixed config to find the trace" >&2; exit 1; }

log "Reproduction complete: root cause confirmed (config defect), fix verified."
