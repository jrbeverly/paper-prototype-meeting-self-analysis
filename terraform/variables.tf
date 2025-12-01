variable "aws_region" {
  description = "AWS Region in which to deploy the scheduled jobs."
  type        = string
  default     = "ca-central-1"
}

variable "name_prefix" {
  description = "Lowercase prefix used for every resource name."
  type        = string
  default     = "meeting-intelligence"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,19}$", var.name_prefix))
    error_message = "name_prefix must be 2-20 lowercase letters, digits, or hyphens and start with a letter or digit."
  }
}

variable "environment" {
  description = "Short lowercase deployment environment name."
  type        = string
  default     = "dev"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,9}$", var.environment))
    error_message = "environment must be 1-10 lowercase letters, digits, or hyphens and start with a letter or digit."
  }
}

variable "artifact_bucket_name" {
  description = "Optional exact name for the Terraform-managed artifact and workflow bucket. A globally unique account/Region-qualified name is used when null."
  type        = string
  default     = null

  validation {
    condition = var.artifact_bucket_name == null ? true : (
      length(var.artifact_bucket_name) >= 3 &&
      length(var.artifact_bucket_name) <= 63 &&
      can(regex("^[a-z0-9][a-z0-9.-]*[a-z0-9]$", var.artifact_bucket_name))
    )
    error_message = "artifact_bucket_name must be a valid 3-63 character S3 bucket name, or null."
  }
}

variable "artifact_bucket_force_destroy" {
  description = "Allow Terraform to delete the artifact bucket and all object versions. Keep false outside disposable test environments."
  type        = bool
  default     = false
}

variable "abort_incomplete_multipart_days" {
  description = "Days after initiation that incomplete S3 multipart uploads are aborted."
  type        = number
  default     = 7

  validation {
    condition     = var.abort_incomplete_multipart_days >= 1
    error_message = "abort_incomplete_multipart_days must be at least 1."
  }
}

variable "source_bundle_path" {
  description = "Path, relative to the Terraform working directory or absolute, to the repository ZIP uploaded as the immutable CodeBuild S3 source version."
  type        = string

  validation {
    condition     = endswith(lower(var.source_bundle_path), ".zip")
    error_message = "source_bundle_path must point to a .zip file."
  }
}

variable "source_bundle_key" {
  description = "Object key at which Terraform uploads the CodeBuild source ZIP."
  type        = string
  default     = "source/meeting-intelligence.zip"

  validation {
    condition     = !startswith(var.source_bundle_key, "/") && endswith(lower(var.source_bundle_key), ".zip")
    error_message = "source_bundle_key must be a relative S3 key ending in .zip."
  }
}

variable "meetings_prefix" {
  description = "S3 prefix containing Zoom recordings, VTTs, and their annotations."
  type        = string
  default     = "meetings/"

  validation {
    condition     = !startswith(var.meetings_prefix, "/") && endswith(var.meetings_prefix, "/")
    error_message = "meetings_prefix must be a relative S3 prefix ending in '/'."
  }
}

variable "quarantine_prefix" {
  description = "S3 prefix in which the Zoom downloader records explicitly quarantined routing metadata."
  type        = string
  default     = "quarantine/"

  validation {
    condition     = !startswith(var.quarantine_prefix, "/") && endswith(var.quarantine_prefix, "/")
    error_message = "quarantine_prefix must be a relative S3 prefix ending in '/'."
  }
}

variable "zoom_users" {
  description = "Zoom user IDs scanned by the downloader. Values other than 'me' require suitable General OAuth admin scopes."
  type        = set(string)
  default     = ["me"]

  validation {
    condition     = length(var.zoom_users) > 0 && alltrue([for user in var.zoom_users : trimspace(user) != ""])
    error_message = "zoom_users must contain at least one non-empty Zoom user ID."
  }
}

variable "zoom_lookback_days" {
  description = "Default inclusive recording lookback window used on each Zoom download run."
  type        = number
  default     = 1

  validation {
    condition     = var.zoom_lookback_days >= 0 && floor(var.zoom_lookback_days) == var.zoom_lookback_days
    error_message = "zoom_lookback_days must be a non-negative integer."
  }
}

variable "sharepoint_sync_prefix" {
  description = "S3 prefix containing data produced by the retained inbound SharePoint synchronization job."
  type        = string
  default     = "sharepoint/"

  validation {
    condition     = !startswith(var.sharepoint_sync_prefix, "/") && endswith(var.sharepoint_sync_prefix, "/")
    error_message = "sharepoint_sync_prefix must be a relative S3 prefix ending in '/'."
  }
}

