#!/bin/sh
set -eu

localstack_image="${SKILLWRIGHT_LOCALSTACK_IMAGE:-localstack/localstack:4.8.1}"
terraform_image="${SKILLWRIGHT_TERRAFORM_IMAGE:-hashicorp/terraform:1.13.5}"
attempts="${SKILLWRIGHT_LOCALSTACK_ATTEMPTS:-60}"
run_id="smoke-$$"
container_name="skillwright-localstack-$run_id"
repo_root="$(git rev-parse --show-toplevel)"
work_rel=".skillwright/localstack-$run_id"
work_dir="$repo_root/$work_rel"
fixture_dir="$repo_root/infra/aws/terraform/localstack-smoke"
terraform_applied=0
started_localstack=0
endpoint_url="${LOCALSTACK_ENDPOINT:-}"
terraform_endpoint="${LOCALSTACK_TERRAFORM_ENDPOINT:-}"

# Git for Windows rewrites Docker's Linux-side paths unless argument conversion is disabled.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

terraform_smoke() {
    case "$(uname -s)" in
        MINGW*|MSYS*|CYGWIN*)
            docker run --rm \
                -e AWS_ACCESS_KEY_ID=test \
                -e AWS_SECRET_ACCESS_KEY=test \
                -e AWS_DEFAULT_REGION=us-east-1 \
                -e AWS_REGION=us-east-1 \
                -e AWS_EC2_METADATA_DISABLED=true \
                -e TF_VAR_endpoint_url="$terraform_endpoint" \
                -e TF_VAR_run_id="$run_id" \
                -v "$repo_root:/work" \
                -w "/work/$work_rel" \
                "$terraform_image" "$@"
            ;;
        *)
            docker run --rm \
                --add-host host.docker.internal:host-gateway \
                -e AWS_ACCESS_KEY_ID=test \
                -e AWS_SECRET_ACCESS_KEY=test \
                -e AWS_DEFAULT_REGION=us-east-1 \
                -e AWS_REGION=us-east-1 \
                -e AWS_EC2_METADATA_DISABLED=true \
                -e TF_VAR_endpoint_url="$terraform_endpoint" \
                -e TF_VAR_run_id="$run_id" \
                -v "$repo_root:/work" \
                -w "/work/$work_rel" \
                "$terraform_image" "$@"
            ;;
    esac
}

cleanup() {
    status=$?
    cleanup_failed=0
    trap - EXIT HUP INT TERM
    if [ "$terraform_applied" -eq 1 ]; then
        if ! terraform_smoke destroy -auto-approve -input=false >/dev/null 2>&1; then
            echo "LocalStack smoke Terraform cleanup failed; state retained at $work_dir" >&2
            cleanup_failed=1
        fi
    fi
    if [ "$started_localstack" -eq 1 ]; then
        docker rm -f "$container_name" >/dev/null 2>&1 || true
    fi
    if [ "$cleanup_failed" -eq 0 ]; then
        rm -rf "$work_dir"
    elif [ "$status" -eq 0 ]; then
        status=1
    fi
    exit "$status"
}
trap cleanup EXIT HUP INT TERM

command -v docker >/dev/null 2>&1 || {
    echo "docker is required for the LocalStack AWS smoke" >&2
    exit 1
}
command -v uv >/dev/null 2>&1 || {
    echo "uv is required for the LocalStack AWS smoke" >&2
    exit 1
}
command -v curl >/dev/null 2>&1 || {
    echo "curl is required for the LocalStack AWS smoke" >&2
    exit 1
}

mkdir -p "$work_dir"
cp "$fixture_dir/main.tf" "$work_dir/main.tf"

if [ -z "$endpoint_url" ]; then
    docker run --detach --rm \
        --name "$container_name" \
        --publish 4566 \
        -e SERVICES=secretsmanager,ssm \
        "$localstack_image" >/dev/null
    started_localstack=1

    host_port="$(docker port "$container_name" 4566/tcp | awk -F: 'NR == 1 { print $NF }')"
    [ -n "$host_port" ] || {
        echo "could not determine LocalStack host port" >&2
        exit 1
    }
    endpoint_url="http://127.0.0.1:$host_port"
fi

if [ -z "$terraform_endpoint" ]; then
    terraform_endpoint="$(printf '%s' "$endpoint_url" | \
        sed -e 's#://127\.0\.0\.1:#://host.docker.internal:#' \
            -e 's#://localhost:#://host.docker.internal:#')"
fi

attempt=1
while ! curl --fail --silent --show-error "$endpoint_url/_localstack/health" >/dev/null 2>&1; do
    if [ "$attempt" -ge "$attempts" ]; then
        echo "LocalStack did not become ready" >&2
        docker logs "$container_name" >&2 || true
        exit 1
    fi
    attempt=$((attempt + 1))
    sleep 1
done

terraform_smoke init -input=false >/dev/null
terraform_applied=1
terraform_smoke apply -auto-approve -input=false >/dev/null

secret_name="$(terraform_smoke output -raw secret_name)"
parameter_name="$(terraform_smoke output -raw parameter_name)"
[ -n "$secret_name" ]
[ -n "$parameter_name" ]

export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1
export AWS_REGION=us-east-1
export AWS_EC2_METADATA_DISABLED=true
export AWS_ENDPOINT_URL="$endpoint_url"
export AWS_ENDPOINT_URL_SECRETS_MANAGER="$endpoint_url"
export AWS_ENDPOINT_URL_SSM="$endpoint_url"
export SKILLWRIGHT_LOCALSTACK_SECRET_NAME="$secret_name"
export SKILLWRIGHT_LOCALSTACK_PARAMETER_NAME="$parameter_name"

cd "$repo_root"
uv run python - <<'PY'
import asyncio
import os

import boto3

from skillwright_mcp.secrets import SecretResolver


async def main() -> None:
    secret_name = os.environ["SKILLWRIGHT_LOCALSTACK_SECRET_NAME"]
    parameter_name = os.environ["SKILLWRIGHT_LOCALSTACK_PARAMETER_NAME"]
    region = os.environ["AWS_REGION"]
    resolver = SecretResolver(aws_region=region, aws_timeout_seconds=5.0)

    assert await resolver.resolve(secret_name, provider="aws-secrets-manager") == (
        "localstack-secret-v1"
    )
    assert await resolver.resolve(parameter_name, provider="aws-ssm") == (
        "localstack-parameter-v1"
    )

    secrets = boto3.client("secretsmanager", region_name=region)
    ssm = boto3.client("ssm", region_name=region)
    secrets.put_secret_value(SecretId=secret_name, SecretString="localstack-secret-v2")
    ssm.put_parameter(
        Name=parameter_name,
        Type="SecureString",
        Value="localstack-parameter-v2",
        Overwrite=True,
    )

    assert await resolver.resolve(secret_name, provider="aws-secrets-manager") == (
        "localstack-secret-v2"
    )
    assert await resolver.resolve(parameter_name, provider="aws-ssm") == (
        "localstack-parameter-v2"
    )


asyncio.run(main())
PY

printf 'LocalStack AWS smoke passed (Terraform + boto3 Secrets Manager/SSM).\n'
