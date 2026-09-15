data "aws_caller_identity" "current" {}

locals {
  image           = "${aws_ecr_repository.skillwright.repository_url}:${var.image_tag}"
  migration_tag   = coalesce(var.migration_image_tag, var.image_tag)
  migration_image = "${aws_ecr_repository.skillwright.repository_url}:${local.migration_tag}"

  ecs_task_assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = data.aws_caller_identity.current.account_id
        }
        ArnLike = {
          "aws:SourceArn" = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"
        }
      }
    }]
  })

  common_environment = concat(
    [
      { name = "AWS_REGION", value = var.aws_region },
      { name = "AWS_DEFAULT_REGION", value = var.aws_region },
      { name = "SKILLWRIGHT_AWS_REGION", value = var.aws_region },
      { name = "SKILLWRIGHT_DATABASE_HOST", value = aws_db_instance.postgres.address },
      { name = "SKILLWRIGHT_DATABASE_PORT", value = tostring(aws_db_instance.postgres.port) },
      { name = "SKILLWRIGHT_DATABASE_NAME", value = var.database_name },
      { name = "SKILLWRIGHT_DATABASE_USER", value = var.database_user },
      { name = "SKILLWRIGHT_DATABASE_SSL", value = "verify-full" },
      { name = "SKILLWRIGHT_DATABASE_SSL_ROOT_CERT", value = "/etc/ssl/certs/aws-rds-global-bundle.pem" },
      { name = "SKILLWRIGHT_REDIS_URL", value = "rediss://${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0" },
      { name = "SKILLWRIGHT_EXECUTION_BACKEND", value = "redis" },
      { name = "SKILLWRIGHT_ALLOW_UNAUTHENTICATED_LOCAL", value = "false" },
      { name = "SKILLWRIGHT_AUTH_ISSUER_URL", value = var.external_base_url },
      { name = "SKILLWRIGHT_OTEL_ENABLED", value = "true" },
      { name = "SKILLWRIGHT_OTEL_EXPORTER_OTLP_ENDPOINT", value = "http://127.0.0.1:4318" },
    ],
    var.bootstrap_admin_principal == null ? [] : [
      { name = "SKILLWRIGHT_BOOTSTRAP_ADMIN_PRINCIPAL", value = var.bootstrap_admin_principal },
    ],
  )

  runtime_database_secrets = [
    {
      name      = "SKILLWRIGHT_DATABASE_PASSWORD"
      valueFrom = "${aws_secretsmanager_secret.database_runtime.arn}:password::"
    }
  ]

  migration_database_secrets = [
    {
      name      = "SKILLWRIGHT_DATABASE_PASSWORD"
      valueFrom = "${aws_db_instance.postgres.master_user_secret[0].secret_arn}:password::"
    }
  ]

  control_service_secrets = concat(
    local.runtime_database_secrets,
    [for name, arn in var.service_secret_arns : { name = name, valueFrom = arn }],
  )

  migration_environment = concat(
    [for item in local.common_environment : item if item.name != "SKILLWRIGHT_DATABASE_USER"],
    [
      { name = "SKILLWRIGHT_DATABASE_USER", value = var.database_admin_user },
      { name = "SKILLWRIGHT_MIGRATION_RUNTIME_DATABASE_USER", value = var.database_user },
      { name = "SKILLWRIGHT_MIGRATION_RUNTIME_DATABASE_SECRET_ARN", value = aws_secretsmanager_secret.database_runtime.arn },
      { name = "SKILLWRIGHT_MIGRATION_MARKER_PARAMETER", value = aws_ssm_parameter.migration_marker.name },
      { name = "SKILLWRIGHT_RELEASE_IMAGE_TAG", value = local.migration_tag },
    ],
  )

  adot_config = <<-YAML
    receivers:
      otlp:
        protocols:
          http:
            endpoint: 0.0.0.0:4318
    processors:
      memory_limiter:
        check_interval: 1s
        limit_mib: 192
      batch: {}
    exporters:
      awsxray: {}
      awsemf:
        namespace: Skillwright
        log_group_name: ${aws_cloudwatch_log_group.adot.name}
        dimension_rollup_option: NoDimensionRollup
    service:
      pipelines:
        traces:
          receivers: [otlp]
          processors: [memory_limiter, batch]
          exporters: [awsxray]
        metrics:
          receivers: [otlp]
          processors: [memory_limiter, batch]
          exporters: [awsemf]
  YAML

  adot_container = {
    name      = "adot"
    image     = var.adot_collector_image
    essential = false
    command   = ["--config=env:AOT_CONFIG_CONTENT"]
    environment = [
      { name = "AOT_CONFIG_CONTENT", value = local.adot_config },
      { name = "AWS_REGION", value = var.aws_region },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.adot.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "collector"
      }
    }
  }

  fargate_memory_by_cpu = {
    "256"   = [512, 1024, 2048]
    "512"   = [1024, 2048, 3072, 4096]
    "1024"  = [2048, 3072, 4096, 5120, 6144, 7168, 8192]
    "2048"  = [for memory in range(4096, 16385, 1024) : memory]
    "4096"  = [for memory in range(8192, 30721, 1024) : memory]
    "8192"  = [for memory in range(16384, 61441, 4096) : memory]
    "16384" = [for memory in range(32768, 122881, 8192) : memory]
  }
}

