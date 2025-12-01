# Architecture

This is the canonical architecture for the implementation. It follows
[new.md](new.md), the revised technical vision. [VISION.md](VISION.md) records
the earlier product vision and event-driven design; its queues, event
envelopes, Step Functions state machine, DynamoDB coordination tables, and
per-operation Lambdas are not part of the current system.

Binding payload and external-system interfaces live in
[docs/contracts.md](docs/contracts.md).

## Design in one sentence

Four scheduled, idempotent commands reconcile external systems against S3 and
Confluence state, using deterministic identifiers so a later run can safely
continue after a crash.

```text
                         owned General OAuth broker
                                    │
                                    ▼
EventBridge Scheduler ───────▶ AWS-managed CodeBuild
                                    │
                    ┌───────────────┼────────────────┐
                    │               │                │
                    ▼               ▼                ▼
              Zoom recordings   Jira via acli   reconciliation
                    │               │                │
                    ▼               ▼                │
              MP4 objects      snapshot JSON         │
                    │                                │
                    ├──── Amazon Transcribe ─▶ VTT ─┤
                    │                                │
existing inbound SharePoint sync ─▶ text context ───┤
                                                     ▼
                                      Confluence input page
                                                     │
                                      Automation invokes Rovo
                                                     │
                                                     ▼
                                  deterministic JSON result page
                                                     │
                                                     ▼
                                        analysis finalization
                                        ├─ FFmpeg in ephemeral disk
                                        ├─ clips sent to SharePoint
                                        ├─ final Confluence page
                                        └─ done receipt written last
```

The runtime uses AWS-managed CodeBuild images. Tool installation happens at
build time; no repository-owned ECR image is required.

## The four bounded commands

| Command | Normal cadence | Responsibility |
| --- | --- | --- |
| `zoom-download` | 30 minutes | Discover completed Zoom MP4 segments and stream missing objects to S3 using a token obtained from the General OAuth broker. |
| `jira-snapshot` | 30 minutes | Export active-sprint work with Atlassian `acli`, normalize the fields used in reports, and write timestamped snapshots. |
| `meeting-reconcile` | 5 minutes | Reconcile transcription, validate complete occurrences, select contextual data, and publish or repair Confluence input pages. |
| `analysis-finalize` | 5 minutes | Validate unfinished Rovo results, generate clips, upload them to SharePoint, publish the final page, then write the completion marker. |

A command must inspect state, make bounded progress, and exit. It must not keep
a build worker open while waiting for Transcribe or Rovo.

## Durable state

### S3 source and enrichment data

The artifact bucket contains:

```text
meetings/{occurrence-id}/
  {recording-file-id}.mp4
  {recording-file-id}.vtt

jira/{team}/snapshots/{slot}.json

{configured inbound SharePoint text prefixes}
  ... content objects ...
  ... metadata/provenance objects ...
```

