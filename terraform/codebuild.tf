resource "aws_cloudwatch_log_group" "codebuild" {
  for_each = local.projects

  name              = "/aws/codebuild/${local.resource_prefix}-${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_codebuild_project" "jobs" {
  for_each = local.projects

  name                   = "${local.resource_prefix}-${each.key}"
  description            = "Bounded scheduled ${each.key} reconciler"
  service_role           = aws_iam_role.codebuild[each.key].arn
  build_timeout          = each.value.timeout_minutes
  queued_timeout         = 60
  concurrent_build_limit = 1

  source_version = aws_s3_object.source_bundle.version_id

  source {
    type      = "S3"
    location  = "${aws_s3_bucket.artifacts.bucket}/${aws_s3_object.source_bundle.key}"
    buildspec = each.value.buildspec
  }

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    type                        = "LINUX_CONTAINER"
    compute_type                = each.value.compute_type
    image                       = var.codebuild_image
    image_pull_credentials_type = "CODEBUILD"
    privileged_mode             = false

    dynamic "environment_variable" {
      for_each = each.value.plaintext_environment

      content {
        name  = environment_variable.key
        value = environment_variable.value
        type  = "PLAINTEXT"
      }
    }

    dynamic "environment_variable" {
      for_each = each.value.secret_environment

      content {
        name  = environment_variable.key
        value = environment_variable.value
        type  = "SECRETS_MANAGER"
      }
    }
  }

  logs_config {
    cloudwatch_logs {
      group_name  = aws_cloudwatch_log_group.codebuild[each.key].name
      stream_name = each.key
    }

    s3_logs {
      status = "DISABLED"
    }
  }

  depends_on = [
    aws_iam_role_policy.codebuild_base,
    aws_iam_role_policy.confluence_secret_kms,
    aws_iam_role_policy.finalize,
    aws_iam_role_policy.graph_secret_kms,
    aws_iam_role_policy.jira,
    aws_iam_role_policy.jira_secret_kms,
    aws_iam_role_policy.reconcile,
    aws_iam_role_policy.zoom,
    aws_iam_role_policy.zoom_secret_kms,
    aws_s3_bucket_policy.artifacts,
  ]
}