check "fargate_task_sizes" {
  assert {
    condition = alltrue([
      contains(lookup(local.fargate_memory_by_cpu, tostring(var.api_cpu), []), var.api_memory),
      contains(lookup(local.fargate_memory_by_cpu, tostring(var.mcp_cpu), []), var.mcp_memory),
      contains(lookup(local.fargate_memory_by_cpu, tostring(var.worker_cpu), []), var.worker_memory),
      contains(lookup(local.fargate_memory_by_cpu, tostring(var.migration_cpu), []), var.migration_memory),
    ])
    error_message = "API, MCP, worker, and migration CPU/memory values must be valid AWS Fargate combinations."
  }
}

check "database_roles_are_distinct" {
  assert {
    condition     = var.database_user != var.database_admin_user
    error_message = "database_user must be distinct from database_admin_user so runtime tasks never use the RDS master role."
  }
}

resource "aws_ecr_repository" "skillwright" {
  name                 = var.name_prefix
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "skillwright" {
  repository = aws_ecr_repository.skillwright.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Retain the most recent 50 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 50
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_ecs_cluster" "skillwright" {
  name = var.name_prefix

  setting {
    name  = "containerInsights"
    value = "enhanced"
  }
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/skillwright/${var.name_prefix}/api"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "mcp" {
  name              = "/skillwright/${var.name_prefix}/mcp"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/skillwright/${var.name_prefix}/worker"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "migration" {
  name              = "/skillwright/${var.name_prefix}/migration"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "adot" {
  name              = "/skillwright/${var.name_prefix}/adot"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "ecs_execution" {
  name               = "${var.name_prefix}-ecs-execution"
  assume_role_policy = local.ecs_task_assume_role_policy
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name = "service-secret-injection"
  role = aws_iam_role.ecs_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [{
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = concat(
          [
            aws_db_instance.postgres.master_user_secret[0].secret_arn,
            aws_secretsmanager_secret.database_runtime.arn,
          ],
          values(var.service_secret_arns),
        )
      }],
      length(var.service_secret_kms_key_arns) > 0 ? [{
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = tolist(var.service_secret_kms_key_arns)
      }] : [],
    )
  })
}

resource "aws_iam_role" "api_task" {
  name               = "${var.name_prefix}-api-task"
  assume_role_policy = local.ecs_task_assume_role_policy
}

resource "aws_iam_role" "runtime_task" {
  name               = "${var.name_prefix}-runtime-task"
  assume_role_policy = local.ecs_task_assume_role_policy
}

resource "aws_iam_role" "mcp_task" {
  name               = "${var.name_prefix}-mcp-task"
  assume_role_policy = local.ecs_task_assume_role_policy
}

resource "aws_iam_role" "migration_task" {
  name               = "${var.name_prefix}-migration-task"
  assume_role_policy = local.ecs_task_assume_role_policy
}

resource "aws_iam_role_policy" "migration_bootstrap" {
  name = "database-runtime-bootstrap"
  role = aws_iam_role.migration_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:PutSecretValue",
        ]
        Resource = aws_secretsmanager_secret.database_runtime.arn
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:PutParameter"]
        Resource = aws_ssm_parameter.migration_marker.arn
      },
    ]
  })
}

resource "aws_iam_policy" "workflow_secrets" {
  count = length(var.workflow_secrets_manager_arns) + length(var.workflow_ssm_parameter_arns) + length(var.workflow_kms_key_arns) > 0 ? 1 : 0
  name  = "workflow-secret-resolution"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      length(var.workflow_secrets_manager_arns) > 0 ? [{
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = tolist(var.workflow_secrets_manager_arns)
      }] : [],
      length(var.workflow_ssm_parameter_arns) > 0 ? [{
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = tolist(var.workflow_ssm_parameter_arns)
      }] : [],
      length(var.workflow_kms_key_arns) > 0 ? [{
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = tolist(var.workflow_kms_key_arns)
      }] : [],
    )
  })
}