The exact SharePoint prefix is deliberately configuration-owned because the
inbound synchronizer is outside this repository. Its required object contract
is fixed in [docs/contracts.md](docs/contracts.md#inbound-sharepoint-to-s3-contract).

There is no occurrence `manifest.json`. Mutable stage state is stored in
separate S3 Object Annotations:

| Annotation | Owner | Meaning |
| --- | --- | --- |
| `mi.source` | `zoom-download` | Stable Zoom IDs, occurrence metadata, complete expected MP4 set, and its fingerprint |
| `mi.transcription` | `meeting-reconcile` | Deterministic job/configuration identity, attempt, state, VTT key, and update time |
| `mi.confluence` | `meeting-reconcile` | Associated input-page ID/version and publication state |

An annotation write is conditional on the object ETag observed during the
scan. A changed MP4 cannot accidentally receive state computed for an older
object.

### Confluence workflow state

Confluence owns the post-Rovo record:

1. The reconciler creates or rediscovers a revision-addressed input page under
   a trusted parent. The page body contains all text Rovo needs, not merely
   private S3 links. Labels support discovery; a content property records the
   exact occurrence, source prefix, associations, and request revision. The
   same rendered request reuses its page; changed transcript, context,
   association, or instructions produce a new revision title and page ID.
2. Confluence Automation runs the named Rovo agent against that page and
   persists `{{agentResponse}}` in the deterministic result page
   `MI result — {inputPageId}`.
3. The finalizer treats that page and its version as the durable analysis
   source. It does not copy the analysis JSON into S3.
4. After every clip and the final page are confirmed, the finalizer writes a
   `meeting-intelligence-completion` content property and adds
   `status-analysis-done` to the result page. The property is the exact
   receipt; the label is a discovery aid.

### SharePoint output state

Generated clips go directly to:

```text
Meeting Intelligence/{occurrence-id}/
  {source-recording-id}-{start-ms}-{end-ms}-r{result-page-version}.mp4
```

The result-page version prevents a revised analysis from silently overwriting
clips produced from an earlier result. A retry resolves the deterministic
path and reuses or replaces that version rather than accepting a collision
rename. The final page records stable Graph `driveItem` identifiers and
`webUrl` values, never a temporary upload URL.

## Reconciliation lifecycle

### Recording ingestion

The Zoom command:

1. requests a current access token from the owned OAuth broker;
2. lists and paginates configured users' completed cloud recordings;
3. validates key-value meeting metadata;
4. retains every completed MP4 segment;
5. derives the occurrence ID, expected-file fingerprint, and S3 keys;
6. uses object existence plus `mi.source` to skip a complete upload;
7. streams each recording into S3; and
8. writes `mi.source` only after the upload succeeds.

Every segment repeats the small expected-MP4 ID set. An occurrence is complete
only when all expected IDs exist, all annotations agree on the set
fingerprint, and all segments reach a terminal transcription state.

The occurrence ID is
`{team-or-central}-{meeting-type}-{UTC-start}-{Zoom-UUID-hash}`. The date,
meeting type, scope/team, and raw/normalized routing metadata remain in
`mi.source`; the configured recordings prefix plus occurrence ID is the
canonical grouping boundary.

### Transcription and input publication

For each MP4, `meeting-reconcile` computes:

```text
mi-{sha256(bucket, key, object identity, source fingerprint,
           transcription configuration fingerprint, attempt)}
```

It verifies the sibling VTT before trusting `state=complete`, observes an
existing deterministic Transcribe job before attempting to start one, and
records queued/in-progress work for a later cron run. On completion it writes
and verifies the VTT before marking the MP4 complete. A successful no-speech
job becomes `complete_no_speech` with an empty valid VTT sentinel.

When every expected segment is terminal, the reconciler:

- selects the latest eligible Jira snapshot at or before meeting time;
- resolves the named inbound SharePoint profile and applies its missing/stale
  policy;
- renders transcript text, normalized Jira context, selected SharePoint text,
  meeting metadata, the strict result schema, and instructions into Markdown;
- publishes or repairs the revision-addressed Confluence input page; and
- writes `mi.confluence` to all source segments.

### Optional bounded Daily Brief aggregation

An occurrence whose normalized routing says `pipeline=aggregation` is grouped
by configured aggregation profile and business date. The reconciler chooses
one deterministic winner per team, then submits when every expected team is
present or the profile's local deadline has passed. A conditional create of:

```text
aggregates/{profile-id}/{business-date}.json
```

freezes the exact membership before publication. Later arrivals do not
silently revise the brief. A revision is an explicit operator action; it is
not recovered from the historical DynamoDB aggregation table.

### Rovo and finalization

The Automation rule is a required runtime component, not optional
documentation. Its exact trigger/actions/prompt are in
[automation/README.md](automation/README.md).

For every result lacking a current completion receipt, `analysis-finalize`:

1. loads its associated input-page property and result-page version;
2. parses and validates the JSON response;
3. verifies every requested recording and time interval;
4. downloads the required MP4s to the ephemeral build workspace;
5. creates timestamp-accurate clips with FFmpeg;
6. uploads deterministic clip paths to the configured SharePoint drive;
7. renders and creates or repairs the final Confluence report;
8. confirms the remote page and clip references; and
9. writes the completion property and done label last.

If there are no demos, the same command publishes the report and records the
empty clip list.

## Idempotency and crash recovery

Scheduler invocation is at least once and builds can overlap or be retried.
Correctness does not depend on a schedule firing exactly once.

| Crash window | Next-run behavior |
| --- | --- |
| MP4 uploaded before `mi.source` | `HeadObject` finds it; the downloader verifies it and repairs the annotation. |
| Transcribe started before annotation update | The deterministic job name is rediscovered; no second job is needed. |
| VTT written before MP4 marked complete | The sibling output is verified and `mi.transcription` is completed. |
| Confluence input page created before annotation | Exact revision title, parent, label, and property searches rediscover it; missing state is repaired. Multiple matches are quarantined. |
| One or more clips uploaded before final page | Their deterministic SharePoint paths resolve to the same `driveItem`; finalization continues. |
| Final page created before result marked done | The deterministic page is rediscovered and the result receipt is written. |
| Done label written but receipt missing | The item remains unfinished because the receipt is authoritative; the finalizer repairs both. |

See [docs/runbooks/replay.md](docs/runbooks/replay.md) for operator-directed
recovery and revision handling.

## Infrastructure boundaries

Terraform creates one small platform rather than a stack per operation:

- an encrypted/versioned S3 artifact bucket;
- four CodeBuild projects with stage-specific IAM;
- four EventBridge Scheduler schedules and one invocation DLQ;
- secret shells and one configuration parameter;
- log groups, build-failure alarms, and an SNS alert topic.

`analysis-finalize` remains a separate project because it needs more ephemeral
disk, a longer timeout, FFmpeg, and outbound Microsoft write access. Scheduler
success means only that `StartBuild` was accepted, so CodeBuild build-state
alarms are required in addition to the Scheduler DLQ.

The Zoom project must be serialized for each shared OAuth token set.
Reconciler concurrency should also be one in the proof of concept. The
idempotency contract still applies even with those limits.

## Trust and security boundaries

- The General OAuth component owns authorization, token refresh, rotation,
  validation, and deauthorization. CodeBuild receives a broker configuration,
  not authority to implement another Zoom OAuth flow.
- Secrets are injected from Secrets Manager at build time and must not appear
  in the shared JSON configuration, source archive, logs, Markdown, or
  Confluence properties.
- Each project has only its stage's S3 prefixes and external secret. The
  finalizer alone receives Graph write credentials for the clip destination.
- Confluence parent IDs, space keys, inbound SharePoint prefixes, and outbound
  drive/folder IDs are allowlisted configuration, not values accepted from
  Rovo JSON.
- AI output is untrusted executable input. Clip bounds, source ownership,
  maximum count/duration, paths, and schema version are validated before
  FFmpeg or Graph is called.

## Intentional limits

- Discovery performs paginated, linear scans of configured active prefixes
  because S3 object listings do not include annotations. This is suitable for
  the stated proof-of-concept scale, not an unbounded archive.
- Daily Brief aggregation is optional and configuration-driven. Membership is
  frozen in one conditional S3 receipt; late arrivals do not automatically
  revise a published brief.
- The repository consumes an existing inbound SharePoint-to-S3 feed; it does
  not own that synchronization job.
- Rovo indexing has no deterministic readiness signal. The Automation prompt
  analyzes the triggering page directly; broader tenant search would require
  a separately proven age/retry policy.
- Exact clip boundaries require re-encoding and therefore sufficient
  CodeBuild disk, memory, and timeout for the largest supported recording.
- Live AWS/SaaS deployment and tenant integration have not been validated by
  this implementation. Required proof gates are listed in
  [docs/deployment.md](docs/deployment.md).
