#!/bin/sh
set -eu

api_url="${SKILLWRIGHT_SMOKE_API_URL:-http://127.0.0.1:8767}"
mcp_url="${SKILLWRIGHT_SMOKE_MCP_URL:-http://127.0.0.1:8766/mcp}"
metrics_url="${SKILLWRIGHT_SMOKE_METRICS_URL:-http://127.0.0.1:9464/metrics}"
attempts="${SKILLWRIGHT_SMOKE_ATTEMPTS:-45}"
smoke_token="${SKILLWRIGHT_SMOKE_BEARER_TOKEN:?set SKILLWRIGHT_SMOKE_BEARER_TOKEN for the authenticated MCP smoke}"

retry() {
    attempt=1
    while ! "$@"; do
        if [ "$attempt" -ge "$attempts" ]; then
            return 1
        fi
        attempt=$((attempt + 1))
        sleep 2
    done
}

fetch_contains() {
    url="$1"
    expected="$2"
    body="$(curl --fail --silent --show-error "$url")" || return 1
    printf '%s' "$body" | grep -Fq "$expected"
}

assert_running() {
    service="$1"
    container_id="$(docker compose --profile app ps -q "$service")"
    [ -n "$container_id" ] || return 1
    [ "$(docker inspect --format '{{.State.Status}}' "$container_id")" = "running" ]
}

retry fetch_contains "$api_url/health/ready" '"status":"ready"'

auth_status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
    "$api_url/api/v1/runs/compose-smoke-missing")"
[ "$auth_status" = "401" ]

request_file="$(mktemp)"
response_file="$(mktemp)"
headers_file="$(mktemp)"
trap 'rm -f "$request_file" "$response_file" "$headers_file"' EXIT HUP INT TERM
cat >"$request_file" <<'JSON'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"compose-smoke","version":"1"}}}
JSON

# With a token verifier configured, even MCP initialization must reject an anonymous client.
unauthenticated_mcp_status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
    --header 'Content-Type: application/json' \
    --header 'Accept: application/json, text/event-stream' \
    --data-binary "@$request_file" \
    "$mcp_url")"
[ "$unauthenticated_mcp_status" = "401" ]

retry curl --fail --silent --show-error \
    --header "Authorization: Bearer $smoke_token" \
    --header 'Content-Type: application/json' \
    --header 'Accept: application/json, text/event-stream' \
    --data-binary "@$request_file" \
    --dump-header "$headers_file" \
    --output "$response_file" \
    "$mcp_url"
grep -Fq '"result"' "$response_file"
grep -Fq '"protocolVersion"' "$response_file"

session_id="$(awk 'tolower($1) == "mcp-session-id:" {gsub("\r", "", $2); print $2; exit}' \
    "$headers_file")"
[ -n "$session_id" ]

cat >"$request_file" <<'JSON'
{"jsonrpc":"2.0","method":"notifications/initialized"}
JSON
curl --fail --silent --show-error \
    --header "Authorization: Bearer $smoke_token" \
    --header 'Content-Type: application/json' \
    --header 'Accept: application/json, text/event-stream' \
    --header "Mcp-Session-Id: $session_id" \
    --header 'MCP-Protocol-Version: 2025-06-18' \
    --data-binary "@$request_file" \
    --output /dev/null \
    "$mcp_url"

cat >"$request_file" <<'JSON'
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"skill_list","arguments":{}}}
JSON
curl --fail --silent --show-error \
    --header "Authorization: Bearer $smoke_token" \
    --header 'Content-Type: application/json' \
    --header 'Accept: application/json, text/event-stream' \
    --header "Mcp-Session-Id: $session_id" \
    --header 'MCP-Protocol-Version: 2025-06-18' \
    --data-binary "@$request_file" \
    --output "$response_file" \
    "$mcp_url"
grep -Fq '"result"' "$response_file"
grep -Fq 'skills' "$response_file"
if grep -Fq '"error"' "$response_file"; then
    exit 1
fi

# The SDK exports metrics asynchronously. The Compose profile shortens that interval to five
# seconds, and this proves the API -> OTLP collector -> Prometheus path is actually working.
retry fetch_contains "$metrics_url" 'http_server_duration'

for service in postgres redis api mcp worker otel-collector; do
    assert_running "$service"
done

migrate_id="$(docker compose --profile app ps -a -q migrate)"
[ -n "$migrate_id" ]
[ "$(docker inspect --format '{{.State.ExitCode}}' "$migrate_id")" = "0" ]

schema_version="$(docker compose --profile app exec -T postgres \
    psql -U skillwright -d skillwright -Atc 'select version_num from alembic_version')"
[ -n "$schema_version" ]

printf 'Compose smoke passed with authenticated MCP (schema %s).\n' "$schema_version"
