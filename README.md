# skillwright-mcp

Persistent, deterministic browser skills built on Microsoft's Playwright MCP server.

Skillwright lets an agent perform a browser task once through proxied Playwright MCP tools,
records the successful interaction, compiles it into a durable workflow, and replays that workflow
later without an LLM. Runs are pinned to immutable workflow versions, persisted in the database,
and stop with structured context when a target needs repair or a gated action needs approval.

## Architecture

```mermaid
flowchart LR
    Agent["MCP client / agent"] --> MCP["Skillwright MCP"]
    Operator["Operator / service"] --> API["Control API"]
    MCP --> DB[(PostgreSQL)]
    API --> DB
    MCP --> Redis[(Redis / Taskiq)]
    API --> Redis
    Redis --> Worker["Skillwright worker"]
    MCP --> Browser["Microsoft Playwright MCP"]
    Worker --> BrowserWorker["Isolated Playwright MCP"]
    Browser --> Site["Browser / website"]
    BrowserWorker --> Site
    MCP --> OTEL["OTLP traces + metrics"]
    API --> OTEL
    Worker --> OTEL
```

The MCP service owns the authoring surface: proxied browser actions, recording, parameterization,
secret binding, approvals, repair, versioning, and access control. PostgreSQL is the source of
truth for principals, recordings, browser history, skills and versions, runs, step executions,
repairs, approvals, secret-binding metadata, and audit events. In the production backend, Redis
and Taskiq distribute persisted run IDs to workers; a worker executes the pinned workflow in an
isolated Playwright MCP/browser session. The FastAPI control service exposes a deliberately small
HTTP surface for starting and operating existing runs.

The implementation maintains several invariants:

- Replay is deterministic application code. The LLM is involved when demonstrating a task and,
  when needed, selecting a repair candidate; routine execution does not call an LLM.
- Recording compiles successful proxied actions. `browser_snapshot` supplies semantic evidence but
  is not itself a workflow step. The recorder currently emits navigate, click, fill, select, and
  wait steps.
- Element steps persist semantic target evidence and, when available, a generated Playwright
  locator. A transient snapshot ref such as `e12` is rejected at compile time if there is no
  matching snapshot evidence from which to build a durable target.
- Every run pins a specific workflow version when it is created. Parameterization, approval-gate
  changes, successful persisted repairs, and rollback all create a new version instead of editing
  an existing version in place.
- A target that cannot be resolved stops exactly at that step with `repair_required`, the expected
  target, and ranked candidates. A mutating action that fails after dispatch reports its side-effect
  state as unknown instead of assuming the action was safe to retry.
- Approval gates stop before the gated click, fill, or select is dispatched. The run resumes only
  after an authorized approval decision.
- Secret workflow inputs are resolved on the server from explicit bindings. Callers cannot supply
  a secret input in `skill_run.inputs` or the control API request body.
- Authorization is checked when work is submitted and again before a queued run begins browser
  work. Revoking access or disabling the requester while a run waits in the queue prevents the
  browser execution from starting.

## Local development

Skillwright requires Python 3.11-3.14. Source development uses `uv`; local browser execution also
requires Node.js/npm so the default `npx @playwright/mcp@0.0.81` launcher is available. The
production image described below installs the exact Playwright MCP package and matching Chromium
for a hermetic runtime.

Install the project and development dependencies:

```sh
uv sync --extra dev
```

For the same PostgreSQL/Redis dependencies used by the production-shaped services, start the local
dependency containers and apply migrations:

```sh
docker compose up -d postgres redis
uv run alembic upgrade head
```

For a lightweight single-process development loop, SQLite plus inline execution avoids the worker
and Redis. Put this in a local `.env` before launching the MCP server:

```dotenv
SKILLWRIGHT_DATABASE_URL=sqlite+aiosqlite:///./skillwright-dev.db
SKILLWRIGHT_DATABASE_AUTO_CREATE_SCHEMA=true
SKILLWRIGHT_EXECUTION_BACKEND=inline
SKILLWRIGHT_ALLOW_UNAUTHENTICATED_LOCAL=true
SKILLWRIGHT_LOCAL_PRINCIPAL=local
SKILLWRIGHT_LOCAL_ROLE=admin
```

