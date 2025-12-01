# Deployment Guide

The repository supplies one flat Terraform root for the proof of concept. It
has not been applied to a live account or tested end to end against the target
Zoom, Atlassian, or Microsoft tenants as part of this implementation.

## What Terraform creates

- one private, versioned, SSE-S3 artifact bucket;
- one version-pinned source ZIP in that bucket;
- four AWS-managed-image CodeBuild projects;
- four EventBridge Scheduler schedules;
- one Scheduler invocation role and encrypted SQS DLQ;
- one stage-specific CodeBuild IAM role per project;
- one non-secret SSM configuration parameter;
- four empty Secrets Manager shells, or references to supplied secrets;
- one SNS topic and one build-state alarm per project; and
- CloudWatch log groups with configured retention.

It does not create a Lambda function, custom EventBridge bus, Step Functions
workflow, application DynamoDB table, per-stage SQS queue, or ECR repository.

## Prerequisites

- Terraform 1.7 or newer;
- AWS provider `>= 5.80, < 7.0`;
- `git` and `zip` for source packaging;
- AWS credentials authorized to create the declared resources;
- a remote Terraform backend chosen before the first shared apply;
- a working Zoom General OAuth broker and General app authorization;
- Confluence input/result/final parent pages plus a Rovo agent/Automation
  entitlement;
- the existing inbound SharePoint-to-S3 feed; and
- a Microsoft Graph application allowed to write to the configured clip
  destination.

The default region is `ca-central-1`. Prove S3 Object Annotation operations in
the selected account/region before enabling schedules.

## 1. Prepare non-secret configuration

Copy [`terraform/terraform.tfvars.example`](../terraform/terraform.tfvars.example)
to the ignored `terraform/terraform.tfvars`. Replace every placeholder.

Keep:

```hcl
schedules_enabled = false
```

for the first apply. `config_json` must contain the complete non-secret JSON
document except `artifactBucket` and `activePrefix`; Terraform injects their
actual values and stores the result as an SSM `String` parameter. Use
[`config/example.json`](../config/example.json) and
[the configuration reference](configuration.md) to review every field.

Never put broker authorization, Jira/Confluence tokens, Graph credentials, or
other secret values in `config_json`, `.tfvars`, the source ZIP, or a
buildspec.

## 2. Package the exact source

From the repository root:

```bash
./terraform/package-source.sh
```

The default output is `dist/meeting-intelligence-source.zip`. The script uses
tracked and non-ignored files from the current worktree, so inspect
`git status` first. `source_bundle_path` in tfvars must point to this ZIP.

Terraform uploads it to the versioned artifact bucket and pins every
CodeBuild project to the resulting object version. Applying a changed ZIP
updates all four projects to one source revision.

## 3. Initialize, review, and apply with schedules disabled

```bash
cd terraform
terraform init
terraform fmt -check
terraform validate
terraform plan -out=tfplan
terraform apply tfplan
```

Review the plan for:

- exact account and region;
- bucket name and `force_destroy=false`;
- AWS-managed CodeBuild image;
- four project IAM policies and secret separation;
- disabled schedules;
- expected SSM parameter name;
- no secret versions in state; and
- no Lambda, Step Functions, DynamoDB, custom bus, or ECR resources.

The default names are:

```text
{name_prefix}-{environment}-zoom-download
{name_prefix}-{environment}-jira-snapshot
{name_prefix}-{environment}-meeting-reconcile
{name_prefix}-{environment}-analysis-finalize
```

All projects set `concurrent_build_limit=1`.

## 4. Populate secrets outside Terraform

When a `*_secret_arn` variable is null, Terraform creates an empty shell but
no `aws_secretsmanager_secret_version`. Obtain its ARN:

```bash
terraform output -json secret_arns
```

