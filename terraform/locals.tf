locals {
  resource_prefix = "${var.name_prefix}-${var.environment}"
  bucket_name = coalesce(
    var.artifact_bucket_name,
    "${local.resource_prefix}-${data.aws_caller_identity.current.account_id}-${data.aws_region.current.region}",
  )
  config_parameter_name = coalesce(
    var.config_parameter_name,
    "/${var.name_prefix}/${var.environment}/config",
  )

  bucket_arn = aws_s3_bucket.artifacts.arn
  source_arn = "${aws_s3_bucket.artifacts.arn}/${var.source_bundle_key}"

  data_prefixes = [
    var.meetings_prefix,
    var.quarantine_prefix,
    "jira/",
    "aggregates/",
    var.sharepoint_sync_prefix,
  ]

  meetings_object_arn        = "${aws_s3_bucket.artifacts.arn}/${var.meetings_prefix}*"
  quarantine_object_arn      = "${aws_s3_bucket.artifacts.arn}/${var.quarantine_prefix}*"
  jira_object_arn            = "${aws_s3_bucket.artifacts.arn}/jira/*"
  aggregates_object_arn      = "${aws_s3_bucket.artifacts.arn}/aggregates/*"
  sharepoint_sync_object_arn = "${aws_s3_bucket.artifacts.arn}/${var.sharepoint_sync_prefix}*"

  list_prefixes = {
    zoom-download = [
      var.meetings_prefix,
      "${var.meetings_prefix}*",
      var.quarantine_prefix,
      "${var.quarantine_prefix}*",
    ]
    jira-snapshot = [
      "jira/",
      "jira/*",
    ]
    meeting-reconcile = [
      var.meetings_prefix,
      "${var.meetings_prefix}*",
      "jira/",
      "jira/*",
      "aggregates/",
      "aggregates/*",
      var.sharepoint_sync_prefix,
      "${var.sharepoint_sync_prefix}*",
    ]
    analysis-finalize = [
      var.meetings_prefix,
      "${var.meetings_prefix}*",
      "jira/",
      "jira/*",
      "aggregates/",
      "aggregates/*",
      var.sharepoint_sync_prefix,
      "${var.sharepoint_sync_prefix}*",
    ]
  }

  projects = {
    zoom-download = {
      buildspec           = "buildspecs/zoom-download.yml"
      compute_type        = "BUILD_GENERAL1_MEDIUM"
      timeout_minutes     = 30
      schedule_expression = "rate(30 minutes)"
      plaintext_environment = {
        EXPECTED_BUCKET_OWNER = data.aws_caller_identity.current.account_id
        MI_BUCKET             = aws_s3_bucket.artifacts.bucket
        MI_CONFIG_PARAMETER   = aws_ssm_parameter.config.name
        MI_QUARANTINE_PREFIX  = trimsuffix(var.quarantine_prefix, "/")
        MI_RECORDINGS_PREFIX  = trimsuffix(var.meetings_prefix, "/")
        ZOOM_LOOKBACK_DAYS    = tostring(var.zoom_lookback_days)
        ZOOM_USERS            = join(",", sort(tolist(var.zoom_users)))
      }
      secret_environment = {
        MI_ZOOM_OAUTH_SECRET_JSON = local.zoom_secret_arn
      }
    }
    jira-snapshot = {
      buildspec           = "buildspecs/jira-snapshot.yml"
      compute_type        = "BUILD_GENERAL1_SMALL"
      timeout_minutes     = 30
      schedule_expression = "rate(30 minutes)"
      plaintext_environment = {
        MI_ACLI_PATH        = "/usr/local/bin/acli"
        MI_CONFIG_PARAMETER = aws_ssm_parameter.config.name
        MI_BUCKET           = aws_s3_bucket.artifacts.bucket
        PYTHONUNBUFFERED    = "1"
      }
      secret_environment = {
        MI_JIRA_SECRET_JSON = local.jira_secret_arn
      }
    }
    meeting-reconcile = {
      buildspec           = "buildspecs/meeting-reconcile.yml"
      compute_type        = "BUILD_GENERAL1_MEDIUM"
      timeout_minutes     = 60
      schedule_expression = "rate(5 minutes)"
      plaintext_environment = {
        MI_CONFIG_PARAMETER = aws_ssm_parameter.config.name
        MI_BUCKET           = aws_s3_bucket.artifacts.bucket
        PYTHONUNBUFFERED    = "1"
      }
      secret_environment = {
        MI_CONFLUENCE_SECRET_JSON = local.confluence_secret_arn
      }
    }
    analysis-finalize = {
      buildspec           = "buildspecs/analysis-finalize.yml"
      compute_type        = var.finalizer_compute_type
      timeout_minutes     = var.finalizer_timeout_minutes
      schedule_expression = "rate(5 minutes)"
      plaintext_environment = {
        MI_CONFIG_PARAMETER = aws_ssm_parameter.config.name
        MI_BUCKET           = aws_s3_bucket.artifacts.bucket
        PYTHONUNBUFFERED    = "1"
      }
      secret_environment = {
        MI_CONFLUENCE_SECRET_JSON = local.confluence_secret_arn
        MI_GRAPH_SECRET_JSON      = local.graph_secret_arn
      }
    }
  }
}
