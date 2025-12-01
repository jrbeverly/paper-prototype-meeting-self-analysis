# Operations and Limitations

This proof of concept favors a small, inspectable reconciliation loop over a
general workflow platform. The trade-offs below are part of the design and
must be accepted or changed deliberately before production use.

## Monitoring

Monitor both invocation delivery and build outcomes:

- EventBridge Scheduler failed-invocation count and the shared Scheduler DLQ;
- CodeBuild `FAILED`, `FAULT`, `STOPPED`, and `TIMED_OUT` state changes for
  each project;
- build duration approaching the configured timeout;
- S3 scan/object/annotation failures;
- Amazon Transcribe failed jobs and service quota pressure;
- Confluence Automation rule failures and result age;
- Microsoft Graph throttling/upload-session failures; and
- unfinished Rovo results older than the operational objective.

Scheduler reports success when `StartBuild` is accepted, not when the build
finishes. Build-state alarms are therefore mandatory. Logs should include
occurrence ID, recording file ID, external job/page/item IDs, and state
transitions, but never bearer tokens, secret JSON, transcript bodies, or Rovo
result bodies.

The commands emit a machine-readable summary and use non-zero exit status when
operational work failed. For `zoom-download`, invalid meeting metadata is
quarantined and does not itself make an otherwise successful scan fail;
configuration errors exit with status 2, while API/upload/annotation failures
exit with status 1 after other occurrences have been attempted.

## Concurrency

Start with a concurrent-build limit of one for each project.

- Serializing `zoom-download` avoids simultaneous work against one shared
  General OAuth token set.
- Serializing the two reconcilers reduces external API pressure and avoids
  unnecessary duplicate work.

These are load controls, not correctness guarantees. Conditional S3
annotations, deterministic external names, remote-object rediscovery, and
last-written completion receipts remain required because Scheduler and manual
starts are at least once.

## Scale limits

`ListObjectsV2` is paginated, but an S3 listing does not include Object
Annotations. A reconciliation pass therefore performs an object listing plus
annotation reads for candidate MP4s. Restrict `activePrefix` to current
recordings and use targeted prefixes for historical replay.

The design is appropriate for the stated five-team proof of concept. If scan
latency or annotation request cost grows materially, partition by active date
or add an index such as S3 Metadata tables. Do not add a queue framework merely
because a single page contains 1,000 keys; pagination is already required.

`transcription.maxItemsPerRun` bounds work started/observed in one build.
`finalization.maxDemos` bounds AI-driven clip count. Add account-specific
budgets for total recording duration, clip duration, Confluence body size, and
SharePoint storage before production.

For a configured team, `jira.required=true` (the default) defers input-page
publication until a snapshot at or before the meeting exists. With
`required=false`, the page explicitly renders Jira status `missing`; it never
mislabels an expected snapshot as unconfigured.

## S3 Object Annotations

The selected region, account, SDK/CLI version, and CodeBuild image must support
`get-object-annotation` and `put-object-annotation`. The Python adapter uses a
current boto3 model when available and falls back to a current AWS CLI. The Go
downloader invokes the configured AWS CLI.

The implementation:

- stores JSON objects no larger than the S3 annotation limit;
- conditions writes on the observed object ETag;
- treats a precondition failure as a rescan, not permission to overwrite; and
- uses separate annotation names so stages do not replace each other's state.

Ordinary `x-amz-meta-*` metadata and S3 tags are not compatible substitutes
for this implementation.

## Amazon Transcribe

- A build starts or observes a job and exits; completion is seen on a later
  run.
- Job identity includes the source object, source/configuration fingerprints,
  and attempt number.
- `FAILED` is terminal for an attempt. Automatic retries require the explicit
  `transcription.retryFailed` setting and create a new name.
- A completed job with no subtitle URI produces a valid empty VTT sentinel and
  terminal `complete_no_speech`.
- A `complete` annotation is trusted only when its dependency fingerprints
  match and the sibling VTT exists.

Language selection is configured globally with an optional per-meeting
vocabulary. Automatic language identification and speaker diarization are not
provided unless added to the explicit transcription configuration contract.

## Confluence and Rovo

- The Automation rule in [`automation/README.md`](../automation/README.md) is a
  required deployed component.
- Rovo must analyze the triggering page directly. There is no documented
  deterministic global-index readiness signal.
- Input/result/final parents are trusted configured IDs. Moving pages or
  allowing humans to create pages with reserved revision/result titles can
  create ambiguity and quarantine an item. Multiple input pages for the same
  occurrence are normal only when their `requestRevision` suffixes differ.
- Labels are for discovery. Content properties carry associations and
  completion receipts.
- AI output is untrusted. Invalid JSON remains unfinished for operator
  correction; the finalizer does not guess timestamps or source recordings.
- Confluence Cloud APIs and Rovo Automation entitlements, actor permissions,
  action limits, and tenant rate limits must be proven in the target tenant.

## FFmpeg and SharePoint output

Exact timestamps require re-encoding rather than stream copying. Size the
finalizer's ephemeral disk, memory, compute class, and timeout for the largest
expected source plus all generated outputs.

Files at or below `finalization.simpleUploadLimitBytes` use a direct Graph
content upload; larger clips use an upload session with
`finalization.uploadChunkBytes`, which must be a multiple of 320 KiB. A retry
must address the deterministic path; it must not accept an automatically
renamed collision.

The Graph application needs write permission to one configured site/drive/
folder. This is separate from the existing inbound SharePoint synchronization
job. Returned stable item IDs and browser URLs are persisted in the result
receipt and final page.

## Inbound SharePoint context

This repository does not synchronize source documents from SharePoint. It
consumes text objects already written to allowlisted S3 prefixes by an
existing job and discovered through strict `metadata.json` sidecars.
Binary-only documents, objects without the documented provenance sidecar,
stale objects outside profile policy, and inaccessible prefixes are not
silently included. A required profile with no valid current item defers the
input page.

The publisher places selected text in the Confluence input body so Rovo can
use it. An `s3://` link or a binary attachment alone is not considered
analysis context.

## Product omissions

- The prior Lambda/SQS/EventBridge/Step Functions/DynamoDB implementation is
  intentionally removed.
- This repository does not host the interactive Zoom OAuth callback,
  deauthorization endpoint, or refresh-token store. It calls the existing
  broker contract.
- This repository does not own inbound SharePoint synchronization.
- Generated clips do not re-enter S3 ingestion or transcription.
- Daily Brief aggregation is optional. Its membership is frozen by a
  conditional S3 receipt; late arrivals do not automatically republish it.
- Zoom `artifact_class` is preserved as routing/provenance, but the Terraform
  proof of concept does not install temporary/permanent retention lifecycle
  rules. Define and prove retention separately before storing production
  recordings.

## Production-readiness disclaimer

Unit tests and static Terraform checks do not prove SaaS behavior. This
implementation has not been deployed or end-to-end tested against a live AWS
account, Zoom General app, Atlassian/Rovo tenant, or Microsoft tenant as part
of this work. Complete the proof gates in
[docs/deployment.md](deployment.md#tenant-level-proof-gates) before describing
the platform as production-ready.
