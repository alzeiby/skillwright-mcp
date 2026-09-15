mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
      arn        = "arn:aws:iam::123456789012:user/terraform-test"
      user_id    = "AIDATEST"
    }
  }

  mock_data "aws_ssm_parameter" {
    defaults = {
      arn   = "arn:aws:ssm:us-east-1:123456789012:parameter/skillwright-test/migration-image-tag"
      name  = "/skillwright-test/migration-image-tag"
      type  = "String"
      value = "release-sha"
    }
  }

  mock_resource "aws_ecr_repository" {
    defaults = {
      arn            = "arn:aws:ecr:us-east-1:123456789012:repository/skillwright-test"
      registry_id    = "123456789012"
      repository_url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/skillwright-test"
    }
  }

  mock_resource "aws_db_instance" {
    defaults = {
      address  = "skillwright-test.cluster.local"
      endpoint = "skillwright-test.cluster.local:5432"
      port     = 5432
      master_user_secret = [{
        kms_key_id    = "arn:aws:kms:us-east-1:123456789012:key/test"
        secret_arn    = "arn:aws:secretsmanager:us-east-1:123456789012:secret:rds-master-test"
        secret_status = "active"
      }]
    }
  }

  mock_resource "aws_secretsmanager_secret" {
    defaults = {
      arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:skillwright-test/database-runtime"
    }
  }

  mock_resource "aws_ssm_parameter" {
    defaults = {
      arn = "arn:aws:ssm:us-east-1:123456789012:parameter/skillwright-test/migration-image-tag"
    }
  }

  mock_resource "aws_elasticache_replication_group" {
    defaults = {
      primary_endpoint_address = "redis.skillwright-test.local"
    }
  }

  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::123456789012:role/mock-role"
    }
  }

  mock_resource "aws_iam_policy" {
    defaults = {
      arn = "arn:aws:iam::123456789012:policy/mock-policy"
    }
  }

  mock_resource "aws_lb" {
    defaults = {
      arn      = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/mock/1234"
      dns_name = "skillwright-test.elb.local"
    }
  }

  mock_resource "aws_lb_target_group" {
    defaults = {
      arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/mock/1234"
    }
  }

  mock_resource "aws_lb_listener" {
    defaults = {
      arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:listener/app/mock/1234/5678"
    }
  }

  mock_resource "aws_ecs_task_definition" {
    defaults = {
      arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/mock:1"
    }
  }
}

override_resource {
  target = aws_iam_role.api_task
  values = {
    arn = "arn:aws:iam::123456789012:role/skillwright-test-api-task"
    id  = "skillwright-test-api-task"
  }
}

override_resource {
  target = aws_iam_role.runtime_task
  values = {
    arn = "arn:aws:iam::123456789012:role/skillwright-test-runtime-task"
    id  = "skillwright-test-runtime-task"
  }
}

override_resource {
  target = aws_iam_role.mcp_task
  values = {
    arn = "arn:aws:iam::123456789012:role/skillwright-test-mcp-task"
    id  = "skillwright-test-mcp-task"
  }
}

override_resource {
  target = aws_iam_role.migration_task
  values = {
    arn = "arn:aws:iam::123456789012:role/skillwright-test-migration-task"
    id  = "skillwright-test-migration-task"
  }
}

variables {
  aws_region                   = "us-east-1"
  name_prefix                  = "skillwright-test"
  vpc_id                       = "vpc-0123abcd"
  private_subnet_ids           = ["subnet-0123abcd", "subnet-0456def0"]
  public_subnet_ids            = ["subnet-0789abcd", "subnet-0abc1234"]
  certificate_arn              = "arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555"
  external_base_url            = "https://skillwright.example.com"
  image_tag                    = "release-sha"
  services_enabled             = false
  database_multi_az            = false
  database_deletion_protection = false
  database_skip_final_snapshot = true
  redis_replica_count          = 0
}

run "services_disabled_initial_plan" {
  command = plan

  assert {
    condition     = aws_ecs_service.api.desired_count == 0 && aws_ecs_service.mcp.desired_count == 0 && aws_ecs_service.worker.desired_count == 0
    error_message = "the initial services_enabled=false plan must not start long-lived services"
  }

  assert {
    condition     = length(aws_appautoscaling_target.api) == 0 && length(aws_appautoscaling_target.mcp) == 0 && length(aws_appautoscaling_target.worker) == 0
    error_message = "autoscaling targets must stay absent while services are disabled"
  }
}

