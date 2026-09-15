#!/bin/sh
set -eu

terraform_image="${SKILLWRIGHT_TERRAFORM_IMAGE:-hashicorp/terraform:1.13.5}"
endpoint_url="${LOCALSTACK_ENDPOINT:-http://127.0.0.1:4566}"
terraform_endpoint="${LOCALSTACK_TERRAFORM_ENDPOINT:-}"
repo_root="$(git rev-parse --show-toplevel)"
module_dir="$repo_root/infra/aws/terraform"
run_id="$(cd "$repo_root" && uv run python -c 'import secrets; print(secrets.token_hex(4))')"
name_prefix="swls-$run_id"
release_tag="smoke-$run_id"
candidate_tag="candidate-$run_id"
work_rel=".skillwright/localstack-production-$run_id"
work_dir="$repo_root/$work_rel"
terraform_initialized=0
bootstrap_created=0

# Git for Windows rewrites Docker's Linux-side paths unless argument conversion is disabled.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

command -v docker >/dev/null 2>&1 || {
    echo "docker is required for the LocalStack production smoke" >&2
    exit 1
}
command -v uv >/dev/null 2>&1 || {
    echo "uv is required for the LocalStack production smoke" >&2
    exit 1
}

if [ -z "$terraform_endpoint" ]; then
    terraform_endpoint="$(printf '%s' "$endpoint_url" | \
        sed -e 's#://127\.0\.0\.1:#://host.docker.internal:#' \
            -e 's#://localhost:#://host.docker.internal:#')"
fi

terraform_prod() {
    case "$(uname -s)" in
        MINGW*|MSYS*|CYGWIN*)
            docker run --rm \
                -e AWS_ACCESS_KEY_ID=test \
                -e AWS_SECRET_ACCESS_KEY=test \
                -e AWS_DEFAULT_REGION=us-east-1 \
                -e AWS_REGION=us-east-1 \
                -e AWS_EC2_METADATA_DISABLED=true \
                -e AWS_ENDPOINT_URL="$terraform_endpoint" \
                -v "$repo_root:/work" \
                -w "/work/$work_rel" \
                "$terraform_image" "$@"
            ;;
        *)
            docker run --rm \
                --user "$(id -u):$(id -g)" \
                --add-host host.docker.internal:host-gateway \
                -e HOME=/tmp \
                -e AWS_ACCESS_KEY_ID=test \
                -e AWS_SECRET_ACCESS_KEY=test \
                -e AWS_DEFAULT_REGION=us-east-1 \
                -e AWS_REGION=us-east-1 \
                -e AWS_EC2_METADATA_DISABLED=true \
                -e AWS_ENDPOINT_URL="$terraform_endpoint" \
                -v "$repo_root:/work" \
                -w "/work/$work_rel" \
                "$terraform_image" "$@"
            ;;
    esac
}

cleanup_bootstrap() {
    SKILLWRIGHT_LOCALSTACK_ENDPOINT="$endpoint_url" \
    SKILLWRIGHT_LOCALSTACK_WORK_DIR="$work_dir" \
    uv run python - <<'PY'
import json
import os
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

work_dir = Path(os.environ["SKILLWRIGHT_LOCALSTACK_WORK_DIR"])
bootstrap_path = work_dir / "bootstrap.json"
if not bootstrap_path.exists():
    raise SystemExit(0)

data = json.loads(bootstrap_path.read_text())
endpoint = os.environ["SKILLWRIGHT_LOCALSTACK_ENDPOINT"]
region = "us-east-1"

def client(service: str):
    return boto3.client(
        service,
        region_name=region,
        endpoint_url=endpoint,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )

errors: list[str] = []
acm = client("acm")
ec2 = client("ec2")

try:
    acm.delete_certificate(CertificateArn=data["certificate_arn"])
except ClientError as exc:
    errors.append(f"certificate cleanup failed: {exc.response.get('Error', {}).get('Code', 'unknown')}")

for subnet_id in [*data["private_subnet_ids"], *data["public_subnet_ids"]]:
    try:
        ec2.delete_subnet(SubnetId=subnet_id)
    except ClientError as exc:
        errors.append(
            f"subnet {subnet_id} cleanup failed: "
            f"{exc.response.get('Error', {}).get('Code', 'unknown')}"
        )

try:
    ec2.delete_vpc(VpcId=data["vpc_id"])
except ClientError as exc:
    errors.append(f"VPC cleanup failed: {exc.response.get('Error', {}).get('Code', 'unknown')}")

if errors:
    raise RuntimeError("; ".join(errors))
PY
}

