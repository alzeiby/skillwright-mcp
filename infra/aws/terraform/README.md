# Skillwright AWS / ECS deployment

This Terraform stack is the production deployment slice for the existing Skillwright container.
It intentionally uses ECS/Fargate rather than Kubernetes and does not add any frontend surface.

It provisions:

- an immutable, scan-on-push ECR repository;
- an ECS cluster with separate API, MCP, and deterministic worker services;
- a one-off Alembic migration task definition plus an SSM migration-image promotion marker;
- an encrypted RDS PostgreSQL instance whose master password is managed by RDS in Secrets Manager,
  with a separate least-privilege runtime login stored in a dedicated managed secret;
- an encrypted, TLS-only ElastiCache Redis replication group;
- an HTTPS ALB routing `/mcp` to the MCP service and all other paths to the control API;
- MCP target-group stickiness plus ECS task scale-in protection for stateful Streamable HTTP browser
  sessions;
- per-service CloudWatch log groups and an ADOT sidecar exporting traces to X-Ray and metrics via EMF;
- task/execution IAM roles with separate permissions for ECS secret injection versus runtime workflow
  secret resolution;
- optional ECS target-tracking autoscaling; and
- backup, retention, Multi-AZ, and deletion-protection controls.

## Prerequisites

Supply an existing VPC with at least two private subnets and two ALB subnets. Private task subnets
must have egress through NAT or the required VPC endpoints for ECR, CloudWatch Logs, Secrets
Manager/SSM, and AWS telemetry APIs. Workers and MCP tasks also need NAT/private-egress access to
the external websites they automate. Supply an ACM certificate for the HTTPS listener.
`external_base_url` must be the canonical hostname covered by that certificate; create its Route 53
alias to the `alb_dns_name` output.

Terraform never needs the plaintext Skillwright bearer tokens or workflow secrets. Put service
configuration such as `SKILLWRIGHT_AUTH_TOKEN_HASHES` in pre-created Secrets Manager secrets and
pass only their ARNs through `service_secret_arns`; these service secrets are injected only into
the API and MCP task definitions. Workflow secret permissions are separately
allow-listed through `workflow_secrets_manager_arns`, `workflow_ssm_parameter_arns`, and optional
KMS key ARNs. If a service-injected secret uses a customer-managed KMS key, list that key under
`service_secret_kms_key_arns` so the ECS execution role can decrypt it.

RDS manages its own master password. ECS injects that password only into the migration task. On the
first migration, Skillwright generates the runtime database password inside that task, writes only
the runtime `{username,password}` value into the dedicated `database-runtime` Secrets Manager
secret, creates a constrained PostgreSQL login, runs Alembic as the RDS administrator, and grants
the runtime role DML/sequence access after the migration. API/MCP/worker task definitions receive
only the runtime password field. The application role is explicitly `NOSUPERUSER`, `NOCREATEDB`,
`NOCREATEROLE`, `NOREPLICATION`, and `NOBYPASSRLS`, and receives read-only access to
`alembic_version`.

All AWS PostgreSQL connections require `verify-full` TLS. The production image checksum-pins the
AWS RDS global CA bundle and Terraform points `SKILLWRIGHT_DATABASE_SSL_ROOT_CERT` at that bundle.
Redis uses `rediss://` together with ElastiCache transit encryption.

API, MCP, worker, and migration use separate-purpose ECS task roles. Only MCP/worker receive
workflow Secrets Manager/SSM permissions; only MCP receives ECS task-protection permissions; the
migration role can initialize the runtime DB secret and write the migration marker. Task trust
policies are restricted to the current AWS account. The ADOT v0.50.0 multi-architecture image is
pinned by OCI digest rather than a mutable tag.

For the first authenticated deployment, set `bootstrap_admin_principal` to the same principal key
referenced by your bearer-token hash secret. Once that principal has been created in PostgreSQL,
remove the bootstrap value on a later deployment.

## First deployment

Copy `terraform.tfvars.example` to an environment-specific tfvars file and keep
`services_enabled = false` for the first apply:

```sh
terraform init
terraform fmt -check -recursive
terraform validate
terraform plan -out=tfplan
terraform apply tfplan
```

## Local Terraform / AWS validation

The AWS deployment has three local validation layers. CI always runs the Terraform native test
suite in `tests/production.tftest.hcl` with Terraform 1.13.5 and the locked AWS provider. Those
mock-provider tests exercise production invariants and rollout preconditions without requiring AWS
or LocalStack.

The mandatory, license-free LocalStack smoke exercises real Terraform provider and boto3 calls for
Secrets Manager and SSM:

```sh
sh scripts/localstack-aws-smoke.sh
```

The script starts an isolated LocalStack Community 4.8.1 container, then uses Terraform 1.13.5 in a container to
apply the test-only `localstack-smoke` fixture. That fixture creates a Secrets Manager secret and an
SSM `SecureString`. Skillwright then resolves both values through its normal boto3-backed
`SecretResolver`, rotates both values through boto3, and resolves them again to verify that
plaintext values are not cached. The script destroys the fixture, removes its container, and clears
its temporary Terraform state on exit. It requires Docker, `uv`, and `curl`; no AWS account or AWS
credentials are used. Override `SKILLWRIGHT_LOCALSTACK_IMAGE` or `SKILLWRIGHT_TERRAFORM_IMAGE` to
test another container version.

To reuse an already-running LocalStack instance, set `LOCALSTACK_ENDPOINT`, for example
`LOCALSTACK_ENDPOINT=http://127.0.0.1:4566 sh scripts/localstack-aws-smoke.sh`. The Terraform
container automatically rewrites local loopback endpoints to `host.docker.internal`; if your Docker
runtime needs a different address, set `LOCALSTACK_TERRAFORM_ENDPOINT` explicitly.

