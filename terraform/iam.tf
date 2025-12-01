data "aws_iam_policy_document" "codebuild_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "codebuild" {
  for_each = local.projects

  name               = "${local.resource_prefix}-${each.key}-codebuild"
  description        = "Stage-specific CodeBuild role for ${each.key}"
  assume_role_policy = data.aws_iam_policy_document.codebuild_assume.json
}

data "aws_iam_policy_document" "codebuild_base" {
  for_each = local.projects

  statement {
    sid = "WriteBuildLogs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.codebuild[each.key].arn}:*"]
  }

  statement {
    sid = "ReadVersionedSourceBundle"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
    ]
    resources = [local.source_arn]
  }

  statement {
    sid = "ReadSourceBucketLocation"
    actions = [
      "s3:GetBucketAcl",
      "s3:GetBucketLocation",
    ]
    resources = [local.bucket_arn]
  }

  statement {
    sid       = "ReadRuntimeConfiguration"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.config.arn]
  }
}

resource "aws_iam_role_policy" "codebuild_base" {
  for_each = local.projects

  name   = "source-config-and-logs"
  role   = aws_iam_role.codebuild[each.key].id
  policy = data.aws_iam_policy_document.codebuild_base[each.key].json
}

data "aws_iam_policy_document" "zoom" {
  statement {
    sid       = "ListZoomWorkingPrefixes"
    actions   = ["s3:ListBucket"]
    resources = [local.bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = local.list_prefixes["zoom-download"]
    }
  }

  statement {
    sid = "ReconcileZoomRecordingObjects"
    actions = [
      "s3:AbortMultipartUpload",
      "s3:GetObject",
      "s3:GetObjectAnnotation",
      "s3:GetObjectAttributes",
      "s3:ListMultipartUploadParts",
      "s3:ListObjectAnnotations",
      "s3:PutObject",
      "s3:PutObjectAnnotation",
    ]
    resources = [
      local.meetings_object_arn,
      local.quarantine_object_arn,
    ]
  }

  statement {
    sid       = "ListBucketMultipartUploads"
    actions   = ["s3:ListBucketMultipartUploads"]
    resources = [local.bucket_arn]
  }

  statement {
    sid       = "ReadZoomOAuthIntegrationSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [local.zoom_secret_arn]
  }
}

resource "aws_iam_role_policy" "zoom" {
  name   = "zoom-download"
  role   = aws_iam_role.codebuild["zoom-download"].id
  policy = data.aws_iam_policy_document.zoom.json
}

data "aws_iam_policy_document" "jira" {
  statement {
    sid       = "ListJiraSnapshotPrefix"
    actions   = ["s3:ListBucket"]
    resources = [local.bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = local.list_prefixes["jira-snapshot"]
    }
  }

  statement {
    sid = "WriteJiraSnapshots"
    actions = [
      "s3:GetObject",
      "s3:GetObjectAttributes",
      "s3:PutObject",
    ]
    resources = [local.jira_object_arn]
  }

  statement {
    sid       = "ReadJiraSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [local.jira_secret_arn]
  }
}

resource "aws_iam_role_policy" "jira" {
  name   = "jira-snapshot"
  role   = aws_iam_role.codebuild["jira-snapshot"].id
  policy = data.aws_iam_policy_document.jira.json
}

