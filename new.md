# Technical Viability Assessment: Cron-Driven Alternative

Assessment date: 2026-07-23

## Conclusion

The proposed architecture is technically viable and is a materially simpler
fit for the scale described by this repository.

The event-driven implementation in [ARCHITECTURE.md](ARCHITECTURE.md) is
coherent, but the current requirements do not establish a need for a queue and
event envelope at every boundary, a package-construction state machine, two
DynamoDB coordination tables, or separate Lambda deployment units for each
small operation. Recurring, idempotent reconcilers can replace most of that
infrastructure while retaining deterministic processing and recovery.

The proposal should proceed with four required corrections:

1. **Use a small custom Zoom downloader, not DLZoom as-is.** The downloader
   must use the existing General OAuth component. None of the reviewed
   open-source tools combines General OAuth, all MP4 recording segments,
   one-shot execution, native CodeBuild credentials, and direct S3 output.
2. **Use S3 Object Annotations for mutable processing state.** Ordinary
   `x-amz-meta-*` metadata cannot be updated without copying the MP4. Object
   tags work as a fallback, but annotations are now the better native S3
   mechanism and remove the need for a manifest.
3. **Add one Confluence Automation rule to invoke Rovo.** An AWS cron job can
   find and retrieve Confluence pages, but publishing or labeling a page does
   not itself invoke a Rovo agent. Atlassian Automation is the documented
   autonomous execution surface.
4. **Use one post-Rovo finalization job.** The scheduled job should keep the
   analysis JSON on ephemeral disk, generate the clips in the same build,
   publish those clips directly to SharePoint, create the final Confluence page
   with the returned SharePoint references, and mark the result
   `status-analysis-done` last. Demo clips do not need to re-enter the recording
   pipeline.

With those corrections, the target can use:

- the retained General OAuth endpoint and Secrets Manager;
- the existing inbound SharePoint synchronization job plus direct outbound
  clip uploads;
- AWS-managed CodeBuild images, with tools downloaded at execution time;
- EventBridge Scheduler cron invocations;
- S3 objects and S3 Object Annotations as artifact and workflow state;
- Amazon Transcribe;
- Atlassian's official CLI for Jira;
- `markdown-to-confluence` (`md2conf`) for generated pages;
- one Confluence Automation/Rovo rule; and
- one scheduled analysis-finalization job with FFmpeg downloaded into its
  ephemeral CodeBuild worker.

No custom ECR repository, Step Functions workflow, application DynamoDB table,
custom event bus, or per-stage SQS queues are required for this model.

## Viability by component

| Component | Verdict | Required adjustment |
| --- | --- | --- |
| General OAuth | Viable as stated | Keep token refresh and validation in the owned OAuth component; do not introduce Server-to-Server OAuth. |
| Zoom download | Viable | Build a small one-shot Go HTTP client. Existing tools are not a clean fit. |
| SharePoint | Viable | Reuse the existing inbound sync. Give the finalizer a configured destination and write access for generated clips. |
| S3 transcription scan | Viable | Use S3 Object Annotations and a small reconciler, not mutable user metadata and not “metadata exists means done.” |
| Jira snapshots | Viable | Download Atlassian's official `acli` and aggregate its JSON with a small Python script. |
| Markdown publication | Viable | Use `md2conf`; put text needed by Rovo in the page body and preserve exact S3 association in a page property. |
| Rovo processing | Viable after correction | A Confluence Automation rule must invoke Rovo and persist `agentResponse`; CodeBuild retrieves that result into ephemeral storage. |
| Analysis-page creation | Viable | One finalizer generates clips, uploads them to SharePoint, publishes the final page, and applies the done marker last. |
| FFmpeg clipping | Viable | Run it inside the scheduled finalizer with sufficient local storage; clips do not become new pipeline inputs. |
| Runtime tool download | Viable | Use AWS-managed CodeBuild images and install/download the small tools during `install` or `pre_build`. |

## Recommended minimal topology