cleanup() {
    status=$?
    cleanup_failed=0
    trap - EXIT HUP INT TERM

    if [ "$terraform_initialized" -eq 1 ] && [ -f "$work_dir/terraform.tfstate" ]; then
        if ! terraform_prod destroy -auto-approve -input=false >/dev/null 2>&1; then
            echo "LocalStack production Terraform cleanup failed; state retained at $work_dir" >&2
            cleanup_failed=1
        fi
    fi

    if [ "$bootstrap_created" -eq 1 ]; then
        if ! (cd "$repo_root" && cleanup_bootstrap); then
            echo "LocalStack production bootstrap cleanup failed; details retained at $work_dir" >&2
            cleanup_failed=1
        fi
    fi

    if [ "$cleanup_failed" -eq 0 ]; then
        rm -rf "$work_dir"
    elif [ "$status" -eq 0 ]; then
        status=1
    fi
    exit "$status"
}
trap cleanup EXIT HUP INT TERM

mkdir -p "$work_dir"
cp "$module_dir"/*.tf "$work_dir/"
cp "$module_dir/.terraform.lock.hcl" "$work_dir/.terraform.lock.hcl"

cd "$repo_root"
SKILLWRIGHT_LOCALSTACK_ENDPOINT="$endpoint_url" uv run python - <<'PY'
import json
import os
import urllib.request

endpoint = os.environ["SKILLWRIGHT_LOCALSTACK_ENDPOINT"].rstrip("/")
try:
    with urllib.request.urlopen(f"{endpoint}/_localstack/health", timeout=5) as response:
        payload = json.load(response)
except Exception as exc:
    raise SystemExit(
        f"LocalStack is not reachable at {endpoint}. Start the licensed AWS emulator with `lstk start`: {exc}"
    ) from None

if payload.get("edition") != "pro":
    raise SystemExit(
        "the production smoke requires a licensed LocalStack Student/Pro AWS emulator"
    )
PY

export SKILLWRIGHT_LOCALSTACK_ENDPOINT="$endpoint_url"
export SKILLWRIGHT_LOCALSTACK_WORK_DIR="$work_dir"
export SKILLWRIGHT_LOCALSTACK_RUN_ID="$run_id"
export SKILLWRIGHT_LOCALSTACK_NAME_PREFIX="$name_prefix"
export SKILLWRIGHT_LOCALSTACK_RELEASE_TAG="$release_tag"
export SKILLWRIGHT_LOCALSTACK_CANDIDATE_TAG="$candidate_tag"

uv run python - <<'PY'
import json
import os
from pathlib import Path

import boto3

endpoint = os.environ["SKILLWRIGHT_LOCALSTACK_ENDPOINT"]
work_dir = Path(os.environ["SKILLWRIGHT_LOCALSTACK_WORK_DIR"])
run_id = os.environ["SKILLWRIGHT_LOCALSTACK_RUN_ID"]
prefix = os.environ["SKILLWRIGHT_LOCALSTACK_NAME_PREFIX"]
release_tag = os.environ["SKILLWRIGHT_LOCALSTACK_RELEASE_TAG"]
region = "us-east-1"

def client(service: str):
    return boto3.client(
        service,
        region_name=region,
        endpoint_url=endpoint,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )

ec2 = client("ec2")
acm = client("acm")
vpc_id = ec2.create_vpc(CidrBlock="10.97.0.0/16")["Vpc"]["VpcId"]
zones = [item["ZoneName"] for item in ec2.describe_availability_zones()["AvailabilityZones"][:2]]
if len(zones) < 2:
    raise RuntimeError("LocalStack did not expose at least two availability zones")

private_subnet_ids: list[str] = []
public_subnet_ids: list[str] = []
for index, zone in enumerate(zones, start=1):
    private_subnet_ids.append(
        ec2.create_subnet(
            VpcId=vpc_id,
            CidrBlock=f"10.97.{index}.0/24",
            AvailabilityZone=zone,
        )["Subnet"]["SubnetId"]
    )
    public_subnet_ids.append(
        ec2.create_subnet(
            VpcId=vpc_id,
            CidrBlock=f"10.97.{index + 10}.0/24",
            AvailabilityZone=zone,
        )["Subnet"]["SubnetId"]
    )

certificate_arn = acm.request_certificate(
    DomainName=f"{run_id}.skillwright.local",
    ValidationMethod="DNS",
)["CertificateArn"]

bootstrap = {
    "vpc_id": vpc_id,
    "private_subnet_ids": private_subnet_ids,
    "public_subnet_ids": public_subnet_ids,
    "certificate_arn": certificate_arn,
}
(work_dir / "bootstrap.json").write_text(json.dumps(bootstrap, indent=2))

def hcl_list(values: list[str]) -> str:
    return "[" + ", ".join(json.dumps(value) for value in values) + "]"

tfvars = f'''aws_region = "us-east-1"
name_prefix = "{prefix}"
vpc_id = "{vpc_id}"
private_subnet_ids = {hcl_list(private_subnet_ids)}
public_subnet_ids = {hcl_list(public_subnet_ids)}
certificate_arn = "{certificate_arn}"
external_base_url = "https://{run_id}.skillwright.local"
image_tag = "{release_tag}"
services_enabled = false
enable_autoscaling = false
alb_deletion_protection = false
database_deletion_protection = false
database_skip_final_snapshot = true
database_multi_az = false
database_backup_retention_days = 1
database_instance_class = "db.t3.micro"
database_allocated_storage_gib = 20
database_max_allocated_storage_gib = 20
redis_node_type = "cache.t3.micro"
redis_replica_count = 0
redis_snapshot_retention_days = 0
log_retention_days = 1
'''
(work_dir / "terraform.tfvars").write_text(tfvars)
PY
bootstrap_created=1

terraform_prod init -backend=false -input=false -lockfile=readonly >/dev/null
terraform_initialized=1
terraform_prod apply -auto-approve -input=false >/dev/null

uv run python - <<'PY'
import json
import os

import boto3

endpoint = os.environ["SKILLWRIGHT_LOCALSTACK_ENDPOINT"]
prefix = os.environ["SKILLWRIGHT_LOCALSTACK_NAME_PREFIX"]
release_tag = os.environ["SKILLWRIGHT_LOCALSTACK_RELEASE_TAG"]
region = "us-east-1"

def client(service: str):
    return boto3.client(
        service,
        region_name=region,
        endpoint_url=endpoint,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )

ecr = client("ecr")
ecs = client("ecs")
iam = client("iam")
rds = client("rds")
secretsmanager = client("secretsmanager")
ssm = client("ssm")
elasticache = client("elasticache")
elbv2 = client("elbv2")
logs = client("logs")

repository = ecr.describe_repositories(repositoryNames=[prefix])["repositories"][0]
assert repository["imageTagMutability"] == "IMMUTABLE"
assert repository["imageScanningConfiguration"]["scanOnPush"] is True

services = ecs.describe_services(
    cluster=prefix,
    services=[f"{prefix}-api", f"{prefix}-mcp", f"{prefix}-worker"],
)["services"]
assert len(services) == 3
assert all(service["desiredCount"] == 0 for service in services)
for family in [f"{prefix}-api", f"{prefix}-mcp", f"{prefix}-worker", f"{prefix}-migration"]:
    assert ecs.list_task_definitions(familyPrefix=family)["taskDefinitionArns"]

role_names = {role["RoleName"] for role in iam.list_roles()["Roles"]}
for suffix in ["api-task", "runtime-task", "mcp-task", "migration-task", "ecs-execution"]:
    assert f"{prefix}-{suffix}" in role_names

database = rds.describe_db_instances(DBInstanceIdentifier=f"{prefix}-postgres")["DBInstances"][0]
assert database["StorageEncrypted"] is True
assert database["PubliclyAccessible"] is False
assert database["MultiAZ"] is False

runtime_secret = secretsmanager.describe_secret(SecretId=f"{prefix}/database-runtime")
assert runtime_secret["Name"] == f"{prefix}/database-runtime"
marker = ssm.get_parameter(Name=f"/{prefix}/migration-image-tag")["Parameter"]["Value"]
assert marker == "unmigrated"

redis = elasticache.describe_replication_groups(ReplicationGroupId=f"{prefix}-redis")[
    "ReplicationGroups"
][0]
assert redis["AtRestEncryptionEnabled"] is True
assert redis["TransitEncryptionEnabled"] is True

load_balancer = elbv2.describe_load_balancers(Names=[f"{prefix}-alb"])["LoadBalancers"][0]
assert load_balancer["Type"] == "application"
for target_group_name in [f"{prefix}-api", f"{prefix}-mcp"]:
    target_group = elbv2.describe_target_groups(Names=[target_group_name])["TargetGroups"][0]
    assert target_group["HealthCheckPath"] == "/health/ready"
    assert target_group["Matcher"]["HttpCode"] == "200"
    if target_group_name.endswith("-mcp"):
        attributes = {
            item["Key"]: item["Value"]
            for item in elbv2.describe_target_group_attributes(
                TargetGroupArn=target_group["TargetGroupArn"]
            )["Attributes"]
        }
        assert attributes["stickiness.enabled"] == "true"
        assert attributes["stickiness.lb_cookie.duration_seconds"] == "1800"
        assert attributes["deregistration_delay.timeout_seconds"] == "300"

log_groups = {
    group["logGroupName"]
    for group in logs.describe_log_groups(logGroupNamePrefix=f"/skillwright/{prefix}/")["logGroups"]
}
for suffix in ["api", "mcp", "worker", "migration", "adot"]:
    assert f"/skillwright/{prefix}/{suffix}" in log_groups

print(
    json.dumps(
        {
            "production_resources": "verified",
            "ecs_service_desired_count": 0,
            "migration_marker": marker,
            "release_tag": release_tag,
        }
    )
)
PY

terraform_prod apply -auto-approve -input=false \
    -var="migration_image_tag=$candidate_tag" >/dev/null

uv run python - <<'PY'
import os

import boto3

endpoint = os.environ["SKILLWRIGHT_LOCALSTACK_ENDPOINT"]
prefix = os.environ["SKILLWRIGHT_LOCALSTACK_NAME_PREFIX"]
release_tag = os.environ["SKILLWRIGHT_LOCALSTACK_RELEASE_TAG"]
candidate_tag = os.environ["SKILLWRIGHT_LOCALSTACK_CANDIDATE_TAG"]
ecs = boto3.client(
    "ecs",
    region_name="us-east-1",
    endpoint_url=endpoint,
    aws_access_key_id="test",
    aws_secret_access_key="test",
)

def image_for(family: str, container_name: str) -> str:
    arn = ecs.list_task_definitions(
        familyPrefix=family,
        sort="DESC",
        maxResults=1,
    )["taskDefinitionArns"][0]
    definition = ecs.describe_task_definition(taskDefinition=arn)["taskDefinition"]
    return next(
        container["image"]
        for container in definition["containerDefinitions"]
        if container["name"] == container_name
    )

assert image_for(f"{prefix}-migration", "skillwright-migration").endswith(f":{candidate_tag}")
for family_suffix, container_name in [
    ("api", "skillwright-api"),
    ("mcp", "skillwright-mcp"),
    ("worker", "skillwright-worker"),
]:
    assert image_for(f"{prefix}-{family_suffix}", container_name).endswith(f":{release_tag}")
PY

stale_plan_log="$work_dir/stale-rollout.log"
if terraform_prod plan -input=false \
    -var="image_tag=$candidate_tag" \
    -var="migration_image_tag=$candidate_tag" \
    -var="services_enabled=true" >"$stale_plan_log" 2>&1; then
    echo "service rollout unexpectedly passed with a stale migration marker" >&2
    exit 1
fi
if ! grep -q "successful migration marker" "$stale_plan_log"; then
    echo "service rollout failed for an unexpected reason; see $stale_plan_log" >&2
    exit 1
fi

uv run python - <<'PY'
import os

import boto3

client = boto3.client(
    "ssm",
    region_name="us-east-1",
    endpoint_url=os.environ["SKILLWRIGHT_LOCALSTACK_ENDPOINT"],
    aws_access_key_id="test",
    aws_secret_access_key="test",
)
client.put_parameter(
    Name=f"/{os.environ['SKILLWRIGHT_LOCALSTACK_NAME_PREFIX']}/migration-image-tag",
    Type="String",
    Value=os.environ["SKILLWRIGHT_LOCALSTACK_CANDIDATE_TAG"],
    Overwrite=True,
)
PY

terraform_prod plan -input=false \
    -var="image_tag=$candidate_tag" \
    -var="migration_image_tag=$candidate_tag" \
    -var="services_enabled=true" >/dev/null

printf 'LocalStack production AWS smoke passed (full Terraform module + migration gate).\n'
