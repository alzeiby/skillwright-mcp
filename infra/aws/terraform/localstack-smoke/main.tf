terraform {
  required_version = ">= 1.8.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.90, < 7.0"
    }
  }
}

variable "endpoint_url" {
  description = "LocalStack edge endpoint reachable from the Terraform container."
  type        = string
}

variable "run_id" {
  description = "Unique suffix so concurrent smoke runs do not share resources."
  type        = string
}

provider "aws" {
  region                      = "us-east-1"
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_region_validation      = true
  skip_requesting_account_id  = true

  endpoints {
    secretsmanager = var.endpoint_url
    ssm            = var.endpoint_url
  }
}

resource "aws_secretsmanager_secret" "workflow" {
  name                    = "/skillwright/localstack/${var.run_id}/workflow-password"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "workflow" {
  secret_id     = aws_secretsmanager_secret.workflow.id
  secret_string = "localstack-secret-v1"
}

resource "aws_ssm_parameter" "workflow" {
  name  = "/skillwright/localstack/${var.run_id}/workflow-password"
  type  = "SecureString"
  value = "localstack-parameter-v1"
}

output "secret_name" {
  value = aws_secretsmanager_secret.workflow.name
}

output "parameter_name" {
  value = aws_ssm_parameter.workflow.name
}