```text
                         General OAuth endpoint
                    (callback, validation, refresh)
                                  │
                                  ▼
                         AWS Secrets Manager
                                  │
                                  ▼
EventBridge Scheduler ───────▶ Scheduled CodeBuild jobs
                                  │
                 ┌────────────────┼─────────────────┐
                 │                │                 │
                 ▼                ▼                 ▼
        Zoom General OAuth   Atlassian CLI    Reconciliation jobs
                 │                │                 │
                 ▼                ▼                 │
             MP4s in S3      Jira snapshots         │
                 │                                  │
                 ├──── Amazon Transcribe ───▶ VTT ─┤
                 │                                  │
Existing SharePoint sync ──▶ synchronized assets   │
                                                    ▼
                                     Markdown input page in Confluence
                                                    │
                                                    ▼
                                     Confluence Automation invokes Rovo
                                                    │
                                                    ▼
                                      Labeled result page containing JSON
                                                    │
                                                    ▼
                                      Scheduled analysis finalizer
                                      ├─ JSON + S3 inputs on ephemeral disk
                                      ├─ FFmpeg creates demo clips
                                      ├─ clips uploaded to SharePoint
                                      ├─ returned IDs/URLs added to page
                                      └─ final Confluence page + done marker
```

For a proof of concept, four CodeBuild projects are a reasonable balance:

| Project | Suggested cadence | Responsibility |
| --- | --- | --- |
| `zoom-download` | Every 30 minutes | Discover completed Zoom recordings and stream missing MP4s to S3. |
| `jira-snapshot` | Every 30 minutes | Capture active-sprint JSON and simple derived views. |
| `meeting-reconcile` | Every 5 minutes | Reconcile transcription and publish ready Rovo input pages. |
| `analysis-finalize` | Every 5 minutes | Find unfinished Rovo results, download their JSON and S3 inputs to disk, create and upload clips, publish the final page, and mark the analysis done. |

The projects can share one small source bundle. They do not need a shared
runtime service or a custom image. If minimizing the number of CodeBuild
projects is more important than keeping IAM permissions separate,
`zoom-download`, `jira-snapshot`, and `meeting-reconcile` can be different
commands in one project. `analysis-finalize` is worth keeping separate because
it has different storage, duration, Microsoft write access, and compute needs.

Each scheduled command should be bounded: inspect current state, advance work,
and exit. A CodeBuild worker should not remain running while Transcribe or Rovo
finishes.

## S3 without manifests

### Use Object Annotations, not ordinary user metadata