variable "config_parameter_name" {
  description = "Optional exact SSM parameter name. Defaults to /<name_prefix>/<environment>/config."
  type        = string
  default     = null

  validation {
    condition     = var.config_parameter_name == null ? true : startswith(var.config_parameter_name, "/")
    error_message = "config_parameter_name must start with '/', or be null."
  }
}

variable "config_json" {
  description = "Non-secret runtime configuration encoded as a JSON object. Terraform injects artifactBucket and activePrefix. Credentials belong in Secrets Manager, never in this value."
  type        = string
  default     = "{}"

  validation {
    condition     = can(keys(jsondecode(var.config_json)))
    error_message = "config_json must decode to a JSON object."
  }
}

variable "zoom_secret_arn" {
  description = "Existing Zoom General OAuth integration secret ARN. When null, Terraform creates an empty secret shell without a value."
  type        = string
  default     = null
}

variable "jira_secret_arn" {
  description = "Existing Jira bot secret ARN. When null, Terraform creates an empty secret shell without a value."
  type        = string
  default     = null
}

variable "confluence_secret_arn" {
  description = "Existing Confluence publishing secret ARN. When null, Terraform creates an empty secret shell without a value."
  type        = string
  default     = null
}

variable "graph_secret_arn" {
  description = "Existing Microsoft Graph outbound-upload secret ARN. When null, Terraform creates an empty secret shell without a value."
  type        = string
  default     = null
}

variable "zoom_secret_kms_key_arn" {
  description = "Optional customer-managed KMS key ARN used by the supplied Zoom secret."
  type        = string
  default     = null
}

variable "jira_secret_kms_key_arn" {
  description = "Optional customer-managed KMS key ARN used by the supplied Jira secret."
  type        = string
  default     = null
}

variable "confluence_secret_kms_key_arn" {
  description = "Optional customer-managed KMS key ARN used by the supplied Confluence secret."
  type        = string
  default     = null
}

variable "graph_secret_kms_key_arn" {
  description = "Optional customer-managed KMS key ARN used by the supplied Microsoft Graph secret."
  type        = string
  default     = null
}

variable "codebuild_image" {
  description = "AWS-managed CodeBuild image. The default supports Go 1.24 and Python 3.11."
  type        = string
  default     = "aws/codebuild/standard:7.0"

  validation {
    condition     = startswith(var.codebuild_image, "aws/codebuild/")
    error_message = "codebuild_image must be an AWS-managed aws/codebuild/* image."
  }
}

variable "finalizer_compute_type" {
  description = "CodeBuild compute for FFmpeg finalization. LARGE supplies 8 vCPU, 16 GiB RAM, and 128 GB workspace storage on Linux on-demand compute."
  type        = string
  default     = "BUILD_GENERAL1_LARGE"

  validation {
    condition = contains([
      "BUILD_GENERAL1_LARGE",
      "BUILD_GENERAL1_XLARGE",
      "BUILD_GENERAL1_2XLARGE",
    ], var.finalizer_compute_type)
    error_message = "finalizer_compute_type must be LARGE, XLARGE, or 2XLARGE so video finalization is not under-provisioned."
  }
}

variable "finalizer_timeout_minutes" {
  description = "Maximum duration of one bounded analysis-finalize build."
  type        = number
  default     = 240

  validation {
    condition     = var.finalizer_timeout_minutes >= 30 && var.finalizer_timeout_minutes <= 2160
    error_message = "finalizer_timeout_minutes must be between 30 and 2160."
  }
}

variable "schedules_enabled" {
  description = "Whether the four EventBridge Scheduler schedules are enabled."
  type        = bool
  default     = true
}

variable "scheduler_maximum_retry_attempts" {
  description = "Delivery retries before Scheduler sends an invocation to the shared DLQ."
  type        = number
  default     = 2

  validation {
    condition     = var.scheduler_maximum_retry_attempts >= 0 && var.scheduler_maximum_retry_attempts <= 185
    error_message = "scheduler_maximum_retry_attempts must be between 0 and 185."
  }
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for CodeBuild project logs."
  type        = number
  default     = 30
}

variable "alarm_email_endpoints" {
  description = "Email addresses subscribed to the build-failure SNS topic. Each recipient must confirm the AWS subscription."
  type        = set(string)
  default     = []
}

variable "tags" {
  description = "Additional tags applied to all taggable resources."
  type        = map(string)
  default     = {}
}