run "production_invariants" {
  command = apply

  assert {
    condition     = aws_ecr_repository.skillwright.image_tag_mutability == "IMMUTABLE"
    error_message = "production ECR tags must remain immutable"
  }

  assert {
    condition     = aws_db_instance.postgres.storage_encrypted && !aws_db_instance.postgres.publicly_accessible && aws_db_instance.postgres.manage_master_user_password
    error_message = "RDS must stay encrypted, private, and use an AWS-managed master password"
  }

  assert {
    condition     = aws_db_instance.postgres.username == var.database_admin_user && var.database_admin_user != var.database_user
    error_message = "the RDS administrator must stay separate from the runtime database role"
  }

  assert {
    condition     = aws_elasticache_replication_group.redis.at_rest_encryption_enabled && aws_elasticache_replication_group.redis.transit_encryption_enabled
    error_message = "ElastiCache must keep at-rest and in-transit encryption enabled"
  }

  assert {
    condition     = aws_lb_target_group.api.health_check[0].path == "/health/ready" && aws_lb_target_group.api.health_check[0].matcher == "200"
    error_message = "API ALB health must use the dependency-aware readiness endpoint"
  }

  assert {
    condition     = aws_lb_target_group.mcp.health_check[0].path == "/health/ready" && aws_lb_target_group.mcp.health_check[0].matcher == "200"
    error_message = "MCP ALB health must use the dependency-aware readiness endpoint"
  }

  assert {
    condition     = aws_lb_target_group.mcp.stickiness[0].enabled && aws_lb_target_group.mcp.deregistration_delay >= 300
    error_message = "stateful MCP routing must retain stickiness and a drain window"
  }

  assert {
    condition = (
      aws_iam_role_policy.mcp_task_protection.role == aws_iam_role.mcp_task.id &&
      toset(jsondecode(aws_iam_role_policy.mcp_task_protection.policy).Statement[0].Action) == toset([
        "ecs:GetTaskProtection",
        "ecs:UpdateTaskProtection",
      ]) &&
      jsondecode(aws_iam_role_policy.mcp_task_protection.policy).Statement[0].Resource == "*"
    )
    error_message = "the MCP task role must retain ECS task-protection permissions for connection draining"
  }

  assert {
    condition = contains(
      [for item in one([for container in jsondecode(aws_ecs_task_definition.worker.container_definitions) : container if container.name == "skillwright-worker"]).environment : "${item.name}=${item.value}"],
      "SKILLWRIGHT_DATABASE_SSL=verify-full",
    )
    error_message = "worker tasks must require verify-full PostgreSQL TLS"
  }

  assert {
    condition = contains(
      [for item in one([for container in jsondecode(aws_ecs_task_definition.worker.container_definitions) : container if container.name == "skillwright-worker"]).environment : item.name],
      "SKILLWRIGHT_REDIS_URL",
      ) && startswith(
      one([for item in one([for container in jsondecode(aws_ecs_task_definition.worker.container_definitions) : container if container.name == "skillwright-worker"]).environment : item.value if item.name == "SKILLWRIGHT_REDIS_URL"]),
      "rediss://",
    )
    error_message = "worker tasks must use TLS Redis URLs"
  }

  assert {
    condition     = one([for item in one([for container in jsondecode(aws_ecs_task_definition.migration.container_definitions) : container if container.name == "skillwright-migration"]).environment : item.value if item.name == "SKILLWRIGHT_DATABASE_USER"]) == var.database_admin_user
    error_message = "the migration task must use the administrator database role"
  }

  assert {
    condition = alltrue([
      for task in [
        aws_ecs_task_definition.api.container_definitions,
        aws_ecs_task_definition.mcp.container_definitions,
        aws_ecs_task_definition.worker.container_definitions,
        ] : one(flatten([
          for container in jsondecode(task) : [
            for secret in try(container.secrets, []) : secret.valueFrom if secret.name == "SKILLWRIGHT_DATABASE_PASSWORD"
          ]
      ])) == "${aws_secretsmanager_secret.database_runtime.arn}:password::"
    ])
    error_message = "all long-lived runtime tasks must receive the least-privilege runtime database secret"
  }

  assert {
    condition = alltrue([
      for task in [
        aws_ecs_task_definition.api.container_definitions,
        aws_ecs_task_definition.mcp.container_definitions,
        aws_ecs_task_definition.worker.container_definitions,
        ] : alltrue(flatten([
          for container in jsondecode(task) : [
            for secret in try(container.secrets, []) : !strcontains(secret.valueFrom, "rds-master-test")
          ]
      ]))
    ])
    error_message = "long-lived runtime tasks must never receive the RDS master secret"
  }

  assert {
    condition = one(flatten([
      for container in jsondecode(aws_ecs_task_definition.migration.container_definitions) : [
        for secret in try(container.secrets, []) : secret.valueFrom if secret.name == "SKILLWRIGHT_DATABASE_PASSWORD"
      ]
    ])) == "${aws_db_instance.postgres.master_user_secret[0].secret_arn}:password::"
    error_message = "the migration task must receive the RDS master secret for schema/bootstrap work"
  }

  assert {
    condition = (
      length(toset([
        aws_ecs_task_definition.api.task_role_arn,
        aws_ecs_task_definition.worker.task_role_arn,
        aws_ecs_task_definition.mcp.task_role_arn,
        aws_ecs_task_definition.migration.task_role_arn,
      ])) == 4 &&
      aws_ecs_task_definition.api.task_role_arn == aws_iam_role.api_task.arn &&
      aws_ecs_task_definition.worker.task_role_arn == aws_iam_role.runtime_task.arn &&
      aws_ecs_task_definition.mcp.task_role_arn == aws_iam_role.mcp_task.arn &&
      aws_ecs_task_definition.migration.task_role_arn == aws_iam_role.migration_task.arn
    )
    error_message = "API, worker runtime, MCP, and migration task definitions must retain separate dedicated IAM roles"
  }

  assert {
    condition     = aws_ecs_service.api.desired_count == 0 && aws_ecs_service.mcp.desired_count == 0 && aws_ecs_service.worker.desired_count == 0
    error_message = "services_enabled=false must register infrastructure without starting long-lived services"
  }
}

