resource "aws_sqs_queue" "scheduler_dlq" {
  name                      = "${local.resource_prefix}-scheduler-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_scheduler_schedule" "jobs" {
  for_each = local.projects

  name        = "${local.resource_prefix}-${each.key}"
  description = "Start ${each.key} on ${each.value.schedule_expression}"
  state       = var.schedules_enabled ? "ENABLED" : "DISABLED"

  schedule_expression = each.value.schedule_expression

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_codebuild_project.jobs[each.key].arn
    role_arn = aws_iam_role.scheduler.arn

    dead_letter_config {
      arn = aws_sqs_queue.scheduler_dlq.arn
    }

    retry_policy {
      maximum_event_age_in_seconds = 3600
      maximum_retry_attempts       = var.scheduler_maximum_retry_attempts
    }
  }

  depends_on = [aws_iam_role_policy.scheduler]
}