`SKILLWRIGHT_DATABASE_AUTO_CREATE_SCHEMA=true` is intended for local development. Production uses
Alembic migrations and leaves automatic schema creation disabled.

Run the unit/integration suite with:

```sh
uv run pytest
```

## MCP configuration

### stdio

`skillwright-mcp` uses stdio by default:

```sh
uv run skillwright-mcp
```

A typical local MCP client entry is:

```json
{
  "mcpServers": {
    "skillwright": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/skillwright-mcp",
        "skillwright-mcp"
      ]
    }
  }
}
```

The exact outer configuration key varies by MCP client, but the command and arguments above are
the server's real CLI. With the local settings above, stdio resolves requests to the configured
local principal. For shared or remote deployments, disable unauthenticated local access and use
the bearer-token configuration below.

### Streamable HTTP

Start the MCP server on Streamable HTTP with:

```sh
uv run skillwright-mcp --transport streamable-http --host 127.0.0.1 --port 8766
```

The MCP endpoint is `http://127.0.0.1:8766/mcp`. A client configuration is conceptually:

```json
{
  "mcpServers": {
    "skillwright": {
      "url": "http://127.0.0.1:8766/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_OPAQUE_TOKEN"
      }
    }
  }
}
```

Client syntax for HTTP headers and environment-variable interpolation differs, so inject the token
using the client's own secret/configuration mechanism rather than committing it to a config file.
Stateful Streamable HTTP sessions receive separate interactive browser sessions; stdio and
stateless/fallback traffic is isolated by authenticated principal. Idle interactive browser
sessions are closed after 30 minutes.

## Authentication and principal provisioning

Skillwright uses opaque bearer service tokens. `SKILLWRIGHT_AUTH_TOKEN_HASHES` is a JSON object
whose keys are SHA-256 token digests and whose values are Skillwright principal external keys. The
raw bearer token is never stored in this setting.

The following command generates a high-entropy token and the corresponding JSON mapping for a
principal named `ops-admin`:

```sh
python -c "import hashlib,json,secrets; p='ops-admin'; t=secrets.token_urlsafe(48); print('TOKEN='+t); print('SKILLWRIGHT_AUTH_TOKEN_HASHES='+json.dumps({hashlib.sha256(t.encode()).hexdigest():p}))"
```

The token mapping authenticates a key; it does not create that principal in the database. For the
first authenticated startup of a new deployment, configure all three values together:

```dotenv
SKILLWRIGHT_ALLOW_UNAUTHENTICATED_LOCAL=false
SKILLWRIGHT_BOOTSTRAP_ADMIN_PRINCIPAL=ops-admin
SKILLWRIGHT_AUTH_TOKEN_HASHES={"<sha256-digest>":"ops-admin"}
```

Start the service once, verify the administrator exists, then remove
`SKILLWRIGHT_BOOTSTRAP_ADMIN_PRINCIPAL`. Bootstrap creation is one-time: if that key already exists,
startup does not promote or otherwise overwrite its role. An authenticated administrator can then
use `principal_set_role` to create or change additional principals and `principal_set_disabled` to
disable or re-enable them. Add each service's token digest to `SKILLWRIGHT_AUTH_TOKEN_HASHES` with
the same external key used for its provisioned principal.

For MCP deployments that validate an explicit resource, set
`SKILLWRIGHT_MCP_RESOURCE_SERVER_URL` to the externally visible MCP URL. The issuer reported by the
MCP token verifier is configured with `SKILLWRIGHT_AUTH_ISSUER_URL`.

## Record a skill

Recording is explicit. Start a recording, perform the task with Skillwright's proxied `browser_*`
tools, then stop the recording:

1. Call `skill_record_start` with a unique `name` and optional `description`.
2. Use `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_fill`, `browser_select`,
   and `browser_wait` to complete the task. Capture a snapshot before element actions so the
   snapshot ref can be compiled into semantic target evidence.
3. Call `skill_record_stop`. Skillwright compiles the successful actions and persists version 1 (or
   the next version when the skill already exists).
4. Inspect the result with `skill_get`, or enumerate history with `skill_versions`.

For example, an agent might make these MCP calls in sequence:

```text
skill_record_start(name="search-docs", description="Search the documentation")
browser_navigate(url="https://example.test/docs")
browser_snapshot()
browser_fill(target="e5", text="playwright", element="Search")
browser_snapshot()
browser_click(target="e9", element="Search")
skill_record_stop()
```

`skill_save_from_history` is an alternate authoring path when the useful interaction already
exists in browser history: provide a contiguous `start_event`/`end_event` range and Skillwright
compiles that range without requiring a new live recording.

## Parameterize recorded literals

`skill_parameterize` replaces selected recorded literals with typed `{{ input_name }}` templates
and creates a new immutable workflow version. Bindings use zero-based step indexes. Supported
binding locations are:

| Step operation | Binding field | Additional argument |
| --- | --- | --- |
| `navigate` | `url` | none |
| `fill` | `value` | none |
| `select` | `select_value` | `item_index` for the selected value |
| `wait` | `text` | none |
| `wait` | `text_gone` | none |

Input types are `string`, `number`, `integer`, and `boolean`. Example:

```text
skill_parameterize(
  name="search-docs",
  bindings=[
    {
      "step": 1,
      "field": "value",
      "input_name": "query",
      "input_type": "string",
      "description": "Search query"
    }
  ]
)
```

Inspect `skill_get` before parameterizing to confirm the current step indexes. Parameterization is
optimistic against the current version; concurrent changes return a conflict instead of silently
overwriting a newer version.

## Secret-safe recording and execution

Secrets require the explicit secret path. The current secret provider is the server environment;
a reference such as `LOGIN_PASSWORD` resolves to `SKILLWRIGHT_SECRET_LOGIN_PASSWORD`. Secret refs
must match `[A-Z][A-Z0-9_]*`.

Configure the value only in the process that needs to execute or demonstrate the secret action:

```dotenv
SKILLWRIGHT_SECRET_LOGIN_PASSWORD=<secret value>
```

During recording, an administrator uses `browser_fill_secret` instead of `browser_fill`:

```text
skill_record_start(name="portal-login")
browser_navigate(url="https://portal.example.test/login")
browser_snapshot()
browser_fill_secret(
  target="e7",
  secret_ref="LOGIN_PASSWORD",
  input_name="password",
  element="Password"
)
skill_record_stop()
```

`browser_fill_secret` resolves the value server-side, sends the plaintext only across the
Playwright boundary needed to fill the page, records a secret reference/input name instead of the
value, and redacts raw and URL-encoded variants from captured browser results and later snapshots
in that browser session. Compilation creates a required secret string input such as
`{{ password }}` and automatically binds that input to the recorded secret reference.

Use `skill_secret_status` to see which secret inputs are configured without exposing their refs or
values. Administrators with `manage` permission can change a binding with `skill_secret_bind` or
remove it with `skill_secret_unbind`. A run with a missing binding returns `secret_unavailable`;
if the binding exists but its environment value is unavailable when execution begins or resumes,
the run fails before that browser action starts. Values are resolved again after approval/repair
resumption, so rotating an environment secret does not require a workflow-version change.

`browser_fill` is **not secret-safe**. Its `text` argument is ordinary browser input and can be
stored in browser-action history, snapshots, run evidence, and logs/results. Never put credentials
or other secrets in `browser_fill`, ordinary workflow inputs, URLs, select values, wait text, or
other non-secret fields. Secret inputs are server-bound by design, and supplying one through
`skill_run(inputs=...)` or `POST /api/v1/runs` is rejected as invalid input.

## Run, inspect, and cancel deterministic workflows

Start a workflow with `skill_run`:

```text
skill_run(
  name="search-docs",
  inputs={"query": "playwright"},
  idempotency_key="docs-search-2026-09-14"
)
```

Omitting `version` pins the run to the skill's current version at submission time; supplying a
positive version runs that immutable historical definition. `idempotency_key` is unique per skill,
but reuse is accepted only for the exact same requester, pinned workflow version, and normalized
inputs. An exact retry returns the existing run; reusing the key for a different request is an
`idempotency_conflict` (HTTP `409` in the control API) and does not expose the existing run.

With `SKILLWRIGHT_EXECUTION_BACKEND=redis`, `skill_run` persists the run and returns a `run_id`
after queue submission. Use `skill_status(run_id)` until the run succeeds, fails, is cancelled, or
requires an intervention. With the local `inline` backend, `skill_run` executes in the MCP process
and may return the final result directly.