run "migration_candidate_does_not_promote_services" {
  command = apply

  variables {
    migration_image_tag = "candidate-sha"
  }

  assert {
    condition = endswith(
      one([for container in jsondecode(aws_ecs_task_definition.migration.container_definitions) : container.image if container.name == "skillwright-migration"]),
      ":candidate-sha",
    )
    error_message = "migration_image_tag must update only the migration task candidate image"
  }

  assert {
    condition = endswith(
      one([for container in jsondecode(aws_ecs_task_definition.api.container_definitions) : container.image if container.name == "skillwright-api"]),
      ":release-sha",
    )
    error_message = "candidate migrations must not implicitly promote the API image"
  }
}

run "services_start_only_after_matching_migration_marker" {
  command = apply

  variables {
    services_enabled = true
  }

  assert {
    condition     = aws_ecs_service.api.desired_count == var.api_desired_count && aws_ecs_service.mcp.desired_count == var.mcp_desired_count && aws_ecs_service.worker.desired_count == var.worker_desired_count
    error_message = "a matching migration marker must permit the configured service counts"
  }
}

run "stale_migration_marker_blocks_rollout" {
  command = plan

  variables {
    services_enabled = true
  }

  override_data {
    target = data.aws_ssm_parameter.migration_marker
    values = {
      value = "older-release"
    }
  }

  expect_failures = [
    aws_ecs_service.api,
    aws_ecs_service.mcp,
    aws_ecs_service.worker,
  ]
}

run "latest_image_tag_is_rejected" {
  command = plan

  variables {
    image_tag = "latest"
  }

  expect_failures = [var.image_tag]
}

run "latest_migration_image_tag_is_rejected" {
  command = plan

  variables {
    migration_image_tag = "latest"
  }

  expect_failures = [var.migration_image_tag]
}

run "invalid_api_fargate_size_is_rejected" {
  command = plan

  variables {
    api_cpu    = 256
    api_memory = 4096
  }

  expect_failures = [check.fargate_task_sizes]
}

run "invalid_mcp_fargate_size_is_rejected" {
  command = plan

  variables {
    mcp_cpu    = 256
    mcp_memory = 4096
  }

  expect_failures = [check.fargate_task_sizes]
}

run "invalid_worker_fargate_size_is_rejected" {
  command = plan

  variables {
    worker_cpu    = 512
    worker_memory = 8192
  }

  expect_failures = [check.fargate_task_sizes]
}

run "invalid_migration_fargate_size_is_rejected" {
  command = plan

  variables {
    migration_cpu    = 1024
    migration_memory = 1024
  }

  expect_failures = [check.fargate_task_sizes]
}

run "database_roles_must_be_distinct" {
  command = plan

  variables {
    database_admin_user = "skillwright"
  }

  expect_failures = [check.database_roles_are_distinct]
}