resource "aws_iam_role_policy_attachment" "runtime_workflow_secrets" {
  count      = length(aws_iam_policy.workflow_secrets)
  role       = aws_iam_role.runtime_task.name
  policy_arn = aws_iam_policy.workflow_secrets[0].arn
}

resource "aws_iam_role_policy_attachment" "mcp_workflow_secrets" {
  count      = length(aws_iam_policy.workflow_secrets)
  role       = aws_iam_role.mcp_task.name
  policy_arn = aws_iam_policy.workflow_secrets[0].arn
}

resource "aws_iam_role_policy" "mcp_task_protection" {
  name = "self-task-scale-in-protection"
  role = aws_iam_role.mcp_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ecs:GetTaskProtection",
        "ecs:UpdateTaskProtection",
      ]
      Resource = "*"
    }]
  })
}

resource "aws_iam_policy" "adot" {
  name = "${var.name_prefix}-adot-export"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords",
          "xray:GetSamplingRules",
          "xray:GetSamplingTargets",
          "xray:GetSamplingStatisticSummaries",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricData",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams",
          "logs:DescribeLogGroups",
        ]
        Resource = "*"
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "api_adot" {
  role       = aws_iam_role.api_task.name
  policy_arn = aws_iam_policy.adot.arn
}

resource "aws_iam_role_policy_attachment" "runtime_adot" {
  role       = aws_iam_role.runtime_task.name
  policy_arn = aws_iam_policy.adot.arn
}

resource "aws_iam_role_policy_attachment" "mcp_adot" {
  role       = aws_iam_role.mcp_task.name
  policy_arn = aws_iam_policy.adot.arn
}

resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-alb"
  description = "HTTPS ingress to Skillwright"
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.alb_ingress_cidrs
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "tasks" {
  name        = "${var.name_prefix}-tasks"
  description = "Skillwright Fargate tasks"
  vpc_id      = var.vpc_id

  ingress {
    description     = "API from ALB"
    from_port       = 8767
    to_port         = 8767
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  ingress {
    description     = "MCP from ALB"
    from_port       = 8766
    to_port         = 8766
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "database" {
  name        = "${var.name_prefix}-database"
  description = "PostgreSQL from Skillwright tasks"
  vpc_id      = var.vpc_id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.tasks.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "redis" {
  name        = "${var.name_prefix}-redis"
  description = "Redis from Skillwright tasks"
  vpc_id      = var.vpc_id

  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.tasks.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_db_subnet_group" "postgres" {
  name       = "${var.name_prefix}-postgres"
  subnet_ids = var.private_subnet_ids
}

resource "aws_db_instance" "postgres" {
  identifier                   = "${var.name_prefix}-postgres"
  engine                       = "postgres"
  engine_version               = var.postgres_engine_version
  instance_class               = var.database_instance_class
  allocated_storage            = var.database_allocated_storage_gib
  max_allocated_storage        = var.database_max_allocated_storage_gib
  storage_type                 = "gp3"
  storage_encrypted            = true
  db_name                      = var.database_name
  username                     = var.database_admin_user
  manage_master_user_password  = true
  port                         = 5432
  multi_az                     = var.database_multi_az
  publicly_accessible          = false
  db_subnet_group_name         = aws_db_subnet_group.postgres.name
  vpc_security_group_ids       = [aws_security_group.database.id]
  backup_retention_period      = var.database_backup_retention_days
  copy_tags_to_snapshot        = true
  deletion_protection          = var.database_deletion_protection
  skip_final_snapshot          = var.database_skip_final_snapshot
  final_snapshot_identifier    = var.database_skip_final_snapshot ? null : "${var.name_prefix}-postgres-final"
  performance_insights_enabled = true
  auto_minor_version_upgrade   = true
  apply_immediately            = false
}

resource "aws_secretsmanager_secret" "database_runtime" {
  name                    = "${var.name_prefix}/database-runtime"
  description             = "Least-privilege Skillwright runtime PostgreSQL credentials initialized by the migration task."
  recovery_window_in_days = 7
}

resource "aws_ssm_parameter" "migration_marker" {
  name        = "/${var.name_prefix}/migration-image-tag"
  description = "Last Skillwright image tag whose database migration completed successfully."
  type        = "String"
  value       = "unmigrated"

  lifecycle {
    ignore_changes = [value]
  }
}

data "aws_ssm_parameter" "migration_marker" {
  name       = aws_ssm_parameter.migration_marker.name
  depends_on = [aws_ssm_parameter.migration_marker]
}

resource "aws_elasticache_subnet_group" "redis" {
  name       = "${var.name_prefix}-redis"
  subnet_ids = var.private_subnet_ids
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id       = "${var.name_prefix}-redis"
  description                = "Skillwright deterministic run queue"
  engine                     = "redis"
  engine_version             = var.redis_engine_version
  node_type                  = var.redis_node_type
  port                       = 6379
  num_cache_clusters         = var.redis_replica_count + 1
  automatic_failover_enabled = var.redis_replica_count > 0
  multi_az_enabled           = var.redis_replica_count > 0
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  subnet_group_name          = aws_elasticache_subnet_group.redis.name
  security_group_ids         = [aws_security_group.redis.id]
  snapshot_retention_limit   = var.redis_snapshot_retention_days
  auto_minor_version_upgrade = true
  apply_immediately          = false
}

resource "aws_lb" "skillwright" {
  name                       = substr("${var.name_prefix}-alb", 0, 32)
  internal                   = var.alb_internal
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.alb.id]
  subnets                    = var.public_subnet_ids
  enable_deletion_protection = var.alb_deletion_protection
  idle_timeout               = 3600
}

resource "aws_lb_target_group" "api" {
  name        = substr("${var.name_prefix}-api", 0, 32)
  port        = 8767
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id

  health_check {
    path                = "/health/ready"
    matcher             = "200"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 15
  }
}

resource "aws_lb_target_group" "mcp" {
  name                 = substr("${var.name_prefix}-mcp", 0, 32)
  port                 = 8766
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = var.vpc_id
  deregistration_delay = 300

  stickiness {
    enabled         = true
    type            = "lb_cookie"
    cookie_duration = 1800
  }

  health_check {
    path                = "/health/ready"
    matcher             = "200"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 15
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.skillwright.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_lb_listener_rule" "mcp" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.mcp.arn
  }

  condition {
    path_pattern {
      values = ["/mcp", "/mcp/*"]
    }
  }
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${var.name_prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.api_cpu)
  memory                   = tostring(var.api_memory)
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.api_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([
    {
      name         = "skillwright-api"
      image        = local.image
      essential    = true
      command      = ["api"]
      portMappings = [{ containerPort = 8767, hostPort = 8767, protocol = "tcp" }]
      environment = concat(local.common_environment, [
        { name = "SKILLWRIGHT_OTEL_SERVICE_NAME", value = "skillwright-api" },
      ])
      secrets   = local.control_service_secrets
      dependsOn = [{ containerName = "adot", condition = "START" }]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.api.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "api"
        }
      }
    },
    local.adot_container,
  ])
}

resource "aws_ecs_task_definition" "mcp" {
  family                   = "${var.name_prefix}-mcp"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.mcp_cpu)
  memory                   = tostring(var.mcp_memory)
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.mcp_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([
    {
      name         = "skillwright-mcp"
      image        = local.image
      essential    = true
      command      = ["mcp"]
      portMappings = [{ containerPort = 8766, hostPort = 8766, protocol = "tcp" }]
      environment = concat(local.common_environment, [
        { name = "SKILLWRIGHT_OTEL_SERVICE_NAME", value = "skillwright-mcp" },
        { name = "SKILLWRIGHT_MCP_RESOURCE_SERVER_URL", value = "${var.external_base_url}/mcp" },
      ])
      secrets   = local.control_service_secrets
      dependsOn = [{ containerName = "adot", condition = "START" }]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.mcp.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "mcp"
        }
      }
    },
    local.adot_container,
  ])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.name_prefix}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.worker_cpu)
  memory                   = tostring(var.worker_memory)
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.runtime_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([
    {
      name      = "skillwright-worker"
      image     = local.image
      essential = true
      command   = ["worker"]
      environment = concat(local.common_environment, [
        { name = "SKILLWRIGHT_OTEL_SERVICE_NAME", value = "skillwright-worker" },
        { name = "SKILLWRIGHT_WORKER_CONCURRENCY", value = tostring(var.worker_concurrency) },
      ])
      secrets   = local.runtime_database_secrets
      dependsOn = [{ containerName = "adot", condition = "START" }]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.worker.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "worker"
        }
      }
    },
    local.adot_container,
  ])
}

