variable "aws_region" {
  description = "AWS region for all regional Skillwright resources."
  type        = string
}

variable "name_prefix" {
  description = "Prefix used for AWS resource names."
  type        = string
  default     = "skillwright"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be 3-32 lowercase alphanumeric/hyphen characters, beginning with a letter and ending alphanumeric."
  }
}

variable "tags" {
  description = "Additional tags applied through the AWS provider."
  type        = map(string)
  default     = {}
}

variable "vpc_id" {
  description = "Existing VPC in which Skillwright is deployed."
  type        = string

  validation {
    condition     = can(regex("^vpc-[0-9a-f]+$", var.vpc_id))
    error_message = "vpc_id must be an AWS VPC id."
  }
}

variable "private_subnet_ids" {
  description = "At least two private subnets for ECS tasks, RDS, and ElastiCache."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "private_subnet_ids must contain at least two subnets."
  }

  validation {
    condition     = alltrue([for subnet in var.private_subnet_ids : can(regex("^subnet-[0-9a-f]+$", subnet))])
    error_message = "private_subnet_ids must contain AWS subnet ids."
  }
}

variable "public_subnet_ids" {
  description = "At least two subnets for the application load balancer."
  type        = list(string)

  validation {
    condition     = length(var.public_subnet_ids) >= 2
    error_message = "public_subnet_ids must contain at least two subnets."
  }

  validation {
    condition     = alltrue([for subnet in var.public_subnet_ids : can(regex("^subnet-[0-9a-f]+$", subnet))])
    error_message = "public_subnet_ids must contain AWS subnet ids."
  }
}

variable "certificate_arn" {
  description = "ACM certificate ARN used by the HTTPS listener."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-zA-Z-]*:acm:[a-z0-9-]+:[0-9]{12}:certificate/[A-Za-z0-9-]+$", var.certificate_arn))
    error_message = "certificate_arn must be an ACM certificate ARN."
  }
}

variable "external_base_url" {
  description = "Canonical externally reachable HTTPS origin, e.g. https://skillwright.example.com."
  type        = string

  validation {
    condition     = can(regex("^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$", var.external_base_url))
    error_message = "external_base_url must be an https:// hostname origin without a path, query, fragment, or trailing slash."
  }
}

variable "alb_internal" {
  description = "Whether the ALB is internal instead of internet-facing."
  type        = bool
  default     = false
}

variable "alb_ingress_cidrs" {
  description = "IPv4 CIDRs allowed to reach the ALB HTTPS listener. Narrow this for private/corporate deployments."
  type        = list(string)
  default     = ["0.0.0.0/0"]

  validation {
    condition     = length(var.alb_ingress_cidrs) > 0
    error_message = "alb_ingress_cidrs must contain at least one CIDR."
  }
}

variable "alb_deletion_protection" {
  description = "Enable ALB deletion protection."
  type        = bool
  default     = true
}

variable "image_tag" {
  description = "Immutable image tag deployed from the managed ECR repository."
  type        = string

  validation {
    condition     = length(trimspace(var.image_tag)) > 0 && lower(var.image_tag) != "latest"
    error_message = "image_tag must be a non-empty immutable release/Git tag and cannot be latest."
  }
}

variable "migration_image_tag" {
  description = "Optional candidate image tag used only by the migration task. Set this before promoting image_tag on schema-changing releases."
  type        = string
  default     = null

  validation {
    condition = (
      var.migration_image_tag == null ||
      (length(trimspace(var.migration_image_tag)) > 0 && lower(var.migration_image_tag) != "latest")
    )
    error_message = "migration_image_tag must be null or a non-empty immutable release/Git tag and cannot be latest."
  }
}

variable "adot_collector_image" {
  description = "Digest-pinned AWS Distro for OpenTelemetry collector image."
  type        = string
  default     = "public.ecr.aws/aws-observability/aws-otel-collector@sha256:7968fb60db6a2390a47ba6a2df029745638486e285c9b2487da1b722d0855a3e"
}

variable "bootstrap_admin_principal" {
  description = "Optional initial principal key. Set for first authenticated startup, then remove after the principal exists."
  type        = string
  default     = null
}

variable "services_enabled" {
  description = "Keep false on first apply, run the migration task, then set true."
  type        = bool
  default     = false
}

variable "worker_concurrency" {
  description = "Maximum concurrent deterministic runs in each worker task."
  type        = number
  default     = 2

  validation {
    condition     = var.worker_concurrency >= 1 && var.worker_concurrency <= 32
    error_message = "worker_concurrency must be between 1 and 32."
  }
}

variable "api_desired_count" {
  type    = number
  default = 2
}

variable "mcp_desired_count" {
  type    = number
  default = 2
}

variable "worker_desired_count" {
  type    = number
  default = 2
}

variable "api_cpu" {
  type    = number
  default = 512
}

variable "api_memory" {
  type    = number
  default = 1024
}

variable "mcp_cpu" {
  type    = number
  default = 1024
}

variable "mcp_memory" {
  type    = number
  default = 2048
}

variable "worker_cpu" {
  type    = number
  default = 2048
}

