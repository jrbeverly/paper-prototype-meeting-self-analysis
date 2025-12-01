check "isolated_s3_prefixes" {
  assert {
    condition = (
      length(toset(local.data_prefixes)) == length(local.data_prefixes) &&
      alltrue([
        for left in local.data_prefixes :
        alltrue([
          for right in local.data_prefixes :
          left == right || !startswith(left, right)
        ])
      ])
    )
    error_message = "meetings, quarantine, Jira, aggregate, and SharePoint prefixes must be distinct and must not contain one another; stage IAM depends on their isolation."
  }
}

check "source_outside_workflow_prefixes" {
  assert {
    condition     = alltrue([for prefix in local.data_prefixes : !startswith(var.source_bundle_key, prefix)])
    error_message = "source_bundle_key must be outside every mutable workflow prefix."
  }
}