resource "aws_ecs_task_definition" "migration" {
  family                   = "${var.name_prefix}-migration"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.migration_cpu)
  memory                   = tostring(var.migration_memory)
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.migration_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([{
    name        = "skillwright-migration"
    image       = local.migration_image
    essential   = true
    command     = ["migrate"]
    environment = local.migration_environment
    secrets     = local.migration_database_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.migration.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "migration"
      }
    }
  }])
}

resource "aws_ecs_service" "api" {
  name             = "${var.name_prefix}-api"
  cluster          = aws_ecs_cluster.skillwright.id
  task_definition  = aws_ecs_task_definition.api.arn
  desired_count    = var.services_enabled ? var.api_desired_count : 0
  launch_type      = "FARGATE"
  platform_version = "1.4.0"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "skillwright-api"
    container_port   = 8767
  }

  health_check_grace_period_seconds = 60

  lifecycle {
    precondition {
      condition     = !var.services_enabled || data.aws_ssm_parameter.migration_marker.value == var.image_tag
      error_message = "image_tag must match the successful migration marker before API service rollout. Run the migration task for this image first."
    }
  }
}

resource "aws_ecs_service" "mcp" {
  name             = "${var.name_prefix}-mcp"
  cluster          = aws_ecs_cluster.skillwright.id
  task_definition  = aws_ecs_task_definition.mcp.arn
  desired_count    = var.services_enabled ? var.mcp_desired_count : 0
  launch_type      = "FARGATE"
  platform_version = "1.4.0"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.mcp.arn
    container_name   = "skillwright-mcp"
    container_port   = 8766
  }

  health_check_grace_period_seconds = 90

  lifecycle {
    precondition {
      condition     = !var.services_enabled || data.aws_ssm_parameter.migration_marker.value == var.image_tag
      error_message = "image_tag must match the successful migration marker before MCP service rollout. Run the migration task for this image first."
    }
  }
}

resource "aws_ecs_service" "worker" {
  name             = "${var.name_prefix}-worker"
  cluster          = aws_ecs_cluster.skillwright.id
  task_definition  = aws_ecs_task_definition.worker.arn
  desired_count    = var.services_enabled ? var.worker_desired_count : 0
  launch_type      = "FARGATE"
  platform_version = "1.4.0"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle {
    precondition {
      condition     = !var.services_enabled || data.aws_ssm_parameter.migration_marker.value == var.image_tag
      error_message = "image_tag must match the successful migration marker before worker service rollout. Run the migration task for this image first."
    }
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }
}

resource "aws_appautoscaling_target" "api" {
  count              = var.enable_autoscaling && var.services_enabled ? 1 : 0
  max_capacity       = var.api_max_count
  min_capacity       = var.api_desired_count
  resource_id        = "service/${aws_ecs_cluster.skillwright.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_target" "mcp" {
  count              = var.enable_autoscaling && var.services_enabled ? 1 : 0
  max_capacity       = var.mcp_max_count
  min_capacity       = var.mcp_desired_count
  resource_id        = "service/${aws_ecs_cluster.skillwright.name}/${aws_ecs_service.mcp.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_target" "worker" {
  count              = var.enable_autoscaling && var.services_enabled ? 1 : 0
  max_capacity       = var.worker_max_count
  min_capacity       = var.worker_desired_count
  resource_id        = "service/${aws_ecs_cluster.skillwright.name}/${aws_ecs_service.worker.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "api_cpu" {
  count              = length(aws_appautoscaling_target.api)
  name               = "${var.name_prefix}-api-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.api[0].resource_id
  scalable_dimension = aws_appautoscaling_target.api[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.api[0].service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value = var.autoscaling_cpu_target
  }
}

resource "aws_appautoscaling_policy" "mcp_cpu" {
  count              = length(aws_appautoscaling_target.mcp)
  name               = "${var.name_prefix}-mcp-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.mcp[0].resource_id
  scalable_dimension = aws_appautoscaling_target.mcp[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.mcp[0].service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value = var.autoscaling_cpu_target
  }
}

resource "aws_appautoscaling_policy" "worker_cpu" {
  count              = length(aws_appautoscaling_target.worker)
  name               = "${var.name_prefix}-worker-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.worker[0].resource_id
  scalable_dimension = aws_appautoscaling_target.worker[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.worker[0].service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value = var.autoscaling_cpu_target
  }
}
