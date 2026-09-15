# skillwright-mcp

Persistent, deterministic browser skills built on Microsoft's Playwright MCP server.

## Container image

Build the production image with:

```sh
docker build -t skillwright-mcp .
```

The image installs `@playwright/mcp@0.0.81` and its matching Chromium during the build. It
removes npm and npx before runtime, runs as UID `10001`, and defaults to authenticated mode.
The application therefore never needs a package download when a browser session starts.
Standard Docker runtimes block Chromium's nested sandbox, so the bundled launcher starts
Chromium with `--no-sandbox` while the container itself remains non-root and isolated. A
deployment that enables Chromium-compatible user namespaces/seccomp can set
`SKILLWRIGHT_PLAYWRIGHT_BROWSER_SANDBOX=1` to retain Chromium's inner sandbox as well.

Run database migrations once before starting long-lived processes:

```sh
docker run --rm \
  -e SKILLWRIGHT_DATABASE_URL=postgresql+asyncpg://skillwright:password@postgres:5432/skillwright \
  skillwright-mcp migrate
```

The same image provides three long-lived roles:

```sh
# Control API on port 8767
docker run --rm -p 8767:8767 \
  -e SKILLWRIGHT_DATABASE_URL=postgresql+asyncpg://skillwright:password@postgres:5432/skillwright \
  -e SKILLWRIGHT_REDIS_URL=redis://redis:6379/0 \
  skillwright-mcp api

# MCP streamable HTTP service on port 8766
docker run --rm -p 8766:8766 \
  -e SKILLWRIGHT_DATABASE_URL=postgresql+asyncpg://skillwright:password@postgres:5432/skillwright \
  -e SKILLWRIGHT_REDIS_URL=redis://redis:6379/0 \
  skillwright-mcp mcp

# Taskiq worker
docker run --rm \
  -e SKILLWRIGHT_DATABASE_URL=postgresql+asyncpg://skillwright:password@postgres:5432/skillwright \
  -e SKILLWRIGHT_REDIS_URL=redis://redis:6379/0 \
  skillwright-mcp worker
```

Production containers do not need the Docker socket, the source tree, or cloud-provider
credentials mounted from the host. Supply the database/Redis URLs and only the specific
`SKILLWRIGHT_SECRET_*` values required by recorded workflows. Browser artifacts remain inside
`/var/lib/skillwright/playwright-output` unless the deployment deliberately attaches managed
storage there.

## Docker Compose

The default Compose project keeps the development database and queue available on loopback
ports without starting the application processes:

```sh
docker compose up -d postgres redis
```

Start the complete deployment profile with one image shared by the migration job, control API,
MCP server, and worker:

```sh
docker compose --profile app up -d --build --wait
```

The migration job must exit successfully before the API, MCP server, and worker start. The API
readiness check verifies PostgreSQL and Redis, the MCP service has a transport health check, and
the OpenTelemetry collector exposes Prometheus metrics at `http://127.0.0.1:9464/metrics`.
Application endpoints are bound to loopback at `http://127.0.0.1:8767` (control API) and
`http://127.0.0.1:8766/mcp` (MCP).
The host ports can be changed with `SKILLWRIGHT_COMPOSE_POSTGRES_PORT`,
`SKILLWRIGHT_COMPOSE_REDIS_PORT`, `SKILLWRIGHT_COMPOSE_API_PORT`,
`SKILLWRIGHT_COMPOSE_MCP_PORT`, and `SKILLWRIGHT_COMPOSE_METRICS_PORT`, which also makes it
possible to run isolated smoke or development stacks side by side.

Compose keeps unauthenticated local access disabled. `SKILLWRIGHT_AUTH_TOKEN_HASHES` is a JSON
mapping from SHA-256 token digests to provisioned principal keys; raw bearer tokens must not be
put in that setting. A new deployment must also provision that principal. For initial bootstrap,
set `SKILLWRIGHT_BOOTSTRAP_ADMIN_PRINCIPAL` to the same principal key for the first authenticated
startup, then remove the bootstrap setting after the administrator exists. The Compose file does
not forward arbitrary host environment variables into containers. If a workflow uses
environment-backed secrets, add only the required
`SKILLWRIGHT_SECRET_*` keys to the `mcp` and `worker` service environments in a deployment
override.

The CI smoke harness exercises the same topology:

```sh
sh scripts/compose-smoke.sh
```

CI derives a one-use bearer token and hash in memory for this smoke; neither is committed to the
repository. The harness verifies API readiness, anonymous rejection, an authenticated MCP
`skill_list` call, application metrics reaching the collector, every long-lived service running,
and successful migration to an Alembic schema version.