Prepare secret JSON in a secure location outside the repository and call
`aws secretsmanager put-secret-value`. Exact shapes are in
[docs/configuration.md](configuration.md#secret-json):

- Zoom broker:
  `{"tokenUrl":"...","method":"POST","authorization":"...","headers":{}}`;
- Jira: `{"site":"...","email":"...","apiToken":"..."}`;
- Confluence: `{"email":"...","apiToken":"..."}` or a bearer token; and
- Graph:
  `{"tenantId":"...","clientId":"...","clientSecret":"..."}`.

If reusing a secret encrypted with a customer-managed KMS key, set the matching
`*_secret_kms_key_arn` so that only the intended CodeBuild role can decrypt it.

## 5. Point the retained inbound SharePoint sync at the bucket

Configure the existing producer to write only the versioned text plus
`metadata.json` sidecar contract under the prefixes allowlisted by
`inboundSharePointProfiles`. Grant that producer narrowly scoped `PutObject`
access. Terraform does not create, invoke, or grant credentials to the
external synchronization job; a cross-account producer may require a
separately reviewed bucket policy.

Write at least one representative item and confirm its sidecar, `contentKey`,
provenance, encoding, size, and `syncedAt` satisfy
[the inbound contract](contracts.md#inbound-sharepoint-to-s3-contract).

## 6. Configure and prove Confluence Automation

Create the rule in [`automation/README.md`](../automation/README.md) with the
same space and parent IDs as SSM configuration. Prove that:

- adding `status-ready-for-ai` triggers one execution;
- the agent analyzes the triggering page directly;
- the result title is exactly `MI result — {inputPageId}`;
- the result has `automation-ai-result`;
- its extracted body is one parseable JSON object; and
- replay cannot create a sequentially suffixed duplicate.

Also prove the reconciler revision protocol: an unchanged request reuses the
same `… — r{requestRevision}` input page without another analysis, while a
controlled instruction/context change creates a new revision page ID and one
independent result.

Do this before enabling `analysis-finalize`.

## 7. Run each project manually

Resolve and start projects:

```bash
for project in \
  zoom-download jira-snapshot meeting-reconcile analysis-finalize
do
  name="$(terraform output -json codebuild_project_names |
    jq -r --arg p "$project" '.[$p]')"
  aws codebuild start-build --project-name "$name"
done
```

Run them in that order, waiting for and inspecting each build. The initial
finalizer may correctly report no unfinished results.

Then run the same sequence again. The second pass must not create duplicate
MP4s, Transcribe jobs, input pages, result pages, clips, or final pages.
Inspect JSON summaries, remote IDs, object annotations, and Confluence
properties—not only the CodeBuild success state.

## 8. Enable schedules

After all proof gates pass:

```hcl
schedules_enabled = true
```

Run a new plan and apply. Default cadences are:

| Project | Schedule |
| --- | --- |
| `zoom-download` | `rate(30 minutes)` |
| `jira-snapshot` | `rate(30 minutes)` |
| `meeting-reconcile` | `rate(5 minutes)` |
| `analysis-finalize` | `rate(5 minutes)` |

Scheduler retries failed `StartBuild` delivery and sends exhausted
invocations to the shared DLQ. Scheduler success proves only that CodeBuild
accepted a start. Confirm subscriptions to the SNS build-failure topic and
test one alarm path.

## Tenant-level proof gates

Do not describe the deployment as ready until the target environment has
passed all of these:

1. **S3 annotations:** current CLI/SDK can get and conditionally put an
   annotation on a test object in the selected region; a wrong ETag fails.
2. **Zoom General OAuth:** broker returns a current token, a simulated expired
   token causes one refresh/retry, configured users are actually authorized,
   redirects work, and no token appears in logs.
3. **Split recording:** one occurrence with multiple completed MP4 segments
   produces every deterministic object and one identical expected-set
   fingerprint on each.
4. **Crash recovery:** stop runs after MP4 upload, Transcribe start, VTT write,
   page creation, one clip upload, and final-page creation; the next scheduled
   run repairs each state without duplicates.
5. **Transcribe:** normal speech, no speech, configured vocabulary, terminal
   failure, and retry attempt semantics all behave as documented.
6. **Jira:** a real board with zero, one, and multiple active sprints,
   site-specific story points, and a subtask-to-story-to-epic chain produces
   correct normalized data.
7. **Rovo:** the strict prompt produces raw JSON against the longest realistic
   input, the deterministic title/label association works, invalid JSON stays
   unfinished, and Automation service limits are acceptable.
8. **Finalization:** the longest expected recording and maximum configured
   demos fit local disk/timeout, timestamps are exact enough, both simple and
   resumable Graph uploads return stable IDs/URLs, and retry is idempotent.
9. **Inbound SharePoint:** every configured prefix contains only documented
   versioned text/provenance objects and the missing/stale policy is acceptable.
10. **Daily Brief (if configured):** all-team, deadline-partial, duplicate-team,
    and late-arrival behavior freezes the intended membership.

## Sizing and tool delivery

The finalizer defaults to `BUILD_GENERAL1_LARGE` and a four-hour timeout.
Increase `finalizer_compute_type` only after measuring the largest source plus
re-encoded outputs. Keep `finalization.maxDemos` and per-run scan limits
bounded.

Buildspecs download/install a current AWS CLI, Atlassian `acli`,
`markdown-to-confluence`, and FFmpeg into AWS-managed images. The current
proof-of-concept intentionally does not maintain a custom image. Before
production, pin versions and verify download integrity according to the
organization's supply-chain policy.

## Updating and rollback

1. Disable schedules for an incompatible contract migration.
2. Package the reviewed source and run `terraform plan`.
3. Apply the new source object version.
4. Run targeted manual builds and verify fingerprints/remote identities.
5. Re-enable schedules.

S3 versioning retains old source objects and workflow object versions. A
rollback is a Terraform change that points projects back to a known source
version plus compatible configuration. Do not roll code back across a
durable-schema change without its documented migration.

`artifact_bucket_force_destroy` defaults to false. Destroying the stack does
not authorize deletion of retained meeting data, Confluence pages,
SharePoint clips, or SaaS credentials. Inventory and preserve those systems
before any teardown.

## Useful outputs

```bash
terraform output artifact_bucket_name
terraform output source_bundle
terraform output config_parameter_name
terraform output -json codebuild_project_names
terraform output -json schedule_names
terraform output -raw scheduler_dlq_url
terraform output build_failure_topic_arn
```

Inspect Scheduler delivery failures:

```bash
aws sqs receive-message \
  --queue-url "$(terraform output -raw scheduler_dlq_url)" \
  --attribute-names All \
  --message-attribute-names All
```