`skill_cancel(run_id)` records an attributed cancellation request. Queued/retrying runs and runs
currently waiting for approval or repair become terminal `cancelled` immediately; pending
intervention rows are closed so a late approval or repair cannot resume the cancelled run. A run
already executing a browser step observes cancellation at workflow boundaries. Status, current
step, outputs, and structured failure context are persisted so the control plane does not depend
on an in-memory job result.

## Approval gates

Approval gates are durable workflow metadata. Add one to a mutating step with:

```text
skill_approval_set(
  name="publish-draft",
  step=3,
  required=true,
  reason="Publishing makes the draft externally visible"
)
```

Only `click`, `fill`, and `select` steps can be gated. The change creates a new workflow version.
When execution reaches that step, the run becomes `approval_required` before the browser mutation
is dispatched and exposes an `approval_id`, reason, operation, step, and target in its intervention
context.

An authorized approver decides with:

```text
skill_approval_decide(
  approval_id="<approval-id>",
  approve=true,
  comment="Reviewed"
)
```

Approval resumes the same pinned run/version at the gated step. Run permission is checked again
before the approved action executes. Rejection is terminal and leaves that gated side effect
unstarted. In the Redis backend, a worker retains the live intervention session while it waits.
Approval and repair each have their own wait bound (900 seconds by default), and the bound resets
when a run transitions from one intervention kind to the other. Expiry produces the corresponding
explicit session-expired status.

## Structured repair and immutable versions

When a semantic target cannot be resolved uniquely, Skillwright does not guess and continue. The
run stops at that step with `status="repair_required"`. `skill_status` exposes failure context that
includes the zero-based step index, operation, expected target, current page, and ranked candidate
elements. Candidate IDs are scoped to that repair context.

Select a candidate and submit it with:

```text
skill_repair(
  run_id="<run-id>",
  step=2,
  replacement_element_id="el_0",
  persist=true
)
```

The live worker validates that the candidate is still uniquely present, regenerates a durable
locator when Playwright MCP can provide one, applies the replacement as a run-local override, and
resumes from the failed step. With the Redis backend the MCP/API request may return
`repair_pending`; follow the run with `skill_status` while the owning worker validates and resumes
it. A run can have only one pending/applying repair proposal at a time. Repeating the same proposal
returns that existing repair, while a competing proposal returns `repair_conflict` (HTTP `409` in
the control API) instead of creating two repair branches.

If the repaired run succeeds and `persist=true`, Skillwright creates a new workflow version whose
parent is the version the run used. The original version is never mutated. Persistence uses an
expected-current-version check, so a concurrent edit cannot be overwritten by an old repair; the
run can still finish with its local repair while reporting that the repair was not persisted.
`persist=false` repairs only the current run.

Use `skill_versions(name)` to inspect version history. `skill_rollback(name, version)` also
preserves immutability: it copies the selected historical definition into a new current version
instead of moving or editing old version rows.

## RBAC

Roles are `admin`, `developer`, and `viewer`. Per-skill permissions are `view`, `run`, `edit`,
`approve`, and `manage`.

| Role | Global capabilities | Skill behavior |
| --- | --- | --- |
| `admin` | Browser tools, skill creation, principal administration, secret administration | All permissions on all skills |
| `developer` | Browser tools and skill creation | Full permissions on owned skills; otherwise only explicitly granted permissions |
| `viewer` | No browser or skill-creation capability | `view` only, for skills they still own or where `view` is explicitly granted |

Per-skill permissions control these operations:

| Permission | Representative operations |
| --- | --- |
| `view` | `skill_get`, `skill_versions`, `skill_status`, visibility in list/search |
| `run` | `skill_run`, `skill_cancel` |
| `edit` | `skill_parameterize`, `skill_approval_set`, `skill_repair`, `skill_rollback` |
| `approve` | `skill_approval_decide` and approval intervention access |
| `manage` | `skill_access_grant`, `skill_access_revoke`, `skill_access_get`, `skill_secret_status`; secret binding/unbinding also requires global admin |

