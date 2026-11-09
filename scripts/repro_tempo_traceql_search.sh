#!/usr/bin/env bash
# Reproduce VOYN-W0-AICC-SRV-08-TEMPO-SEARCH with Tempo 2.6.1.
#
# The experiment writes a trace carrying resource.voyn.pr_number="284", proves
# that the trace and its completed block exist, and compares TraceQL search in
# three states: the original config, an unchanged restart, and a corrected
# restart against the same data. It is acceptance evidence for an external
# Tempo configuration; this repository does not deploy Tempo.
#
# Usage: scripts/repro_tempo_traceql_search.sh [--keep]
#   --keep  preserve the generated work directory for inspection
set -euo pipefail

readonly TEMPO_VERSION="2.6.1"
readonly TEMPO_ARCHIVE_SHA256="5f9e6e26e983030f81860893f8387f5ec19c0a193c66e1a2f0da4b3723e800f6"
readonly HTTP_PORT="${HTTP_PORT:-3200}"
readonly OTLP_HTTP_PORT="${OTLP_HTTP_PORT:-4318}"
readonly WORKDIR_PARENT="${WORKDIR:-${TMPDIR:-/tmp}}"
readonly TRACEQL_QUERY='%7Bresource.voyn.pr_number%3D%22284%22%7D'

KEEP=0
case "${1:-}" in
  "") ;;
  --keep) KEEP=1 ;;
  *) printf 'usage: %s [--keep]\n' "$0" >&2; exit 2 ;;
esac

mkdir -p "$WORKDIR_PARENT"
# Cleanup is allowed to remove only this directory, which this process created.
WORKDIR=$(mktemp -d "${WORKDIR_PARENT%/}/tempo-traceql-repro.XXXXXX")
readonly WORKDIR
TEMPO_BIN="${TEMPO_BIN:-}"
TEMPO_PID=""

log()  { printf '\n\033[1;34m== %s ==\033[0m\n' "$*"; }
note() { printf '%s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

cleanup() {
  stop_tempo
  if [[ "$KEEP" -eq 1 ]]; then
    note "workdir kept at: $WORKDIR"
    return
  fi
  case "$WORKDIR" in
    "${WORKDIR_PARENT%/}"/tempo-traceql-repro.*) rm -rf -- "$WORKDIR" ;;
    *) printf 'refusing to remove unexpected workdir: %s\n' "$WORKDIR" >&2 ;;
  esac
}
trap cleanup EXIT

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

check_port_free() {
  python3 - "$1" <<'PY'
import socket, sys
port = int(sys.argv[1])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as exc:
        raise SystemExit(f"port {port} is unavailable: {exc}")
PY
}

dump_tempo_log() {
  local logfile="$1"
  printf '%s\n' "--- Tempo log: $logfile ---" >&2
  if [[ -f "$logfile" ]]; then
    tail -n 200 "$logfile" >&2
  else
    printf '%s\n' '(log file was not created)' >&2
  fi
}

wait_http() {
  local url="$1" logfile="$2" deadline=$((SECONDS + 60))
  while (( SECONDS < deadline )); do
    if ! kill -0 "$TEMPO_PID" 2>/dev/null; then
      wait "$TEMPO_PID" 2>/dev/null || true
      dump_tempo_log "$logfile"
      fail "Tempo exited before becoming ready"
    fi
    if curl -fsS -o /dev/null "$url" 2>/dev/null; then
      return
    fi
    sleep 1
  done
  dump_tempo_log "$logfile"
  fail "timed out waiting for $url"
}

start_tempo() {
  local config="$1" logfile="$2"
  check_port_free "$HTTP_PORT"
  check_port_free "$OTLP_HTTP_PORT"
  "$TEMPO_BIN" -config.file="$config" >"$logfile" 2>&1 &
  TEMPO_PID=$!
  wait_http "http://127.0.0.1:${HTTP_PORT}/ready" "$logfile"
}

stop_tempo() {
  if [[ -n "${TEMPO_PID:-}" ]] && kill -0 "$TEMPO_PID" 2>/dev/null; then
    kill "$TEMPO_PID" 2>/dev/null || true
    for _ in {1..50}; do
      kill -0 "$TEMPO_PID" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "$TEMPO_PID" 2>/dev/null; then
      kill -9 "$TEMPO_PID" 2>/dev/null || true
    fi
    wait "$TEMPO_PID" 2>/dev/null || true
  fi
  TEMPO_PID=""
}

