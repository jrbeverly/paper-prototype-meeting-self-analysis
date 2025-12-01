# Meeting Intelligence

> [!WARNING]
> **AI-authored:** This change was autonomously planned and implemented by an AI software factory from a human-authored specification, with possible subsequent human review or modification.

> [!WARNING]
> This experiment is effectively abandoned. The generated material is retained primarily as a research artifact.

A cron-driven proof of concept that turns Zoom recordings and operational context into Confluence meeting reports. Four bounded commands run in AWS-managed CodeBuild images:

```text
Zoom General OAuth broker ──▶ zoom-download ──▶ MP4 + mi.source in S3
Jira ───────────────────────▶ jira-snapshot ──▶ snapshots in S3
S3 + Transcribe + Confluence ▶ meeting-reconcile ──▶ Rovo input page
Rovo result page ────────────▶ analysis-finalize ──▶ SharePoint clips
                                                     + final Confluence page
```

EventBridge Scheduler starts the commands periodically. They inspect durable state, advance work that is ready, and exit. There is no long-running worker, custom EventBridge bus, Step Functions workflow, application DynamoDB table, per-stage SQS queue, or custom ECR image.