Amazon introduced
[S3 Object Annotations](https://docs.aws.amazon.com/AmazonS3/latest/userguide/annotations-overview.html)
in June 2026. They attach structured JSON, YAML, or XML directly to an object,
can be replaced without copying the object, and are intended for workflow and
processing context. They are available in all AWS Regions, including the
repository's default `ca-central-1` region. This capability directly satisfies
the requirement to keep state on the MP4 without a separate `manifest.json`.

Ordinary `x-amz-meta-*` user metadata is the wrong mechanism. Once an object is
uploaded, changing that metadata requires copying the object onto a new object
or version. Recopying large MP4s merely to record a state transition is slow
and changes object properties.
[AWS documents this copy requirement](https://docs.aws.amazon.com/AmazonS3/latest/userguide/add-object-metadata.html).

S3 object tags are a usable fallback, but an object has a maximum of ten tags,
and `PutObjectTagging` replaces the whole tag set. Concurrent components would
have to read, merge, and replace each other's state. Separate annotations are
cleaner because each stage can own its annotation name.
[AWS documents the object-tag limits and replacement behavior](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-tagging.html).

Recommended annotation ownership:

| Annotation | Writer | Purpose |
| --- | --- | --- |
| `mi.source` | Zoom downloader | Source IDs, occurrence ID, timestamps, and expected recording files. |
| `mi.transcription` | Transcription reconciler | Job identity, configuration fingerprint, state, attempt, VTT key, and update time. |
| `mi.confluence` | Input-page publisher | Input page ID/version and publication state before Rovo processing. |

These annotations cover the S3-side work that precedes Rovo. Post-Rovo
completion is recorded on the Confluence result page, not as another S3 object
or annotation.

For example:

```json
{
  "v": 1,
  "kind": "zoom-recording",
  "occurrenceId": "payments-daily-2026-07-23T130000Z",
  "meetingUuid": "...",
  "recordingFileId": "...",
  "expectedMp4FileIds": ["...", "..."],
  "recordingSetFingerprint": "sha256:...",
  "recordedAt": "2026-07-23T13:00:00Z",
  "durationSeconds": 1827
}
```

Repeating the small expected-file list and its fingerprint on each MP4 replaces
the current “manifest written last” completeness signal. The publisher waits
until every expected MP4 exists, the set fingerprints agree, and each object
has a terminal transcription state. This retains support for Zoom split
recordings without creating a separate occurrence manifest.

The annotation write should use S3's ETag precondition so state is not attached
to an MP4 that was replaced after the scan. The current AWS CLI and SDKs expose
`put-object-annotation`, `get-object-annotation`, and
`list-object-annotations`; an older managed CodeBuild image may require
installing a current AWS CLI or Python SDK during `pre_build`.
[AWS documents the annotation operations and ETag condition](https://docs.aws.amazon.com/AmazonS3/latest/userguide/annotations-managing.html).

### Discovery remains intentionally simple

[`ListObjectsV2`](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectsV2.html)
does not return annotations and returns at most 1,000 keys per page. A
reconciler therefore:

1. paginates objects under the configured active prefix;
2. filters `.mp4` keys;
3. retrieves the stage's annotation for each candidate; and
4. performs work for absent, stale, or non-terminal state.

This is a linear scan. It is entirely reasonable for the described number of
teams and meetings. If the corpus later becomes large enough for scanning cost
or latency to matter, prefix partitioning or S3 Metadata tables can be added
then. Adding an event framework now solely to avoid this modest scan would
conflict with the stated simplicity goal.

Set each reconciler's CodeBuild concurrency to one where practical. Correctness
still comes from deterministic object keys, deterministic external job names,
and state reconciliation, not from assuming cron invocations occur exactly
once.

## Zoom downloader

### Existing open-source tools

No reviewed tool can be adopted unchanged:

- [DLZoom](https://github.com/yaniv-golan/dlzoom) has a user-scoped General
  OAuth mode, but its account-scoped mode is Server-to-Server only. It persists
  a local token file, expects its own refresh arrangement, has no S3 output,
  and
  [prefers an M4A recording over MP4 when both exist](https://github.com/yaniv-golan/dlzoom/blob/df363935cbb9ac76a2b9342bf167bb87b8d66112/src/dlzoom/handlers.py#L1329-L1389).
  Adapting all of those behaviors would remove most of the value of reusing it.
- [`zoomdl`](https://github.com/jobstoit/zoomdl) supports S3 output and recording
  type selection, but it is built around Server-to-Server authentication, runs
  continuously, and keeps a local saved-records JSON file. Its S3
  authentication also does not cleanly match CodeBuild's temporary role
  credentials.
- [`zoom-cli`](https://github.com/rvben/zoom-cli) is also built around
  Server-to-Server authentication.

The best fit is therefore a small custom one-shot Go command. Zoom does not
publish a Go REST client SDK for this API; use `net/http` for Zoom and the AWS
SDK for Go v2 for S3. This should be a small integration, not a new service
framework. The implementation should target Zoom's
[recording REST API](https://developers.zoom.us/docs/api/meetings/) and the
permissions granted to the
[General app](https://developers.zoom.us/docs/integrations/create/).

### Minimum behavior

The command should:

1. request a current access token from the owned General OAuth component;
2. query the authorized user's recordings, or configured users when the
   General app and authorization grant the required admin scopes;
3. paginate the recording responses;
4. accept only completed MP4 files and preserve every split segment;
5. derive the occurrence ID and deterministic S3 key from stable Zoom IDs;
6. use `HeadObject` plus the source annotation to skip a completed upload;
7. download each artifact using a bearer token and follow Zoom redirects;
8. stream the response through the AWS SDK multipart uploader into S3; and
9. write `mi.source` after the upload, repairing a missing annotation on the
   next run if the process fails between those operations.

The CodeBuild job should not own or race refresh-token rotation. It should ask
the OAuth component for a valid access token and retry that exchange after an
authorization failure. If multiple hosts must be covered, the selected
General app must either be admin-managed with suitable scopes or each host
must authorize the app; a user-managed authorization does not implicitly
grant access to every account user's recordings.

The source or a static binary can live in S3. CodeBuild can download and
compile it using an
[AWS-managed Go runtime](https://docs.aws.amazon.com/codebuild/latest/userguide/runtime-versions.html),
then execute it. No maintained ECR image is needed.

## Transcription reconciler

The proposed transcription flow is viable, but “transcription metadata
exists, skip” is too weak. An annotation can mean submitted, failed, complete,
or complete under an old configuration. Skip only when the annotation says
complete, its source fingerprint and configuration match, and the expected VTT
actually exists.

A minimal `mi.transcription` payload is:

```json
{
  "v": 1,
  "cfg": "en-us-default-v1",
  "job": "mi-4c7f...",
  "attempt": 0,
  "state": "submitted",
  "vtt": "meetings/.../recording-file-id.vtt",
  "updated": "2026-07-23T12:34:56Z"
}
```

Use a deterministic job name based on the bucket, key, object ETag or version,
transcription configuration, and attempt:

```text
mi-<sha256(bucket, key, source-fingerprint, configuration, attempt)>
```

Each run should:

1. discover candidate MP4s and read `mi.transcription`;
2. verify the sibling VTT before trusting a `complete` state;
3. call `GetTranscriptionJob` using the deterministic name;
4. call `StartTranscriptionJob` only when that job does not exist;
5. record `submitted` and exit for queued or in-progress work;
6. on completion, use `SubtitleFileUris`, place the VTT at the canonical
   sibling key, and put a reverse source annotation on the VTT;
7. mark the MP4 complete only after verifying the VTT; and
8. record a failed terminal state rather than retrying forever.

This ordering repairs the important crash windows. If starting Transcribe
succeeds but writing the annotation fails, the next cron run finds the
deterministically named job. If the VTT is written but the final annotation
fails, the next run verifies the output and completes the state. Transcribe job
names are unique, so a duplicate start returns a conflict rather than creating
a second job.
[AWS documents these `StartTranscriptionJob` semantics](https://docs.aws.amazon.com/transcribe/latest/APIReference/API_StartTranscriptionJob.html).

Amazon Transcribe supports MP4 input and WebVTT output. It also writes its
normal JSON transcript when subtitles are requested. A successful recording
with no speech does not produce a subtitle file, so the reconciler must either
write a valid empty VTT sentinel or set `complete_no_speech` and teach the
publisher that this is terminal. Otherwise that meeting waits forever.
[AWS documents the subtitle and no-speech behavior](https://docs.aws.amazon.com/transcribe/latest/dg/subtitles.html).

This logic will not realistically remain below 30 lines once pagination,
deterministic jobs, annotations, failure states, output verification, and the
no-speech case are included. A small Python CodeBuild command of roughly
100–200 straightforward lines is a more honest implementation than optimizing
for an arbitrary Lambda line count.

## Jira snapshot generation

This portion of the proposal is directly viable using Atlassian's official
`acli`. Atlassian explicitly documents
[downloading and authenticating the CLI in CI](https://developer.atlassian.com/cloud/acli/guides/use-acli-on-ci/).
The CLI supports:

- listing active sprints for a board;
- listing the work items in a sprint with pagination and JSON output; and
- arbitrary JQL searches with JSON output.

Relevant commands are documented under
[`jira board list-sprints`](https://developer.atlassian.com/cloud/acli/reference/commands/jira-board-list-sprints/),
[`jira sprint list-workitems`](https://developer.atlassian.com/cloud/acli/reference/commands/jira-sprint-list-workitems/),
and
[`jira workitem search`](https://developer.atlassian.com/cloud/acli/reference/commands/jira-workitem-search/).

A small Python script can normalize only the fields the report uses and
produce:

- totals by status or status category;
- counts and estimates by epic;
- unassigned or blocked work;
- selected JQL perspectives; and
- a timestamped raw-plus-derived snapshot JSON.

One full sprint export followed by local filtering produces a more internally
consistent snapshot than several API calls taken seconds apart. Additional JQL
queries remain useful when the alternate perspective cannot be derived from
the exported fields.

The implementation must account for three site-specific details without
introducing a large model layer:

- Jira can have more than one active sprint when parallel sprints are enabled;
- the story-points custom-field ID varies by site; and
- a subtask's immediate parent may be a story rather than the epic, requiring
  one extra parent resolution.

At publication time, select the most recent eligible snapshot at or before the
meeting start, then write or reference it under that meeting's deterministic
S3 prefix.

## Markdown publication to Confluence

[`markdown-to-confluence` / `md2conf`](https://github.com/hunyadi/md2conf) is a
strong fit. It installs with `pip`, converts Markdown to Confluence storage
format, creates or updates pages, and supports labels and content properties
through front matter.

The generated source can be ephemeral:

```yaml
---
title: "Meeting — payments-daily-2026-07-23T130000Z"
tags:
  - automation-ai-request
  - status-ready-for-ai
  - recording-a41c92e7
properties:
  meeting-intelligence:
    occurrenceId: payments-daily-2026-07-23T130000Z
    bucket: meeting-intelligence-dev
    sourcePrefix: meetings/payments-daily-2026-07-23T130000Z/
---
```

Use labels for workflow discovery and a namespaced content property for the
exact source association. The publisher should also write the resulting page
ID into `mi.confluence` on the source MP4s. A deterministic title under a
fixed, trusted parent lets a retry rediscover the page if the page was created
but the annotation write failed.

“Publish everything” needs one qualification. Content Rovo must analyze should
be rendered into the page body:

- transcript text, not only an attached VTT;
- the selected Jira state and useful aggregates, not only a private S3 URI;
- meeting metadata and instructions; and
- relevant synchronized SharePoint text, when required.

The MP4 can remain in S3 and be linked or separately attached for human use.
Merely attaching an MP4 or placing a private `s3://` reference in the page does
not establish that Rovo can use its audiovisual content. `md2conf` handles
Markdown and referenced images; arbitrary MP4/VTT/JSON attachment upload may
need a few direct Confluence REST calls.

`md2conf` front-matter tags replace the page's existing labels. That is
acceptable if the input page is system-owned and not republished after
Confluence Automation adds state labels. Otherwise omit tags during updates
and append labels with the Confluence REST API.

## Rovo processing

This is the only part of the proposed flow that is not viable as written.

A CodeBuild job can query Confluence by label and retrieve page content, but it
cannot assume that a labeled or indexed page has been processed by Rovo.
Atlassian documents autonomous Rovo execution through
[Automation rules](https://support.atlassian.com/rovo/docs/agents-in-automations/).
In an automation flow, the agent returns text through `{{agentResponse}}`; it
cannot use its own page-creation tools, so a subsequent Automation action must
persist that response.
[Atlassian documents this agent-tool limitation](https://support.atlassian.com/rovo/docs/agent-actions/).

The smallest working arrangement is:

```text
CodeBuild publishes an input page
    labels: automation-ai-request, status-ready-for-ai, recording-<id>
    property: exact S3/occurrence association
        ↓
Confluence Automation rule
    trigger: matching page publication/label, or a scheduled rule
    action: Use Rovo agent with a strict-JSON prompt
    next action: create a result page containing {{agentResponse}}
    result labels: automation-ai-result, recording-<id>
        ↓
Scheduled CodeBuild analysis finalizer
    query result pages without status-analysis-done
    retrieve JSON and source association to ephemeral disk
    generate and publish clips, then publish the final page
    add status-analysis-done only after the entire operation succeeds
```

A fixed result landing page is simpler and more reliable than requiring every
result to be a dynamic child of its input page. Include the input page ID in a
deterministic result title, for example `MI result — 123456`, then have the
finalizer read the source page's content property. If a true child page is a
hard requirement, prove dynamic parent selection in a tenant POC or have
Automation call the Confluence REST API with the triggering page ID.

Comments are possible, but a result page is a better workflow object: it has a
page ID and version, supports labels, is easy to poll, and avoids mixing
machine JSON with discussion.

There is no documented “Rovo index ready” status or deterministic indexing
delay. If all meeting text is placed in the triggering page, the Automation
rule can ask the agent to analyze that page directly and avoid depending on
global search indexing. If broader knowledge search is required, use a
scheduled Automation rule with a minimum page age and retry; treat the delay as
a heuristic.

The Confluence result page remains the durable source of the analysis JSON. The
finalizer downloads it only into the CodeBuild workspace and does not copy it
to S3. Keep the result page and its version so the operation remains auditable
and repeatable.

The finalizer should not attempt to repair semantically invalid AI output. It
can remain small, but at least `JSON.parse` and validation of the fields that
will drive FFmpeg are required.

## Single-pass analysis finalization

The Rovo result needs one small, explicit contract. A full shared schema
framework is unnecessary, but arbitrary JSON cannot safely drive FFmpeg. For
example:

```json
{
  "demos": [
    {
      "sourceRecordingId": "recording-file-id",
      "startSeconds": 12.3,
      "endSeconds": 47.8,
      "title": "Checkout flow"
    }
  ]
}
```

The finalizer must check that:

- the source recording exists and belongs to the associated occurrence;
- `startSeconds` is non-negative;
- `endSeconds` is greater than `startSeconds`;
- the interval does not exceed the recording duration; and
- each generated SharePoint destination name is deterministic.

For each unfinished result page, one CodeBuild execution should:

1. download the result-page JSON and its page version into the ephemeral
   workspace;
2. use the input-page association to download the source MP4 from S3;
3. download any additional inputs needed for the final report, such as the
   applicable Jira snapshot, meeting metadata, or synchronized SharePoint
   context;
4. parse and minimally validate the analysis JSON;
5. generate all requested clips locally with FFmpeg;
6. upload each clip directly to a configured SharePoint document library and
   retain the returned item identifiers and browser URLs;
7. render the final Markdown from the analysis, SharePoint clip references,
   and any additional S3 data;
8. create or repair the deterministic final Confluence analysis page; and
9. add the `status-analysis-done` label and completion property to the Rovo
   result page last.

There is no post-Rovo analysis JSON object or demo-clip object in S3. The S3
recording and Jira data are inputs to the finalizer; the Rovo result page is
the analysis source of truth; SharePoint is the clip destination; and the
final Confluence page is the completed report.

Use a deterministic SharePoint folder and filename such as:

```text
Meeting Intelligence/{occurrence-id}/
  {source-id}-{start-ms}-{end-ms}-r{result-page-version}.mp4
```

The result-page version prevents a revised analysis from silently replacing
clips created by an older analysis. A retry of the same version should resolve
the existing file by its deterministic path and reuse or replace it instead of
creating a renamed duplicate.

Stream copying (`-c copy`) is quick but can cut only at suitable keyframe
boundaries. Re-encoding is required when the requested timestamps must be
exact. The CodeBuild project therefore needs enough local disk and timeout for
the largest supported recording.

Microsoft Graph returns a SharePoint file as a `driveItem`, including its
stable item ID and browser `webUrl`; it can also expose SharePoint-specific
IDs. Those are the values the Markdown renderer should place into the final
page, rather than a temporary upload URL.
[The `driveItem` resource documents these identifiers](https://learn.microsoft.com/en-us/graph/api/resources/driveitem?view=graph-rest-1.0).

For clips up to 250 MB, Graph supports a single content upload. Larger files
should use a resumable upload session.
[Microsoft documents both the simple upload limit](https://learn.microsoft.com/en-us/graph/api/driveitem-put-content?view=graph-rest-1.0)
and
[large-file upload sessions](https://learn.microsoft.com/en-us/graph/api/driveitem-createuploadsession?view=graph-rest-1.0).
This is an outbound SharePoint operation, so the finalizer needs a configured
site, drive, folder, and credential with write permission; the existing
read-oriented synchronization job does not by itself prove that capability.

Use one Confluence content property on the Rovo result page as the exact
completion receipt:

```json
{
  "state": "done",
  "resultVersion": 7,
  "finalPageId": "987654",
  "sharePointClips": [
    {
      "driveId": "...",
      "itemId": "...",
      "webUrl": "https://..."
    }
  ],
  "completedAt": "2026-07-23T15:42:00Z"
}
```

The label is convenient for discovery; the property holds the exact completion
data. Both are written only after the final Confluence page is confirmed.

This single final marker makes partial failures recoverable without another
scheduled stage. A crash after uploading one clip leaves the result unfinished;
the next build resolves that deterministic SharePoint item and continues. A
crash after creating the final page but before setting the marker causes the
next build to rediscover the deterministic page and finish the marker. If the
analysis requests no clips, the same job simply publishes the final page and
marks it done.

Generated clips never become Zoom-style source recordings and never re-enter
transcription or Rovo processing, so no recursion guard or additional clip
schedule is needed.

## What remains from the current domain model

Replacing the transport framework does not mean discarding every contract in
the repository. The following behavior still has product value and should be
kept as small local checks:

- deterministic occurrence, recording, Transcribe-job, page, SharePoint path,
  and clip IDs;
- preservation of all Zoom split recording segments;
- parsing and validation of the Zoom key-value configuration;
- quarantine or explicit failed state for invalid routing metadata;
- selection of the Jira snapshot relative to meeting time;
- source/version provenance in S3 annotations, SharePoint item references, and
  Confluence properties;
- idempotent Confluence page rediscovery after a partial failure; and
- minimal validation of Rovo JSON before using it as executable input.

The current design's generic event envelopes, SQS parsing, partial-batch
responses, DynamoDB status machine, and schemas for every internal handoff can
be removed. A few small data shapes at external boundaries remain necessary.

The proposal also omits the current multi-meeting Daily Brief aggregation
behavior. If that is still a requirement, add a bounded aggregation pass to
`meeting-reconcile`: group eligible completed occurrences by business date,
publish when all configured teams are present or the configured local-time
deadline passes, and record the chosen membership in the aggregate S3 object
or a designated source annotation. This does not require Step Functions,
DynamoDB, or an event bus, but the completeness/deadline rule cannot simply be
deleted without changing product behavior.

The existing SharePoint synchronization job covers inbound source content. A
meeting still needs a small rule—such as a `sharepointProfile` in
`mi.source`—that selects which synchronized content is placed in its
Confluence input page. The finalizer separately needs an outbound destination
profile containing the target site, document library, and folder for clips.

## Runtime delivery without custom ECR

CodeBuild always runs in an image, but it can use an AWS-maintained standard
image. The project does not need to own an image or ECR lifecycle.

At execution time the projects can:

- obtain the small application source or a static internal binary from S3;
- compile the Go Zoom command using the managed Go runtime;
- download the latest `acli` Linux binary;
- install `markdown-to-confluence` and a current AWS Python SDK with `pip`; and
- download a static FFmpeg build or install FFmpeg in the build environment;
  and
- call Microsoft Graph directly for the small or resumable SharePoint upload.

The repository or S3 source ZIP should contain the short Python/Go programs,
templates, and buildspec. This is “package, download, run” without maintaining
a long-lived application container.

This assessment intentionally follows the proposal's proof-of-concept scope:
tool pinning, download verification, dependency caching, and broader security
hardening are not design prerequisites here. They can be added later without
changing the cron-and-reconciliation architecture.

## Revised end-to-end flow

```text
General OAuth authorization/refresh
    ↓
Scheduled custom Zoom downloader
    ↓
S3 MP4 + mi.source annotation
    ↓
Scheduled Transcribe reconciler
    ↓
S3 MP4 + VTT + stage annotations
    ↓
Scheduled Jira snapshot + existing SharePoint sync
    ↓
Scheduled meeting reconciler selects the applicable source data
    ↓
Markdown publisher creates/repairs a labeled Confluence input page
    ↓
Confluence Automation invokes Rovo and persists its JSON response
    ↓
Scheduled analysis finalizer
    ├─ pulls analysis JSON and required S3 inputs to ephemeral disk
    ├─ uses FFmpeg to generate all demo clips
    ├─ uploads clips to SharePoint and captures item IDs/URLs
    ├─ publishes the final Confluence page from all collected data
    └─ marks the Rovo result status-analysis-done last
```

## Recommended proof-of-concept sequence

1. Prove S3 annotation access from the selected CodeBuild managed image and
   region.
2. Implement the one-shot General-OAuth Zoom downloader for one authorized
   user and a split-recording fixture.
3. Implement the Transcribe reconciler, including crash recovery and the
   no-speech outcome.
4. Download `acli`, export one real active sprint, and generate the minimal
   JSON views used by the page.
5. Publish one deterministic Markdown page with labels and an association
   property using `md2conf`.
6. Configure one Confluence Automation rule that invokes Rovo and persists a
   strict JSON result page.
7. Have `analysis-finalize` pull that JSON to disk, generate one deterministic
   clip, upload it to SharePoint, use the returned reference in the final
   Confluence page, and mark the result done.
8. Rerun every job and confirm that no duplicate Transcribe jobs, S3 objects,
   SharePoint clips, or Confluence pages are created.

If those steps pass with the longest realistic transcript and recording, the
alternative is not merely possible; it is the preferred architecture for the
stated simplicity-first goals.