variable "worker_memory" {
  type    = number
  default = 4096
}

variable "migration_cpu" {
  type    = number
  default = 512
}

variable "migration_memory" {
  type    = number
  default = 1024
}

variable "cpu_architecture" {
  description = "Fargate CPU architecture. The image pushed to ECR must be built for this architecture."
  type        = string
  default     = "X86_64"

  validation {
    condition     = contains(["X86_64", "ARM64"], var.cpu_architecture)
    error_message = "cpu_architecture must be X86_64 or ARM64."
  }
}

variable "database_name" {
  type    = string
  default = "skillwright"
}

variable "database_user" {
  description = "Least-privilege PostgreSQL login used by API, MCP, and worker tasks."
  type        = string
  default     = "skillwright"

  validation {
    condition     = can(regex("^[a-z_][a-z0-9_]{0,62}$", var.database_user))
    error_message = "database_user must be a safe PostgreSQL role name."
  }
}

variable "database_admin_user" {
  description = "RDS-managed master login used only by the one-off migration task."
  type        = string
  default     = "skillwright_admin"

  validation {
    condition     = can(regex("^[a-z_][a-z0-9_]{0,62}$", var.database_admin_user))
    error_message = "database_admin_user must be a safe PostgreSQL role name."
  }
}

variable "database_instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "database_allocated_storage_gib" {
  type    = number
  default = 50
}

variable "database_max_allocated_storage_gib" {
  type    = number
  default = 250
}

variable "postgres_engine_version" {
  description = "Optional PostgreSQL engine version. Null lets RDS choose the current default."
  type        = string
  default     = null
}

variable "database_multi_az" {
  type    = bool
  default = true
}

variable "database_backup_retention_days" {
  type    = number
  default = 14
}

variable "database_deletion_protection" {
  type    = bool
  default = true
}

variable "database_skip_final_snapshot" {
  type    = bool
  default = false
}

variable "redis_node_type" {
  type    = string
  default = "cache.t4g.small"
}

variable "redis_engine_version" {
  description = "Redis OSS engine version supported in the selected AWS region."
  type        = string
  default     = "7.1"
}

variable "redis_replica_count" {
  description = "Number of replicas in addition to the primary."
  type        = number
  default     = 1
}

variable "redis_snapshot_retention_days" {
  type    = number
  default = 7
}

variable "service_secret_arns" {
  description = "Map of API/MCP environment variable names to pre-created Secrets Manager ARNs, e.g. SKILLWRIGHT_AUTH_TOKEN_HASHES. Values are never read by Terraform."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for arn in values(var.service_secret_arns) : can(regex("^arn:aws[a-zA-Z-]*:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:.+$", arn))])
    error_message = "service_secret_arns values must be Secrets Manager ARNs."
  }
}

variable "service_secret_kms_key_arns" {
  description = "Optional customer-managed KMS key ARNs needed by ECS to inject service_secret_arns."
  type        = set(string)
  default     = []

  validation {
    condition     = alltrue([for arn in var.service_secret_kms_key_arns : can(regex("^arn:aws[a-zA-Z-]*:kms:[a-z0-9-]+:[0-9]{12}:key/.+$", arn))])
    error_message = "service_secret_kms_key_arns values must be KMS key ARNs."
  }
}

variable "workflow_secrets_manager_arns" {
  description = "Secrets Manager ARNs the Skillwright task role may resolve for workflow bindings."
  type        = set(string)
  default     = []

  validation {
    condition     = alltrue([for arn in var.workflow_secrets_manager_arns : can(regex("^arn:aws[a-zA-Z-]*:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:.+$", arn))])
    error_message = "workflow_secrets_manager_arns values must be Secrets Manager ARNs."
  }
}

variable "workflow_ssm_parameter_arns" {
  description = "SSM Parameter ARNs the Skillwright task role may resolve for workflow bindings."
  type        = set(string)
  default     = []

  validation {
    condition     = alltrue([for arn in var.workflow_ssm_parameter_arns : can(regex("^arn:aws[a-zA-Z-]*:ssm:[a-z0-9-]+:[0-9]{12}:parameter/.+$", arn))])
    error_message = "workflow_ssm_parameter_arns values must be SSM parameter ARNs."
  }
}

variable "workflow_kms_key_arns" {
  description = "Optional customer-managed KMS key ARNs needed to decrypt workflow secrets/parameters."
  type        = set(string)
  default     = []

  validation {
    condition     = alltrue([for arn in var.workflow_kms_key_arns : can(regex("^arn:aws[a-zA-Z-]*:kms:[a-z0-9-]+:[0-9]{12}:key/.+$", arn))])
    error_message = "workflow_kms_key_arns values must be KMS key ARNs."
  }
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "enable_autoscaling" {
  type    = bool
  default = true
}

variable "api_max_count" {
  type    = number
  default = 6
}

variable "mcp_max_count" {
  type    = number
  default = 6
}

variable "worker_max_count" {
  type    = number
  default = 20
}

variable "autoscaling_cpu_target" {
  type    = number
  default = 65
}