send_trace() {
  local trace_id="$1" pr_number="$2" start_ns="$3" span_id response_code
  span_id=$(python3 -c 'import secrets; print(secrets.token_hex(8))')
  response_code=$(
    python3 - "$trace_id" "$span_id" "$start_ns" "$pr_number" <<'PY' |
import json, sys
trace_id, span_id, start_ns, pr_number = sys.argv[1:5]
print(json.dumps({"resourceSpans": [{
    "resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": "voyn-repro"}},
        {"key": "voyn.pr_number", "value": {"stringValue": pr_number}},
    ]},
    "scopeSpans": [{"scope": {"name": "repro"}, "spans": [{
        "traceId": trace_id,
        "spanId": span_id,
        "name": "repro-span",
        "kind": 1,
        "startTimeUnixNano": start_ns,
        "endTimeUnixNano": str(int(start_ns) + 1_000_000),
    }]}],
}]}))
PY
      curl -fsS -o /dev/null -w '%{http_code}' -X POST \
        "http://127.0.0.1:${OTLP_HTTP_PORT}/v1/traces" \
        -H 'Content-Type: application/json' --data-binary @-
  )
  note "send -> HTTP $response_code"
  [[ "$response_code" == "200" ]] || fail "OTLP ingestion returned HTTP $response_code"
}

search() {
  local start="$1" end="$2"
  curl -fsS "http://127.0.0.1:${HTTP_PORT}/api/search?q=${TRACEQL_QUERY}&start=${start}&end=${end}"
}

search_trace_ids() {
  python3 -c 'import json,sys; print("\n".join(t["traceID"] for t in json.load(sys.stdin).get("traces", [])))'
}

search_count() {
  python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("traces", [])))'
}

wait_for_search_trace() {
  local expected_id="$1" start="$2" end="$3" logfile="$4"
  local deadline=$((SECONDS + 45)) response ids
  while (( SECONDS < deadline )); do
    if ! kill -0 "$TEMPO_PID" 2>/dev/null; then
      dump_tempo_log "$logfile"
      fail "Tempo exited while waiting for TraceQL result"
    fi
    response=$(search "$start" "$end")
    ids=$(printf '%s' "$response" | search_trace_ids)
    if printf '%s\n' "$ids" | grep -Fxq -- "$expected_id"; then
      printf '%s' "$response"
      return
    fi
    sleep 1
  done
  dump_tempo_log "$logfile"
  fail "TraceQL did not return trace $expected_id before timeout"
}

find_block_for_time() {
  local tenant_dir="$1" ingest_seconds="$2" deadline=$((SECONDS + 45)) result
  while (( SECONDS < deadline )); do
    result=$(python3 - "$tenant_dir" "$ingest_seconds" <<'PY'
import datetime as dt
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
instant = dt.datetime.fromtimestamp(int(sys.argv[2]), tz=dt.timezone.utc)
for meta_path in sorted(root.glob("*/meta.json")):
    try:
        meta = json.loads(meta_path.read_text())
        start = dt.datetime.fromisoformat(meta["startTime"].replace("Z", "+00:00"))
        end = dt.datetime.fromisoformat(meta["endTime"].replace("Z", "+00:00"))
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        continue
    if start <= instant <= end:
        print(meta_path.parent)
        break
PY
    )
    if [[ -n "$result" ]]; then
      printf '%s' "$result"
      return
    fi
    sleep 1
  done
  fail "no completed block time range covers ingest time $ingest_seconds"
}

assert_zero_search() {
  local start="$1" end="$2" logfile="$3" label="$4" response matched blocks_line
  response=$(search "$start" "$end")
  matched=$(printf '%s' "$response" | search_count)
  blocks_line=$(grep -F 'total_blocks=0' "$logfile" | tail -n 1 || true)
  note "$response"
  note "traces matched $label: $matched"
  [[ "$matched" -eq 0 ]] || fail "expected zero TraceQL matches $label"
  [[ -n "$blocks_line" ]] || fail "Tempo did not log total_blocks=0 $label"
  note "$blocks_line"
}