Principals with `manage` permission can grant/revoke per-skill permissions with
`skill_access_grant` and `skill_access_revoke`; `skill_access_get` lists current grants.
`principal_set_role` and
`principal_set_disabled` are administrator-only. Disabled principals are rejected even if a token
mapping or skill grant still exists.

The MCP server currently exposes these product tools:

| Group | Tools |
| --- | --- |
| Browser | `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_fill`, `browser_fill_secret`, `browser_select`, `browser_wait` |
| Record/discover | `skill_record_start`, `skill_record_stop`, `skill_save_from_history`, `skill_list`, `skill_search`, `skill_get` |
| Author/configure | `skill_parameterize`, `skill_secret_bind`, `skill_secret_unbind`, `skill_secret_status`, `skill_approval_set` |
| Execute/intervene | `skill_run`, `skill_status`, `skill_cancel`, `skill_repair`, `skill_approval_decide` |
| Version/access | `skill_versions`, `skill_rollback`, `skill_access_grant`, `skill_access_revoke`, `skill_access_get` |
| Principals | `principal_set_role`, `principal_set_disabled` |

## Control API

The control API operates existing skills and runs; skill recording and authoring remain MCP tools.
`/health/live` and `/health/ready` are health endpoints. `/api/v1/*` requests require a valid bearer
token unless loopback-only unauthenticated local mode is explicitly enabled.

| Method | Endpoint | Purpose / required skill permission |
| --- | --- | --- |
| `GET` | `/health/live` | Process liveness |
| `GET` | `/health/ready` | PostgreSQL connectivity, expected Alembic schema head, and (for Redis execution) Redis readiness |
| `POST` | `/api/v1/runs` | Create/queue a run; `run` |
| `GET` | `/api/v1/runs/{run_id}` | Safe persisted run status; `view` |
| `POST` | `/api/v1/runs/{run_id}/cancel` | Request cancellation; `run` |
| `GET` | `/api/v1/runs/{run_id}/intervention` | Fetch current repair/approval context; `edit` for repair, `approve` for approval |
| `POST` | `/api/v1/runs/{run_id}/repair` | Submit `step`, `replacement_element_id`, and optional `persist`; `edit` |
| `POST` | `/api/v1/approvals/{approval_id}/decision` | Submit `approve` and optional `comment`; `approve` |

Create a run over HTTP with:

```sh
curl -X POST http://127.0.0.1:8767/api/v1/runs \
  -H 'Authorization: Bearer YOUR_OPAQUE_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"skill":"search-docs","inputs":{"query":"playwright"},"idempotency_key":"docs-search-1"}'
```

The create/status response intentionally contains run metadata (`run_id`, status, pinned workflow
version, current step, cancellation flag, and timestamps) rather than the input payload, outputs,
or raw failure context. Fetch structured repair/approval data only through the intervention
endpoint, where the corresponding skill permission is enforced.

## Observability

OpenTelemetry is opt-in in source deployments:

```dotenv
SKILLWRIGHT_OTEL_ENABLED=true
SKILLWRIGHT_OTEL_SERVICE_NAME=skillwright-mcp
SKILLWRIGHT_OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
```

The endpoint is an OTLP/HTTP base URL; Skillwright exports to `/v1/traces` and `/v1/metrics`.
FastAPI and Taskiq are instrumented when telemetry is enabled. Skillwright also emits spans for
`skill.run`, `workflow.step`, and `browser.action` and the following application metrics:

| Metric | Meaning |
| --- | --- |
| `skillwright.runs.active` | Active workflow execution segments |
| `skillwright.runs.outcomes` | Run-segment outcomes by status |
| `skillwright.workflow.duration` | Workflow execution-segment duration |
| `skillwright.workflow.step.duration` | Step duration by operation/status |
| `skillwright.browser.action.duration` | Playwright MCP call latency by tool/source/status |
| `skillwright.repair.required` | Steps that stopped for target repair |
| `skillwright.queue.wait.duration` | Time from durable run creation to first worker claim |
| `skillwright.runs.stale_recovery` | Stale-run recovery outcomes |
| `skillwright.queue.publish` | Queue publish success/failure/recovery |