data "aws_iam_policy_document" "reconcile" {
  statement {
    sid       = "ListReconciliationInputs"
    actions   = ["s3:ListBucket"]
    resources = [local.bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = local.list_prefixes["meeting-reconcile"]
    }
  }

  statement {
    sid = "ReconcileMeetingArtifacts"
    actions = [
      "s3:GetObject",
      "s3:GetObjectAnnotation",
      "s3:GetObjectAttributes",
      "s3:ListObjectAnnotations",
      "s3:PutObject",
      "s3:PutObjectAnnotation",
      "s3:PutObjectVersionAnnotation",
    ]
    resources = [local.meetings_object_arn]
  }

  statement {
    sid = "ReadContextInputs"
    actions = [
      "s3:GetObject",
      "s3:GetObjectAttributes",
    ]
    resources = [
      local.jira_object_arn,
      local.sharepoint_sync_object_arn,
    ]
  }

  statement {
    sid = "FreezeAggregateMembership"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = [local.aggregates_object_arn]
  }

  statement {
    sid = "ReconcileTranscriptionJobs"
    actions = [
      "transcribe:GetTranscriptionJob",
      "transcribe:StartTranscriptionJob",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "ReadConfluenceSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [local.confluence_secret_arn]
  }
}

resource "aws_iam_role_policy" "reconcile" {
  name   = "meeting-reconcile"
  role   = aws_iam_role.codebuild["meeting-reconcile"].id
  policy = data.aws_iam_policy_document.reconcile.json
}

data "aws_iam_policy_document" "finalize" {
  statement {
    sid       = "ListFinalizationInputs"
    actions   = ["s3:ListBucket"]
    resources = [local.bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = local.list_prefixes["analysis-finalize"]
    }
  }

  statement {
    sid = "ReadFinalizationInputs"
    actions = [
      "s3:GetObject",
      "s3:GetObjectAnnotation",
      "s3:GetObjectAttributes",
      "s3:ListObjectAnnotations",
    ]
    resources = [
      local.meetings_object_arn,
      local.jira_object_arn,
      local.aggregates_object_arn,
      local.sharepoint_sync_object_arn,
    ]
  }

  statement {
    sid     = "ReadConfluenceAndGraphSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      local.confluence_secret_arn,
      local.graph_secret_arn,
    ]
  }
}

resource "aws_iam_role_policy" "finalize" {
  name   = "analysis-finalize"
  role   = aws_iam_role.codebuild["analysis-finalize"].id
  policy = data.aws_iam_policy_document.finalize.json
}

resource "aws_iam_role_policy" "zoom_secret_kms" {
  count = var.zoom_secret_kms_key_arn == null ? 0 : 1

  name = "decrypt-zoom-secret"
  role = aws_iam_role.codebuild["zoom-download"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["kms:Decrypt"]
      Resource = [var.zoom_secret_kms_key_arn]
    }]
  })
}

resource "aws_iam_role_policy" "jira_secret_kms" {
  count = var.jira_secret_kms_key_arn == null ? 0 : 1

  name = "decrypt-jira-secret"
  role = aws_iam_role.codebuild["jira-snapshot"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["kms:Decrypt"]
      Resource = [var.jira_secret_kms_key_arn]
    }]
  })
}

resource "aws_iam_role_policy" "confluence_secret_kms" {
  for_each = var.confluence_secret_kms_key_arn == null ? toset([]) : toset([
    "meeting-reconcile",
    "analysis-finalize",
  ])

  name = "decrypt-confluence-secret"
  role = aws_iam_role.codebuild[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["kms:Decrypt"]
      Resource = [var.confluence_secret_kms_key_arn]
    }]
  })
}

resource "aws_iam_role_policy" "graph_secret_kms" {
  count = var.graph_secret_kms_key_arn == null ? 0 : 1

  name = "decrypt-graph-secret"
  role = aws_iam_role.codebuild["analysis-finalize"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["kms:Decrypt"]
      Resource = [var.graph_secret_kms_key_arn]
    }]
  })
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values = [
        "arn:${data.aws_partition.current.partition}:scheduler:${var.aws_region}:${data.aws_caller_identity.current.account_id}:schedule/default/${local.resource_prefix}-*",
      ]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.resource_prefix}-scheduler"
  description        = "Allows EventBridge Scheduler to start the four bounded CodeBuild jobs"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    sid       = "StartScheduledBuilds"
    actions   = ["codebuild:StartBuild"]
    resources = [for project in aws_codebuild_project.jobs : project.arn]
  }

  statement {
    sid       = "SendFailedInvocationsToDLQ"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.scheduler_dlq.arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "start-builds-and-write-dlq"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}
