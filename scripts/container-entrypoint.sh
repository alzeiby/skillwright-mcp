#!/bin/sh
set -eu

role="${1:-api}"
if [ "$#" -gt 0 ]; then
    shift
fi

case "$role" in
    api)
        exec skillwright-api "$@"
        ;;
    mcp)
        exec skillwright-mcp \
            --transport streamable-http \
            --host "${SKILLWRIGHT_MCP_HOST:-0.0.0.0}" \
            --port "${SKILLWRIGHT_MCP_PORT:-8766}" \
            "$@"
        ;;
    worker)
        exec taskiq worker \
            skillwright_mcp.queue:broker \
            --workers 1 \
            --max-async-tasks "${SKILLWRIGHT_WORKER_CONCURRENCY:-2}" \
            --ack-type when_executed \
            "$@"
        ;;
    migrate)
        exec alembic upgrade head "$@"
        ;;
    *)
        exec "$role" "$@"
        ;;
esac
