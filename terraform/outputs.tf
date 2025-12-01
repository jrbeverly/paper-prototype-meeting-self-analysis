output "artifact_bucket_name" {
  description = "Versioned S3 bucket containing the source bundle and workflow artifacts."
  value       = aws_s3_bucket.artifacts.bucket
}

output "source_bundle" {
  description = "Version-pinned S3 source object used by all four CodeBuild projects."
  value = {
    bucket     = aws_s3_object.source_bundle.bucket
    key        = aws_s3_object.source_bundle.key
    version_id = aws_s3_object.source_bundle.version_id
  }
}

output "config_parameter_name" {
  description = "SSM parameter from which jobs load non-secret JSON configuration."
  value       = aws_ssm_parameter.config.name
}

output "secret_arns" {
  description = "Secret shells or supplied secrets to populate out of band. Values are ARNs only."
  value = {
    zoom       = local.zoom_secret_arn
    jira       = local.jira_secret_arn
    confluence = local.confluence_secret_arn
    graph      = local.graph_secret_arn
  }
}

output "codebuild_project_names" {
  description = "Scheduled CodeBuild project names."
  value       = { for name, project in aws_codebuild_project.jobs : name => project.name }
}

output "schedule_names" {
  description = "EventBridge Scheduler schedule names and configured cadences."
  value = {
    for name, schedule in aws_scheduler_schedule.jobs :
    name => {
      name       = schedule.name
      expression = local.projects[name].schedule_expression
      state      = schedule.state
    }
  }
}

output "scheduler_dlq_url" {
  description = "Shared dead-letter queue URL for Scheduler delivery failures."
  value       = aws_sqs_queue.scheduler_dlq.url
}

output "build_failure_topic_arn" {
  description = "SNS topic notified by the four CodeBuild failure alarms."
  value       = aws_sns_topic.build_failures.arn
}