wait_for_zero_search() {
  local start="$1" end="$2" logfile="$3" label="$4"
  local deadline=$((SECONDS + 45)) response matched blocks_line
  while (( SECONDS < deadline )); do
    if ! kill -0 "$TEMPO_PID" 2>/dev/null; then
      dump_tempo_log "$logfile"
      fail "Tempo exited while waiting for the zero-result condition"
    fi
    response=$(search "$start" "$end")
    matched=$(printf '%s' "$response" | search_count)
    blocks_line=$(grep -F 'total_blocks=0' "$logfile" | tail -n 1 || true)
    if [[ "$matched" -eq 0 && -n "$blocks_line" ]]; then
      note "$response"
      note "traces matched $label: $matched"
      note "$blocks_line"
      return
    fi
    sleep 1
  done
  dump_tempo_log "$logfile"
  fail "TraceQL did not reach zero matches with total_blocks=0 $label"
}

write_config() {
  local path="$1" backend_after="${2:-}"
  {
    printf '%s\n' \
      'server:' \
      "  http_listen_port: ${HTTP_PORT}" \
      'distributor:' \
      '  receivers:' \
      '    otlp:' \
      '      protocols:' \
      '        http:' \
      "          endpoint: 127.0.0.1:${OTLP_HTTP_PORT}" \
      'ingester:' \
      '  max_block_duration: 5s' \
      '  max_block_bytes: 1000000' \
      '  complete_block_timeout: 5s' \
      'compactor:' \
      '  compaction:' \
      '    block_retention: 1h' \
      'storage:' \
      '  trace:' \
      '    backend: local' \
      '    local:' \
      "      path: ${WORKDIR}/data/traces" \
      '    wal:' \
      "      path: ${WORKDIR}/data/wal" \
      '    blocklist_poll: 5s'
    if [[ -n "$backend_after" ]]; then
      printf '%s\n' \
        'query_frontend:' \
        '  search:' \
        "    query_backend_after: ${backend_after}"
    fi
  } >"$path"
}

for command in curl python3 sha256sum tar; do
  require_command "$command"
done
[[ "$HTTP_PORT" != "$OTLP_HTTP_PORT" ]] || fail "HTTP_PORT and OTLP_HTTP_PORT must differ"

log "1. Fetching pinned Tempo ${TEMPO_VERSION} binary"
if [[ -z "$TEMPO_BIN" ]]; then
  archive="$WORKDIR/tempo.tar.gz"
  curl -fL --retry 3 --max-time 120 -o "$archive" \
    "https://github.com/grafana/tempo/releases/download/v${TEMPO_VERSION}/tempo_${TEMPO_VERSION}_linux_amd64.tar.gz"
  printf '%s  %s\n' "$TEMPO_ARCHIVE_SHA256" "$archive" | sha256sum --check --status \
    || fail "Tempo archive checksum verification failed"
  tar -xzf "$archive" -C "$WORKDIR" tempo
  TEMPO_BIN="$WORKDIR/tempo"
fi
[[ -x "$TEMPO_BIN" ]] || fail "Tempo binary is not executable: $TEMPO_BIN"
"$TEMPO_BIN" --version

mkdir -p "$WORKDIR/data/traces" "$WORKDIR/data/wal"
write_config "$WORKDIR/tempo.yaml"

log "2. Starting Tempo with a deliberately mismatched search retention config"
note "complete_block_timeout=5s; query_backend_after is the 15m default."
note "The short timeout is intentionally artificial so the acceptance run is quick."
start_tempo "$WORKDIR/tempo.yaml" "$WORKDIR/tempo.log"

log "3. Writing the known PR attribute and locating its completed block"
TRACE_ID=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
readonly TRACE_ID
TRACE_START_NS=$(date +%s%N)
readonly TRACE_START_NS
TRACE_START_SECONDS=$((TRACE_START_NS / 1000000000))
readonly TRACE_START_SECONDS
note "trace_id=$TRACE_ID"
send_trace "$TRACE_ID" 284 "$TRACE_START_NS"
curl -fsS -o /dev/null -w 'GET /flush -> HTTP %{http_code}\n' \
  "http://127.0.0.1:${HTTP_PORT}/flush"
BLOCK_DIR=$(find_block_for_time "$WORKDIR/data/traces/single-tenant" "$TRACE_START_SECONDS")
readonly BLOCK_DIR
note "completed block on disk: $(basename "$BLOCK_DIR")"
ls -la "$BLOCK_DIR"

log "4. Confirming the block format (meta.json has no version key)"
python3 - "$BLOCK_DIR/meta.json" <<'PY'
import json, sys
meta = json.load(open(sys.argv[1], encoding="utf-8"))
if "version" in meta:
    raise SystemExit("unexpected meta.json version key")
