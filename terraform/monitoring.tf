resource "aws_sns_topic" "build_failures" {
  name = "${local.resource_prefix}-build-failures"
}

data "aws_iam_policy_document" "build_failure_topic" {
  statement {
    sid    = "AllowAccountAdministration"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"]
    }

    actions = ["sns:*"]
    resources = [
      aws_sns_topic.build_failures.arn,
    ]
  }

  statement {
    sid    = "AllowCloudWatchAlarmPublish"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }

    actions = ["sns:Publish"]
    resources = [
      aws_sns_topic.build_failures.arn,
    ]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "build_failures" {
  arn    = aws_sns_topic.build_failures.arn
  policy = data.aws_iam_policy_document.build_failure_topic.json
}

resource "aws_sns_topic_subscription" "build_failure_email" {
  for_each = var.alarm_email_endpoints

  topic_arn = aws_sns_topic.build_failures.arn
  protocol  = "email"
  endpoint  = each.value
}

resource "aws_cloudwatch_metric_alarm" "codebuild_failed" {
  for_each = local.projects

  alarm_name          = "${local.resource_prefix}-${each.key}-failed"
  alarm_description   = "At least one ${each.key} CodeBuild execution reported a failure or timeout in five minutes."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  metric_name         = "FailedBuilds"
  namespace           = "AWS/CodeBuild"
  period              = 300
  statistic           = "Sum"
  treat_missing_data  = "notBreaching"

  dimensions = {
    ProjectName = aws_codebuild_project.jobs[each.key].name
  }

  alarm_actions = [aws_sns_topic.build_failures.arn]
  ok_actions    = [aws_sns_topic.build_failures.arn]
}

resource "aws_cloudwatch_metric_alarm" "scheduler_dlq_visible" {
  alarm_name          = "${local.resource_prefix}-scheduler-dlq-visible"
  alarm_description   = "At least one EventBridge Scheduler invocation could not start its CodeBuild project and reached the shared DLQ."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.scheduler_dlq.name
  }

  alarm_actions = [aws_sns_topic.build_failures.arn]
  ok_actions    = [aws_sns_topic.build_failures.arn]
}
