resource "aws_secretsmanager_secret" "zoom" {
  count = var.zoom_secret_arn == null ? 1 : 0

  name                    = "${local.resource_prefix}/zoom-oauth"
  description             = "Secret shell for the Zoom General OAuth integration. Populate outside Terraform."
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "jira" {
  count = var.jira_secret_arn == null ? 1 : 0

  name                    = "${local.resource_prefix}/jira"
  description             = "Secret shell for Jira ACLI credentials. Populate outside Terraform."
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "confluence" {
  count = var.confluence_secret_arn == null ? 1 : 0

  name                    = "${local.resource_prefix}/confluence"
  description             = "Secret shell for Confluence publication credentials. Populate outside Terraform."
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "graph" {
  count = var.graph_secret_arn == null ? 1 : 0

  name                    = "${local.resource_prefix}/microsoft-graph"
  description             = "Secret shell for outbound Microsoft Graph clip uploads. Populate outside Terraform."
  recovery_window_in_days = 7
}

locals {
  zoom_secret_arn = var.zoom_secret_arn != null ? var.zoom_secret_arn : aws_secretsmanager_secret.zoom[0].arn
  jira_secret_arn = var.jira_secret_arn != null ? var.jira_secret_arn : aws_secretsmanager_secret.jira[0].arn
  confluence_secret_arn = (
    var.confluence_secret_arn != null
    ? var.confluence_secret_arn
    : aws_secretsmanager_secret.confluence[0].arn
  )
  graph_secret_arn = var.graph_secret_arn != null ? var.graph_secret_arn : aws_secretsmanager_secret.graph[0].arn
}