print(json.dumps(meta, indent=2, sort_keys=True))
print("format field ->", meta["format"])
PY

log "5. Proving trace-by-id still extracts the trace"
TRACE_RESPONSE="$WORKDIR/trace-by-id.json"
TID_CODE=$(curl -fsS -H 'Accept: application/json' -o "$TRACE_RESPONSE" -w '%{http_code}' \
  "http://127.0.0.1:${HTTP_PORT}/api/traces/${TRACE_ID}")
note "GET /api/traces/<id> -> HTTP $TID_CODE"
[[ "$TID_CODE" == "200" ]] || fail "trace-by-id returned HTTP $TID_CODE"
python3 - "$TRACE_RESPONSE" <<'PY'
import json, sys
document = json.load(open(sys.argv[1], encoding="utf-8"))
resource_spans = document.get("batches", document.get("resourceSpans", []))
attributes = [
    attribute
    for batch in resource_spans
    for attribute in batch.get("resource", {}).get("attributes", [])
]
if not any(
    item.get("key") == "voyn.pr_number"
    and item.get("value", {}).get("stringValue") == "284"
    for item in attributes
):
    raise SystemExit("trace-by-id JSON does not contain voyn.pr_number=284")
print("trace-by-id contains voyn.pr_number=284")
PY

SEARCH_START=$((TRACE_START_SECONDS - 60))
SEARCH_END=$((TRACE_START_SECONDS + 3600))
readonly SEARCH_START SEARCH_END

log "6. Demonstrating the young-block TraceQL dead zone"
wait_for_zero_search "$SEARCH_START" "$SEARCH_END" "$WORKDIR/tempo.log" "before restart"

log "7. Proving /flush does not repair search"
curl -fsS -o /dev/null -w 'GET /flush -> HTTP %{http_code}\n' \
  "http://127.0.0.1:${HTTP_PORT}/flush"
assert_zero_search "$SEARCH_START" "$SEARCH_END" "$WORKDIR/tempo.log" "after /flush"

log "8. Capturing the effective version and configuration"
curl -fsS "http://127.0.0.1:${HTTP_PORT}/status/config" >"$WORKDIR/effective-config.yaml"
"$TEMPO_BIN" --version | tee "$WORKDIR/tempo-version.txt"
python3 - "$WORKDIR/effective-config.yaml" <<'PY'
import pathlib, sys
text = pathlib.Path(sys.argv[1]).read_text()
for key in ("complete_block_timeout", "query_backend_after", "query_ingesters_until", "blocklist_poll"):
    lines = [line.strip() for line in text.splitlines() if key in line]
    if not lines:
        raise SystemExit(f"effective config is missing {key}")
    print(*lines, sep="\n")
PY
note "With these deliberately compressed timings, the completed block can leave"
note "ingester search before backend search becomes eligible. blocklist_poll adds"
note "discovery lag. query_ingesters_until is a separate bound on ingester search."
note "For production, retain completed blocks beyond query_backend_after plus"
note "blocklist polling margin; lowering query_backend_after to 5s is only the"
note "fast, controlled correction used by this reproduction."

log "9. Restarting the unchanged config against the same block"
stop_tempo
start_tempo "$WORKDIR/tempo.yaml" "$WORKDIR/tempo.restart.log"
wait_for_zero_search "$SEARCH_START" "$SEARCH_END" "$WORKDIR/tempo.restart.log" "after unchanged restart"

log "10. Correcting the test config and polling for the known trace ID"
stop_tempo
write_config "$WORKDIR/tempo.fixed.yaml" '5s'
start_tempo "$WORKDIR/tempo.fixed.yaml" "$WORKDIR/tempo.fixed.log"
RESP=$(wait_for_search_trace "$TRACE_ID" "$SEARCH_START" "$SEARCH_END" "$WORKDIR/tempo.fixed.log")
note "$RESP"
MATCHED=$(printf '%s' "$RESP" | search_count)
IDS=$(printf '%s' "$RESP" | search_trace_ids)
note "traces matched after correction: $MATCHED"
[[ "$MATCHED" -ge 1 ]] || fail "corrected search returned no traces"
printf '%s\n' "$IDS" | grep -Fxq -- "$TRACE_ID" \
  || fail "corrected search did not return the ingested trace ID"

log "Acceptance complete: failure reproduced, cause measured, known trace recovered."