Manual execution/browser spans disable automatic exception recording and use IDs, operation names,
statuses, and timing rather than browser arguments. Secret-aware browser execution also redacts
known secret values from persisted/browser error evidence before telemetry sees it. Operators
should still keep secrets out of every ordinary/non-secret input field.

## Security boundaries and current limitations

- The explicit secret path is `browser_fill_secret` plus a server-side `SKILLWRIGHT_SECRET_*`
  binding. `browser_fill` and other ordinary fields are not secret-safe and may be persisted as
  normal workflow/run evidence.
- Secret values are not accepted from run callers. Only the current `env` secret provider is
  implemented; Skillwright stores the provider/reference binding, not the environment value.
- PostgreSQL contains normal non-secret workflow inputs, browser-action arguments/results,
  snapshots, outputs, repair/approval records, and audit events. Treat that database as application
  data requiring normal production access controls and backups.
- Playwright MCP and the destination website are inside the execution trust boundary: a secret must
  exist in worker/MCP process memory long enough to be sent to the browser and the website that
  consumes it.
- Production bearer authentication is an operator-provisioned opaque service-token map. Skillwright
  does not issue user sessions or provide a frontend identity/login flow. Terminate TLS and apply
  network policy at the deployment edge.
- Local source defaults permit an unauthenticated local principal for development. Production
  containers set `SKILLWRIGHT_ALLOW_UNAUTHENTICATED_LOCAL=false`; keep it disabled on shared or
  remotely reachable services.
- Redis workers keep a browser session alive only for the bounded repair/approval wait window.
  Expired intervention sessions are explicit terminal statuses rather than silently rebuilding a
  browser state that may no longer match the failed run.
- Interactive MCP browser state is isolated by authenticated principal plus MCP transport session,
  and Redis worker browser artifacts use a separate SHA-256-keyed output directory per run. Raw
  principal IDs, transport-session IDs, and run IDs are not used as artifact-directory names.
- Worker ownership is heartbeated continuously during long browser steps. If ownership is lost,
  the original worker aborts instead of continuing into later workflow steps. Stale recovery will
  not replay a run once any replay click/fill/select mutation was dispatched, even if its browser
  action outcome was recorded; that ambiguity is surfaced as `failed_unknown` instead of risking a
  duplicate side effect after worker loss.
- The repository is backend-only. It currently ships no web frontend and no Kubernetes manifests.
  Docker/Compose is the included deployment topology.

## Production / AWS next steps

The current container/Compose topology maps cleanly to an initial AWS deployment: publish the
single image to ECR; run the API, MCP service, and workers as separate ECS/Fargate services; run
Alembic as a one-off deployment task; use RDS PostgreSQL and ElastiCache Redis; and send OTLP to an
ADOT/OpenTelemetry Collector for CloudWatch/X-Ray or a Prometheus-compatible backend. Put the API
and MCP endpoints behind TLS and private/network controls appropriate to their callers, and keep
workers/database/Redis in private subnets.

Before horizontally scaling the stateful Streamable HTTP MCP service, preserve session affinity so
each MCP session keeps reaching the process that owns its interactive browser session, or move
interactive browser-session state behind a separately managed browser service. The next security
integration should add an AWS Secrets Manager or SSM-backed secret provider while preserving the
same opaque workflow binding contract. Managed browser-artifact storage, worker autoscaling, and
backup/retention policy can then be added independently. No Kubernetes layer is required for that
path.

## Container image

Build the production image with:

```sh
docker build -t skillwright-mcp .
```

The image installs `@playwright/mcp@0.0.81` and its matching Chromium during the build. It
removes npm and npx before runtime, runs as UID `10001`, and disables the unauthenticated local
principal by default. MCP bearer middleware is enabled when `SKILLWRIGHT_AUTH_TOKEN_HASHES` is
configured; without a configured token map, product tool calls still fail closed instead of
creating an implicit remote principal.
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
  -e SKILLWRIGHT_WORKER_CONCURRENCY=2 \
  skillwright-mcp worker
```

Each worker container runs one Taskiq process and allows `SKILLWRIGHT_WORKER_CONCURRENCY`
concurrent async run tasks (default `2`). Each active run owns an isolated Playwright MCP process;
repair or approval waits keep that run's worker slot and browser session reserved, so size worker
capacity for the expected number of simultaneous executions and interventions.

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