With a running licensed LocalStack Student/Pro AWS emulator, run the deeper production-module gate:

```sh
lstk start
sh scripts/localstack-production-smoke.sh
```

That smoke uses the actual files in this production Terraform module. It creates an isolated VPC,
four subnets, and ACM certificate in LocalStack, applies the production module with long-lived ECS
service counts forced to zero, and verifies the resulting ECR, ECS, IAM, RDS, Secrets Manager, SSM,
ElastiCache, ALB, target-group, and CloudWatch resources through boto3. It then changes only the
migration task to a candidate image, proves an `unmigrated`/stale SSM marker blocks service rollout,
writes the candidate marker, and proves the matching rollout plan passes without actually starting
Fargate tasks. The script destroys only resources from its own run and does not stop a LocalStack
emulator that you started separately. Override `LOCALSTACK_ENDPOINT`,
`LOCALSTACK_TERRAFORM_ENDPOINT`, or `SKILLWRIGHT_TERRAFORM_IMAGE` when needed.

LocalStack is a control-plane compatibility gate, not a replacement for a real AWS pre-production
deployment. Actual Fargate task launch behavior, AWS-managed RDS/ElastiCache engine behavior and
networking, real ALB DNS/TLS, IAM propagation, and other managed-service semantics still require a
real AWS environment before production promotion.

Build the repository Docker image, tag it with an immutable release/Git SHA, and push it to
`ecr_repository_url`. On the first deployment, set `image_tag` to that tag while services remain
disabled, apply Terraform to register the migration task, then run it. One way to do that is:

```sh
aws ecs run-task \
  --cluster "$(terraform output -raw ecs_cluster_name)" \
  --task-definition "$(terraform output -raw migration_task_definition_arn)" \
  --launch-type FARGATE \
  --platform-version 1.4.0 \
  --network-configuration "awsvpcConfiguration={subnets=[$(terraform output -json private_subnet_ids | jq -r 'join(",")')],securityGroups=[$(terraform output -raw ecs_task_security_group_id)],assignPublicIp=DISABLED}"
```

Wait for the migration task to exit successfully. It writes its immutable image tag to the SSM
migration marker only after Alembic and runtime-role grants succeed. Then set `services_enabled =
true` and apply again. Terraform refuses to promote any long-lived service unless `image_tag`
matches that marker.

For every later release, including a code-only release, keep the currently deployed `image_tag`
unchanged and set `migration_image_tag` to the new candidate tag. Apply once to register only the
candidate migration task, run that task and verify exit code 0, then promote `image_tag` to the same
candidate and apply again. This prevents Terraform from rolling new application code against an
older schema. Database migrations must still follow expand/contract compatibility: the migration
runs while the previous application revision is serving traffic, so destructive changes that break
the old revision belong in a later contract release.

## Stateful MCP scaling

The MCP process owns interactive Playwright sessions in memory. The `/mcp` target group uses an
ALB-generated sticky cookie, and `BrowserSessionPool` enables ECS task scale-in protection while the
task owns one or more interactive sessions. Protection is refreshed on use and removed after the
last session is closed/reaped. This covers ECS deployments and service autoscaling scale-in, while
the cookie keeps requests on the same healthy target. Clients must preserve cookies for the MCP
session. Forced task termination, host failure, or an expired protection lease can still end the
in-memory session; deterministic Redis worker runs are independent of this interactive MCP state.

Both ALB target groups use dependency-aware `/health/ready` checks with matcher `200`. MCP readiness
checks PostgreSQL, exact Alembic head, Redis, and runtime startup without launching Playwright.

## Worker sizing and autoscaling

Each worker task runs one Taskiq process with `SKILLWRIGHT_WORKER_CONCURRENCY` async run slots.
Repair and approval waits intentionally retain the same browser process and worker slot. CPU target
tracking is provided as a conservative baseline, but production worker scaling should eventually
use queue depth / oldest-message age once those Redis metrics are exported to CloudWatch. Do not
increase concurrency beyond measured Chromium memory capacity merely to reduce queue depth.

## Secret providers

Persisted workflow bindings contain only provider/reference metadata. In ECS, use:

- provider `aws-secrets-manager` with a full secret ARN or allowed secret name; or
- provider `aws-ssm` with a SecureString parameter ARN/name.

Skillwright calls the AWS SDK from the task role at execution/resume time, so secret rotation is
picked up without creating a new workflow version. Secrets Manager binary values are intentionally
unsupported; store the browser fill value as `SecretString`. SSM reads use `WithDecryption=true`.

Keep the task-role ARN allow-lists narrow. If secrets use customer-managed KMS keys, add only those
keys to `workflow_kms_key_arns`.

## Operations

- RDS backups default to 14 days; ElastiCache snapshots default to 7 days.
- RDS, the ALB, and other destructive controls are intentionally conservative by default.
- CloudWatch log retention defaults to 30 days and can be changed independently.
- RDS and Redis are private; only the ECS task security group can connect to them.
- The ALB is public by default. Set `alb_internal = true` for private-only access and route callers
  through your private network/VPN. Use `alb_ingress_cidrs` to restrict HTTPS source networks even
  when the ALB remains internet-facing.
- Create a Route 53 alias from the hostname in `external_base_url` to the `alb_dns_name` output.
  Skillwright derives the MCP resource-server URL from that canonical origin.
- Terraform state remains sensitive operational data even though this module avoids reading
  application secret values. Protect remote state with encryption, locking, and strict IAM.
