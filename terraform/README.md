# Meeting Intelligence infrastructure

This directory is one flat Terraform root for the cron-driven proof of
concept. It creates:

- one private, SSE-S3 encrypted, versioned S3 bucket for the shared source ZIP
  and workflow objects, with incomplete multipart uploads aborted after seven
  days;
- four AWS-managed-image CodeBuild projects named
  `<prefix>-<environment>-zoom-download`, `jira-snapshot`,
  `meeting-reconcile`, and `analysis-finalize`;
- EventBridge Scheduler invocations at 30, 30, 5, and 5 minute cadences,
  respectively;
- one Scheduler invocation role and one encrypted SQS dead-letter queue;
- one least-privilege CodeBuild role per project;
- an SSM `String` parameter containing non-secret JSON configuration;
- either four empty Secrets Manager shells or references to four existing
  secrets, without storing secret values in Terraform; and
- one SNS topic, a CloudWatch `FailedBuilds` alarm per project, and an alarm
  when Scheduler delivery failures become visible in the shared DLQ.

All projects have `concurrent_build_limit = 1`. Only
`meeting-reconcile` can call Amazon Transcribe. Only `analysis-finalize`
receives the Microsoft Graph secret. The stack does not create Lambda
functions, DynamoDB tables, Step Functions, a custom event bus, or ECR
repositories.

## Prerequisites

- Terraform 1.7 or newer, `zip`, and AWS credentials able to create the
  resources.
- A configured Zoom General OAuth component. The build receives only the
  broker endpoint/authentication needed to request a current access token; it
  does not own Zoom refresh tokens.
- Confluence parent pages and an Automation rule that invokes the Rovo agent,
  persists `agentResponse` to a labeled result page, and preserves the source
  page association.
- The retained inbound SharePoint synchronization job, plus a Microsoft Graph
  application allowed to write clips to the configured site and drive.

Terraform state is local by default. Configure your organization's remote
backend before the first shared deployment.

## Package and deploy

The four projects use the same version-pinned S3 ZIP as their source. From the
repository root, package the current tracked and non-ignored runtime files
(`buildspecs`, `cmd`, the Python package, schemas/templates, and language
manifests):

```sh
./terraform/package-source.sh
```

Then configure and apply:

```sh
cd terraform
cp terraform.tfvars.example terraform.tfvars
# Edit every placeholder and leave schedules_enabled=false for the first apply.
terraform init
terraform fmt -check
terraform validate
terraform plan -out=tfplan
terraform apply tfplan
```

`config_json` is non-secret. Terraform normalizes it and injects the actual
S3 `artifactBucket` and `activePrefix` before writing it to SSM. Updating the
source ZIP and applying again uploads a new object version and updates all four
CodeBuild projects to that immutable version.

Keep every `inboundSharePointProfiles.*.prefixes` entry beneath
`sharepoint_sync_prefix` (default `sharepoint/`). IAM deliberately fails
closed for synchronized content outside that allowlisted parent.

## Populate credentials outside Terraform

When an input secret ARN is null, Terraform creates only a secret container;
it deliberately creates no `aws_secretsmanager_secret_version`. Obtain the
ARNs with:

```sh
terraform output -json secret_arns
```

Prepare local JSON files outside the repository and use
`aws secretsmanager put-secret-value --secret-id <arn> --secret-string
file://<path>`. Expected high-level shapes are:

- Zoom: `{"tokenUrl":"...","method":"POST","authorization":"...","headers":{}}`
- Jira:
  `{"site":"example.atlassian.net","email":"bot@example.com","apiToken":"..."}`
- Confluence basic auth:
  `{"email":"bot@example.com","apiToken":"..."}`; a bearer-only
  `{"token":"..."}` is also accepted.
- Graph:
  `{"tenantId":"...","clientId":"...","clientSecret":"..."}`.

The complete JSON is injected at build start as
`MI_ZOOM_OAUTH_SECRET_JSON`, `MI_JIRA_SECRET_JSON`,
`MI_CONFLUENCE_SECRET_JSON`, or `MI_GRAPH_SECRET_JSON`. Do not place these
values in `config_json`, a `.tfvars` file, a buildspec, or source control. If a
supplied secret uses a customer-managed KMS key, set its corresponding
`*_secret_kms_key_arn` variable.

## Prove and enable the schedules

Start every project once while schedules are disabled:

```sh
for project in \
  zoom-download jira-snapshot meeting-reconcile analysis-finalize
do
  name="$(terraform output -json codebuild_project_names | jq -r --arg p "$project" '.[$p]')"
  aws codebuild start-build --project-name "$name"
done
```

Confirm the builds are idempotent and that their CloudWatch logs show the
expected bounded work. Then set `schedules_enabled=true` and apply again.
Scheduler invokes CodeBuild directly with the project ARN. Delivery failures
are retried and then sent to the single queue returned by
`scheduler_dlq_url`.

The finalizer defaults to `BUILD_GENERAL1_LARGE`, which currently provides
8 vCPU, 16 GiB of memory, and 128 GB of workspace disk, with a four-hour
timeout. Set `finalizer_compute_type` to `BUILD_GENERAL1_XLARGE` or
`BUILD_GENERAL1_2XLARGE` when the largest source recording plus re-encoded
clips cannot fit comfortably in 128 GB. Keep each run bounded through
`finalization.maxDemos` and the application's per-run limits.

To confirm an email alarm subscription, use the link AWS sends to each address
in `alarm_email_endpoints`. Inspect Scheduler failures with:

```sh
aws sqs receive-message \
  --queue-url "$(terraform output -raw scheduler_dlq_url)" \
  --attribute-names All \
  --message-attribute-names All
```
