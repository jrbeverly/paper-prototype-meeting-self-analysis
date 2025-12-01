# Architecture Simplification Assessment

Assessment date: 2026-07-23

## Executive conclusion

The current architecture can be simplified substantially.

The event-driven implementation is internally coherent and closely follows
[VISION.md](VISION.md), but the repository does not contain a throughput,
latency, ownership, or availability requirement that makes ten Lambda
functions, a Step Functions workflow, two DynamoDB tables, a custom
EventBridge bus, and a queue at every downstream boundary necessary.

For the documented scale—five teams, source discovery every 30 minutes,
SharePoint synchronization every six hours, and AI-result checks every five
minutes—a scheduled, idempotent reconciliation model is a better default. The
recommended target is:

- one versioned command-line application/container;
- a small number of scheduled CodeBuild projects with separate IAM roles where
  credential isolation matters;
- S3 artifacts, manifests, and completion receipts as the desired-state
  journal;
- direct calls to Amazon Transcribe and the external systems;
- source-controlled Gomplate templates for deterministic report rendering;
- JSON Schema validation at the external and AI boundaries; and
- CloudWatch alarms on failed builds and stale work.

At steady state this means three or four scheduled projects, no Step Functions
workflow, no DynamoDB tables, no custom event bus, no pipeline Lambda
functions, and no stage handoff queues. A minimal Zoom OAuth
callback/deauthorization receiver may remain if the selected General app
configuration requires it; one Scheduler invocation DLQ also remains.

This is not a recommendation to replace all domain logic with shell commands.
The metadata rules, deterministic identities, Jira analytics, AI result
schemas, Confluence labels, safe rendering, provenance, and replay semantics
remain valuable. The simplification comes from removing transport contracts
and distributed coordination that do not add proportional value at the
current scale.

The strongest immediate simplification candidate is to disable SharePoint
integration after confirming the product requirement with its owner. The
current package contains SharePoint references, but the Confluence input-page
builder never reads the referenced artifacts or includes their content in the
AI request. The default Terraform configuration also defines no SharePoint
profiles or items. This would be a deliberate scope change because
[VISION.md](VISION.md) calls for a package-time freshness check; replacing the
Lambda before defining a consumer would optimize an unused path.

## Material reviewed

This assessment reviewed:

- [ARCHITECTURE.md](ARCHITECTURE.md), [VISION.md](VISION.md), and the equivalent
  26-page `Meeting Intelligence and Automated Reporting Platform.pdf`;
- [README.md](README.md), [docs/contracts.md](docs/contracts.md),
  [terraform/README.md](terraform/README.md), and the
  [replay runbook](docs/runbooks/replay.md);
- all Lambda handlers and shared TypeScript modules;
- the package-construction Amazon States Language definition; and
- the Terraform stacks and shared modules.

External tool and service capabilities were checked against current project
and vendor documentation. Tool maturity and versions should be reassessed
before implementation.

## What is complex today

The current repository contains the following coordination surface:

| Area | Current implementation |
| --- | --- |
| Deployable compute | 10 Lambda functions |
| Terraform | 12 independently applied stacks, about 4,050 lines of Terraform |
| Package orchestration | One 504-line, 20-state Standard Step Functions definition invoking a single dispatcher Lambda |
| Queues | Seven queue/DLQ pairs, four scheduled-Lambda async-failure queues, and one package-trigger DLQ: 19 SQS queues in total |
| State | `analysis-requests` and `aggregation` DynamoDB tables |
| Events | 16 declared domain event types plus an S3 event on the default bus |
| Application code | About 2,604 lines in Lambda handlers and 2,687 lines of non-test shared TypeScript |
| Recovery | Event reconstruction, queue redrive, forced DynamoDB status changes, and stage-specific replay commands |

Only five of the 16 declared domain event types are consumed by Terraform
rules. The others are effectively telemetry because there is no subscriber or
event archive. The event envelope, queue parsing, partial-batch response,
DynamoDB transition, EventBridge publication, IAM, DLQ, and alarm pattern is
therefore repeated for stages whose business operation is small.

For example, the report renderer's actual work is to load two JSON documents,
validate one, apply a template, and write an artifact. Its Lambda also has to
parse an EventBridge envelope from SQS, load and advance DynamoDB state,
publish another event, and implement partial-batch failure behavior; its
dedicated Terraform stack adds the queue, DLQ, rule, event-source mapping,
function, IAM, logging, and alarms.

### Complexity that does not currently buy full reliability

The current design assumes at-least-once delivery, but several state-plus-event
writes are not transactional:

- Direct analysis writes the request to S3, creates its DynamoDB row, and then
  emits `analysis.request.created`. If publication fails, SQS redelivery sees
  the existing row and returns without republishing. The reconciler only logs
  old `created` rows; it does not repair them.
- Result retrieval advances the request to `result-ready` before emitting the
  next event. A publication failure makes later result reconciliation skip the
  request because it is no longer `page-created`.
- Rendering similarly advances to `rendered` before emitting
  `report.rendered`; redelivery then exits early because the artifact already
  exists, leaving publication untriggered.
- Aggregation changes `collecting` to `submitting` before creating and
  publishing the request. A crash after that compare-and-set leaves the group
  in `submitting`, while later deliveries cannot win the compare-and-set.
- `putJsonIfChanged` is a read-compare-write operation, not an atomic
  conditional write, so it does not by itself serialize concurrent workers.

These are standard dual-write/outbox problems. They can be fixed in the
current architecture, but doing so requires still more coordination. A
desired-state reconciler avoids the issue: it repeatedly observes which
deterministic outputs are missing or stale and converges them. There is no
separate event that must agree with a state transition.

### Complexity that represents real domain value

The following behavior should not be removed:

- validate Zoom key-value metadata and quarantine invalid occurrences instead
  of guessing routing;
- preserve every split recording and write the source manifest last;
- use deterministic IDs, S3 keys, Transcribe job names, and Confluence labels;
- retain source-system IDs, versions, timestamps, and provenance;
- select the Jira snapshot at or before the meeting time and preserve its
  derived analytics;
- complete the Daily Brief when all configured teams arrive or when the
  named-time-zone deadline passes;
- validate AI output against a versioned JSON Schema before rendering;
- escape untrusted text before placing it in Confluence storage markup;
- deduplicate Confluence creation after a crash;
- distinguish retryable failures from quarantined input; and
- support targeted replay.

Those are domain and boundary contracts. Event envelopes, queue payload
schemas, status mirrors, and per-stage deployment units are transport
contracts. The alternative retains the first group and removes most of the
second.

## Recommended target architecture

```text
                         EventBridge Scheduler
                           │       │       │
                    Zoom/Jira   SharePoint  Reconcile/deadline
                           └───────┬───────┘
                                   ▼
                    Scheduled CodeBuild projects
                    ┌────────────────────────────┐
                    │ pinned CLI/container image │
                    │                            │
                    │ mi zoom sync               │
                    │ mi jira snapshot           │
                    │ mi sharepoint sync         │
                    │ mi reconcile               │
                    │ mi replay                   │
                    └──────────────┬─────────────┘
                                   │
             ┌─────────────────────┼─────────────────────┐
             ▼                     ▼                     ▼
       External APIs       Amazon Transcribe       Gomplate + validator
  Zoom/Jira/Graph/Confluence                           │
             └─────────────────────┼───────────────────┘
                                   ▼
                   S3 artifacts + JSON state/receipts
                                   │
                                   ▼
                    Confluence input/result/report pages

        CloudWatch build-state alarms + structured logs + SNS alerts
```

Zoom's interactive authorization callback and deauthorization notification are
a control-plane exception to the scheduled data path. If the selected General
app requires either inbound operation, use an existing approved OAuth/webhook
service or one minimal verified endpoint; do not reintroduce per-stage
Lambdas/queues for it.

Use one source repository and one versioned image, but do not require one
all-powerful job. A practical starting point is four CodeBuild projects:

| Project | Schedule | Access |
| --- | --- | --- |
| `zoom-sync` | Every 30 minutes | Zoom General OAuth client secret and rotating token set(s); Zoom source S3 prefix |
| `jira-snapshot` | Every 30 minutes | Jira secret; Jira S3 prefix |
| `sharepoint-sync` | Every six hours or disabled until consumed | Microsoft credential; SharePoint S3 prefix |
| `reconcile` | Every 5–15 minutes plus each configured Daily Brief deadline | Source media/manifests, Jira and optional SharePoint artifacts, transcript/package/analysis prefixes, Transcribe, Confluence credential |

All four run subcommands from the same artifact. They can be collapsed to two
projects if the organization accepts a broader source-sync IAM role. They
should remain in one Terraform root or a small number of roots; separate state
files are not required merely because IAM roles differ.

[EventBridge Scheduler supports CodeBuild `StartBuild` directly](https://docs.aws.amazon.com/scheduler/latest/UserGuide/managing-targets-templated.html)
and [supports cron schedules in a named time zone](https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html).
Scheduler delivery is
[at least once](https://docs.aws.amazon.com/scheduler/latest/UserGuide/what-is-scheduler.html),
so every command must be idempotent. Set the reconciler project's
concurrent-build limit to one. Also serialize `zoom-sync` for each shared
General OAuth token set so two builds cannot rotate the same refresh token
concurrently. Configure the schedule's retry policy, maximum event age, and
[one SQS DLQ](https://docs.aws.amazon.com/scheduler/latest/UserGuide/configuring-schedule-dlq.html)
for rejected or exhausted `StartBuild` invocations. If an overlapping start is
throttled and exhausts retries, the next reconciliation tick must recover the
work. This DLQ covers failure to invoke CodeBuild; Scheduler success means that
`StartBuild` was accepted, not that the build later succeeded. Alert on
[CodeBuild build-state events](https://docs.aws.amazon.com/eventbridge/latest/ref/events-ref-codebuild.html)
as well as Scheduler target failures. CodeBuild supports
[automatic build retries](https://docs.aws.amazon.com/codebuild/latest/userguide/auto-retry-build.html)
and a maximum build timeout of
[36 hours](https://docs.aws.amazon.com/codebuild/latest/userguide/limits.html).

### Reconciliation loop

The reconciler should be a bounded command that advances each item as far as
its current inputs allow. Date partitions bound discovery of new source
manifests, but must not bound recovery: once an item starts, keep a small
deterministic `work/pending/{kind}/{id}.json` root entry across its stage
transitions and descendants until they reach terminal receipts. Every run scans
pending entries regardless of their source date.

Existence alone is not sufficient to decide that a stage is current. Every
derived artifact or receipt should record a dependency fingerprint over the
exact input object versions or content digests and the relevant profile,
schema, template, and CLI-image versions. An absent output or a fingerprint
mismatch is work to do. An upstream change makes its downstream receipts stale;
the reconciler retains prior S3 versions for audit and rollback. Purely local
outputs can update the same logical artifact. For an AI input that was already
published, prefer a new deterministic request revision/page derived from the
new fingerprint; otherwise there must be an explicit update-and-reprocess
protocol. Never accept an AI result whose request revision/fingerprint does not
match the input that produced it.

Invalidation policy is stage-specific. In particular, when the conditional
Daily Brief submission receipt wins, freeze its aggregation-membership
fingerprint. A package arriving after that point is recorded as a late team but
does not automatically invalidate or republish the brief. A revision requires
an explicit `mi replay --revise-aggregation` command, unless the product owner
deliberately changes the current manual-correction policy.

Fingerprint evaluation is still bounded: normal runs cover active and pending
work. A schema, profile, template, or CLI rollout must name the historical
business-date window it intends to regenerate through `mi replay`; it should
not silently republish all history.

The loop is:

1. Find `source-manifest.json` objects in the active lookback window.
2. For each source manifest without a current transcription:
   - reuse existing VTTs;
   - start any missing deterministic Transcribe jobs;
   - inspect jobs already in progress; and
   - write the transcription manifest only when its outputs exist and match the
     source fingerprint.
3. For each fully transcribed occurrence without a current
   `meeting-package.json`:
   - select the appropriate Jira snapshot;
   - if SharePoint is retained, enforce its configured maximum staleness and
     missing/inaccessible policy rather than silently accepting the last copy;
     and
   - validate and write the package.
4. Create or refresh one deterministic direct request per eligible package.
5. For each aggregation profile, resolve expected teams from the registry,
   filter the date's eligible package manifests, and submit when all expected
   teams are present or the local-time deadline has passed. Require at least
   one accepted input unless an empty report is an explicit requirement. If
   multiple packages exist for one team/date, apply a documented deterministic
   winner rule or quarantine the ambiguity; preserve received, missing, failed,
   and late-team details. Freeze membership when the submission receipt is
   claimed and record later arrivals without resubmitting.
6. For a request without a current input-page receipt, create, update, or
   rediscover the Confluence page and record its page ID.
7. For a request awaiting a result, query the known parent page's children,
   validate the result JSON, and copy it to S3. A changed result page version
   invalidates its prior result/render receipts.
8. For a validated result without a current rendered artifact, build a
   composite render context and run Gomplate.
9. For a rendered artifact without a current publication receipt, create,
   update, or rediscover the report page and record its page ID.

Transcribe terminal states need an explicit policy. A `FAILED` job, or a
`COMPLETED` job whose expected output is unavailable, must not be retried
forever under the same occupied job name. Quarantine the attempt and require an
explicit replay, or create an attempt-versioned deterministic name (and grant
`DeleteTranscriptionJob` only if cleanup is part of the policy). Record the
attempt and source fingerprint in state.

Confluence page creation also needs a recovery key that exists in the initial
create request, before the separate label/receipt calls. Include the
deterministic request ID in the title or trusted body marker and, on retry,
search by exact title plus space and parent as well as by label. Repair a
missing label/receipt on the existing page; quarantine multiple matches instead
of creating another page. Test crashes immediately after create, after labeling,
after update, and after receipt write.

Do not hold a CodeBuild worker open merely to poll Transcribe or Confluence.
Start work in one run and inspect it in the next. The resulting 5–15 minute
granularity is consistent with the source and result polling already described
by the repository. If the business requires immediate per-occurrence progress
or a durable visual execution history, retain Step Functions only for
transcription/package construction and still simplify the downstream chain.

### S3 as desired-state journal

The current key structure is already close to what a reconciler needs:

```text
source/.../source-manifest.json
transcripts/.../transcription-manifest.json
packages/.../meeting-package.json
analysis/requests/{request-id}/request.json
analysis/requests/{request-id}/state.json
analysis/results/{request-id}/result.json
analysis/rendered/{request-id}/body.storage.xml
analysis/rendered/{request-id}/metadata.json
analysis/published/{request-id}/receipt.json
work/pending/{kind}/{id}.json
failures/{stage}/{id}.json
```

Use immutable or versioned artifacts and small mutable state documents.
`state.json` needs only external IDs, dependency/output fingerprints, deadline,
attempts, `nextAttemptAt`, last error, and timestamps; it does not need to
duplicate every artifact. Keep body, title, and labels derived only from
versioned inputs; store observation time separately and preserve the first
render timestamp while the fingerprint is unchanged. A single serialized
reconciler is the simplest writer. If concurrent writers are later introduced,
use S3 conditional writes:

- `If-None-Match: *` to claim/create a deterministic receipt once; and
- `If-Match: <etag>` for optimistic updates to mutable state.

The pending prefix is a repairable scan accelerator, not the source of truth.
Keep one root record through stage transitions; do not delete a predecessor and
then create a successor. Write the deterministic output/receipt first and
conditionally advance the root record. A full artifact sweep must be able to
reconstruct any missing pending record after a crash.

[S3 supports both forms of conditional write](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html).
S3 `GET` and `LIST` operations are
[strongly consistent](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html),
so a successful manifest write can be discovered by the next scan without a
secondary-index consistency delay. Scanners must paginate `ListObjectsV2`,
which returns at most
[1,000 keys per response](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectsV2.html).

Transient failure means the next deterministic output remains absent or stale
and the next run tries again after `nextAttemptAt`. Alert when the configured
maximum age is exceeded. A deterministic failure such as invalid Zoom metadata
or invalid AI JSON writes a quarantine record containing the source
fingerprint and emits an alert. The quarantine suppresses only that exact
version. Before a valid source manifest exists, the Zoom quarantine fingerprint
comes from the remote occurrence/agenda plus relevant recording metadata, and
each Zoom sweep re-evaluates it when those values change. Later-stage
quarantines use manifest/page versions as appropriate. A changed Confluence
page is evaluated again, and `mi replay --clear-quarantine` provides the
operator escape hatch. A daily deeper sweep discovers old source partitions
and reconstructs missing pending entries, while pending work remains eligible
on every run.

### Do not use arbitrary S3 object metadata as the work queue

The proposed architecture should use S3, but not by scanning custom
`x-amz-meta-*` headers. [`ListObjectsV2`](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectsV2.html)
returns keys and a limited set of system properties, not arbitrary user
metadata or tags. Reading those values normally requires a `HeadObject` or tag
request for every candidate.
[S3 Metadata tables](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingMetadata.html)
can make metadata queryable, but would add another index and query service to
an architecture intended to be simpler.

Use date-partitioned key prefixes, compact JSON manifests, and completion
receipts written last. Object metadata remains useful for provenance and
content properties, but it should not be the primary work-discovery index.

## Component disposition

| Current component | Proposed disposition | Reason |
| --- | --- | --- |
| Zoom sync Lambda | Convert to `mi zoom sync`; delegate transfer to rclone/AWS CLI if the redirect pilot passes | General OAuth token rotation, discovery, and meeting-specific routing remain custom; scheduled batch is sufficient at the documented cadence |
| Jira snapshot Lambda | Convert existing normalization/analytics to `mi jira snapshot` | The substantive work is domain-specific and worth keeping |
| SharePoint sync Lambda | With product-owner confirmation, disable until consumed; later use rclone or direct Graph behind `mi sharepoint sync` | Current reports do not consume the artifacts, but deferral changes the documented package-time freshness requirement |
| Package Step Functions + dispatcher Lambda | Replace with `mi reconcile`; optionally retain a narrow workflow only if durable immediate transcription history is required | S3 artifacts and deterministic Transcribe jobs already describe progress |
| Direct-analysis Lambda | Fold into reconciliation | A deterministic request file is enough |
| Aggregation Lambda/table/dynamic schedules | Resolve expected teams from each profile, list eligible packages, and use a fixed named-time-zone schedule per configured deadline | The current default is five teams, but the registry is configuration-driven |
| Confluence page-builder Lambda | Fold into reconciliation; use direct REST calls and a create-time recovery marker | Known page ID plus deterministic request marker supports repair across create/label/receipt crashes |
| Result queue + retrieval Lambda/GSI | Poll outstanding S3 request states in reconciliation | The current design already uses a five-minute sweep |
| Renderer Lambda | Validate JSON, then invoke Gomplate | Rendering is a local deterministic transformation |
| Publisher Lambda | Fold into reconciliation; write a publication receipt | Direct API operation with create-time recovery marker and label repair |
| Custom event bus and domain envelopes | Remove from the primary path after confirming there are no consumers outside this repository | The single reconciler deliberately absorbs the six documented internal rules; no external/fan-out consumer or event archive is documented |
| Handoff queues and stage DLQs | Remove; retain one Scheduler invocation DLQ | Missing or stale outputs are retried by reconciliation; quarantine records preserve deterministic failures |
| Analysis-request DynamoDB table | Replace with S3 request state plus a pending-work index | Current volume makes scanning all nonterminal request states inexpensive |
| Aggregation DynamoDB table | Replace with deterministic package listing and a conditional request receipt | The configured team set is small and bounded |
| SSM service-ARN mesh | Remove | Projects can receive direct configuration and secret ARNs from one Terraform root |
| JSON result schemas | Keep | They protect the AI-to-publication boundary |
| Source/package/request manifests | Keep; introduce versioned v2 contracts or adapters before removing duplicated v1 fields | They provide provenance, replay, and deterministic state |

### Shared code and schema reduction

Shared code is useful where it expresses one rule used by several commands. The
target shared library should retain API clients, metadata validation,
deterministic IDs/paths, Jira analytics, analysis profiles, labels, dependency
fingerprinting, JSON Schema validation, and render-context construction.

The durable schema set should be limited to documents that cross a trust,
process, or time boundary: source/transcription manifests, meeting package,
analysis request/state, AI result, render context, output metadata/receipts,
and quarantine records. If the CLI remains TypeScript, generate types from
those schemas or validate once at command ingress rather than maintaining
parallel per-handler interfaces.

After the compatibility window, remove the 16 domain-event payload/envelope
contracts, SQS/EventBridge request parsers, partial-batch response plumbing,
DynamoDB transition/status mirrors, and Step Functions task-state shapes from
the public contract surface. Keep old v1 artifact readers for replay; fewer
schemas must not mean historical artifacts become unreadable.

## Reusable tool assessment

### SharePoint

#### Preferred as the transfer engine: rclone plus Graph metadata

[rclone](https://rclone.org/) has mature
[SharePoint/OneDrive](https://rclone.org/onedrive/) and
[S3](https://rclone.org/s3/) backends. For unattended use, inject the Microsoft
client secret from Secrets Manager, set `client_credentials=true`, configure
the known `drive_id`, obtain admin consent for the Microsoft Graph
[`Sites.Selected` application permission](https://learn.microsoft.com/en-us/graph/permissions-selected-overview),
and separately assign that application `read` access on the exact site. The
fixed drive avoids discovery calls that can require broader permissions; it
does not itself scope authorization.
Configure the S3 backend with `env_auth=true` so it uses the CodeBuild service
role rather than static AWS keys. rclone's documented client-credential mode
uses a secret, not a certificate.

rclone should move bytes, not define the source-version contract. The wrapper
first queries Graph for the stable site, drive, item ID, eTag/cTag or version,
compares that marker with the last receipt, and then invokes a copy to the
immutable versioned destination, for example:

```sh
rclone copyto \
  sharepoint:path/to/file \
  s3:artifact-bucket/sharepoint-staging/run-id/artifact
```

This is not a Microsoft-to-AWS server-side transfer: bytes pass through the
CodeBuild container. Do not use `sync` for the canonical versioned prefix or
allow source deletions to remove prior S3 versions. rclone's ordinary
cross-backend comparison uses the available size, modification time, and hash;
OneDrive and S3 hashes differ, so this is not an exact Graph delta/version
ledger and can require per-object S3 `HEAD` calls. `rclone lsjson` also does not
provide the exact Graph eTag/version needed by this contract. Write a
provenance receipt from the Graph response containing site, drive, item,
source marker, destination key, byte count, content digest, S3 `VersionId`, and
sync time.

The staging step prevents a metadata/download race. After transfer, query the
drive item again by drive and item ID and verify that its content marker still
matches the pre-copy marker and that the staged byte/hash checks pass. If it
changed, ignore the staged object and retry. If stable, promote it with an
owned S3 writer using `If-None-Match: *` to the canonical
`sharepoint/{domain}/{item-id}/{source-version}/artifact` key; rclone alone does
not enforce write-once destinations. Record the resulting S3 version ID and
digest in the receipt, and flag an existing canonical key with a different
digest instead of overwriting it. Prefer a version-specific Graph download
endpoint when exact historical-version retrieval is required, and expire
unreferenced staging objects with a short lifecycle rule.

As of this assessment, use
[rclone v1.74.4 or later](https://rclone.org/changelog/),
pin the binary and image by digest, and monitor the project's
[security policy/advisories](https://github.com/rclone/rclone/security);
security fixes target the latest release. Do not enable the unnecessary
`rcd`, `serve`, or remote-control endpoints. A VPC-attached CodeBuild project
needs NAT/proxy egress to Microsoft, and transfer concurrency must be capped
and tested against Graph/SharePoint throttling.

#### Alternative when exact SharePoint operations/metadata matter

[CLI for Microsoft 365](https://github.com/pnp/cli-microsoft365) supports
[non-interactive secret or certificate login](https://pnp.github.io/cli-microsoft365/cmd/login/)
and can
[download a known SharePoint file](https://pnp.github.io/cli-microsoft365/cmd/spo/file/file-get/)
to a local path, after which `aws s3 cp` uploads it. This route exposes richer
SharePoint properties and is useful when stable file IDs, versions, or
operations beyond bulk copying matter.

It is a PnP community project and is not covered by Microsoft support. Its
application login and
[some file commands](https://pnp.github.io/cli-microsoft365/cmd/file/file-list/)
can require broader
`Sites.Read.All`/`Files.Read.All` discovery permissions. Strict
`Sites.Selected` isolation may therefore rule it out; prove every command with
a pinned version and the actual least-privilege app, or prefer the fixed-drive
rclone/direct-Graph route. The CLI persists connection tokens unencrypted and
persists certificate contents for certificate login. Keep its cache out of
artifacts and use a shell cleanup trap to log out even when the build fails.

A Ministry of Justice
[`aws-sharepoint-connector`](https://github.com/ministryofjustice/aws-sharepoint-connector)
also copies Graph drive items to S3, but as of this assessment it is a Python
library installed from a pinned Git commit, with no published package/release
and little evidence of external adoption; it is not a batch CLI. It is worth
monitoring, not yet a reason to make the platform depend on it.

### Zoom

The authentication constraint for this system is a Zoom **General OAuth app**;
server-to-server OAuth is not available. Under that constraint, no sufficiently
dependable, established Zoom Meetings-to-S3 CLI was found.

[`jobstoit/zoomdl`](https://github.com/jobstoit/zoomdl) is the closest tool with
a direct S3 destination, but it requires server-to-server OAuth. A source review
at the assessment date also found daemon-oriented execution, static S3
credential handling, and pagination/concurrency problems. It is ruled out
without a substantial fork. [`zoom-cli`](https://github.com/rvben/zoom-cli)
also requires server-to-server OAuth and downloads to local disk. Zoom's
official
[`videosdk-s3-cloud-recordings`](https://github.com/zoom/videosdk-s3-cloud-recordings)
sample applies to Video SDK sessions, not normal Zoom Meetings.

[`dlzoom`](https://github.com/yaniv-golan/dlzoom) is the one relevant pilot
candidate because `dlzoom login` supports the General-app authorization-code
flow for a user-managed, per-user authorization. Its account-scoped mode still
requires server-to-server OAuth and is therefore unavailable here. Code
exchange and refresh are delegated to a hosted or self-hosted Cloudflare
Worker, and the hosted deployment uses the app credentials configured by its
operator; do not assume that it can authorize this organization's General app.
It stores the rotating token set in a local mode-0600 JSON file and downloads
artifacts locally. It does not write directly to S3 or implement this
repository's routing, false-start, split-recording, S3-key, or manifest
contract.

Its Marketplace listing was still pending and external adoption was minimal at
the assessment date. Using the hosted broker introduces another party into the
authorization flow; self-hosting it adds Worker/KV infrastructure. A CodeBuild
wrapper would still have to hydrate its token file from Secrets Manager and
persist every rotated token file, with a failure window after Zoom rotates a
refresh token but before the secret write succeeds. Treat a pinned `dlzoom`
version as a pilot or implementation reference, not as the production token
manager.

The recommended composition is:

1. Create a
   [Zoom General app](https://developers.zoom.us/docs/integrations/create/) with
   only the granular recording and meeting-read scopes required by the chosen
   endpoints. Decide deliberately between admin-managed access, which can cover
   account users when the granted scopes permit it, and user-managed access,
   which covers only each authorizing user's data. A user-managed design needs
   a separate token set per recording host. Serialize refreshes within each
   token set; distinct host token sets can run concurrently. For a user-managed
   app, start with `cloud_recording:read:list_user_recordings` and
   `cloud_recording:read:list_recording_files`, adding
   `meeting:read:meeting` only because this repository reads the meeting
   description. For multiple hosts, use an admin-managed General app and the
   corresponding admin scopes, then address explicit authorized user IDs or
   email addresses. Confirm the final granular scopes against the
   [Meetings API](https://developers.zoom.us/docs/api/meetings/) during the
   pilot.
2. Bootstrap each authorization in a manually invoked `mi zoom authorize`
   operation, never in the scheduled build. If the actual private General app
   exposes and is approved for Zoom's **Use App on Device** capability, prefer
   the
   [device-authorization flow](https://developers.zoom.us/docs/integrations/oauth/):
   show the operator the verification URL and user code, poll for completion,
   and write the resulting token set directly to Secrets Manager. This avoids
   running a callback service. Treat availability as a pilot check rather than
   an assumption because Zoom currently limits this capability; see the
   [platform announcement](https://developers.zoom.us/docs/platform/announcements/).
   If it is unavailable, use the General OAuth authorization-code flow through
   a small approved HTTPS callback. A scheduled CodeBuild job cannot serve as
   that interactive callback. Device authorization removes the OAuth redirect
   callback, but it does not necessarily remove every inbound endpoint: prove
   the chosen app's
   [deauthorization-notification requirements](https://developers.zoom.us/docs/integrations/oauth/#deauthorization)
   during the pilot. If Zoom requires the endpoint, retain a tiny verified
   webhook receiver to disable the token set and initiate the required
   user-data-deletion runbook; otherwise deauthorization can only be detected
   later from refresh/API failures.
3. Store the General app's client credentials and exactly one current token
   envelope per authorized identity in Secrets Manager. With General OAuth,
   `me` refers to the user associated with that token; use explicit user IDs
   only when the app's management type and scopes authorize cross-user access.
4. Give exactly one `zoom-sync` worker ownership of refreshes for each token
   envelope. At the start of `mi zoom sync`, load the latest refresh token,
   refresh when needed, and immediately persist Zoom's returned replacement
   token envelope as a new secret version **before** discovery or download.
   Keep the access token in memory. For a build lasting over an hour, repeat
   that rotate-then-persist-before-use sequence. General OAuth access tokens
   last one hour; refresh tokens expire after 90 days, and Zoom says to use the
   latest refresh token returned by each refresh.

   This protocol cannot make Zoom's rotation and the Secrets Manager write
   atomic. A process crash between them can strand the token chain and require
   interactive reauthorization. Alarm on `invalid_grant`, deauthorization,
   changed scopes, refresh failure, and stale authorization; pause the schedule
   while `mi zoom authorize` repairs the token set. Do not allow a second
   refresh consumer or a second interactive authorization to race the active
   worker. Never log authorization codes, access/refresh tokens, or
   authenticated download URLs. Zoom's
   [OAuth error guidance](https://developers.zoom.us/docs/integrations/oauth-error-messages/)
   should drive the runbook.
5. Keep a small owned discovery command that paginates the recording endpoints,
   loads the meeting description, validates its metadata, and emits
   download-URL/destination-key pairs. Back off on
   [429 responses](https://developers.zoom.us/docs/api/using-zoom-apis/) and
   preserve the original API parameters while consuming each short-lived
   pagination token.
6. Pilot [`rclone copyurl`](https://rclone.org/commands/rclone_copyurl/) with
   the bearer token supplied using its `--header-download` option and shell
   tracing disabled; do not use rclone's global `--header`. Alternatively pipe
   `curl --fail --location` to `aws s3 cp -` with shell `pipefail`, the
   CodeBuild IAM role, and `--expected-size` when Zoom supplies it. Zoom
   download URLs can redirect; verify that a cross-host redirect is a signed URL
   that no longer needs the bearer. Do not force-forward credentials to
   arbitrary redirect hosts, append tokens to URLs/manifests, enable header
   dumps, or log them. Fall back to the thin client's streaming upload if the
   redirect behavior is incompatible.
7. Verify the byte count where the
   [Zoom artifact response](https://developers.zoom.us/docs/api/meetings/)
   supplies `file_size`; record the recording file ID where present, and use a
   deterministic fallback identity such as meeting UUID plus artifact
   type/start time where it is absent. Preserve split segments and write
   `source-manifest.json` last.

This removes most custom streaming/multipart integration while retaining the
OAuth lifecycle and small amount of Zoom logic that are genuinely specific to
the product. `dlzoom` may save some authorization/download code if its pilot
passes, but it does not remove the token-state, S3, or domain wrapper. The
existing Zoom HTTP client is only about 96 lines, so extending it may be lower
risk than adopting a young dependency. That change must replace the current
query-string download token with an `Authorization` header and the safe
redirect policy above; Zoom no longer accepts access tokens in download query
parameters, as documented in its
[platform announcement](https://developers.zoom.us/docs/platform/announcements/#send-access_token-in-authorization-header-not-as-query-parameter).

### Confluence retrieval and publication

When a page ID is known, Confluence retrieval is one API request:

```http
GET /wiki/api/v2/pages/{page-id}?body-format=storage
```

The official [Confluence v2 page API](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-page/)
also creates and updates pages. A small `mi confluence get/create/update`
wrapper around `curl` and `jq`, or the existing 124-line client reused by the
CLI, is sufficient. Store the known input page ID, result child page ID, and
published page ID in the S3 request state. Use the create-time request marker,
exact title/parent lookup, and label lookup to recover from “page created,
label/receipt not written” failures; keep labels for Rovo routing.

An Atlassian service account with
[OAuth 2.0 client credentials](https://support.atlassian.com/user-management/docs/create-oauth-2-0-credential-for-service-accounts/)
is preferable for unattended builds where centralized user management makes it
available. These access tokens last 60 minutes and the API base is
`https://api.atlassian.com/ex/confluence/{cloudId}`. The current repository's
unscoped user API token uses the site-domain base;
[scoped service-account API tokens](https://support.atlassian.com/user-management/docs/manage-api-tokens-for-service-accounts/)
also require the `api.atlassian.com` gateway. The existing authentication can
be retained during migration.

### Gomplate rendering

[Gomplate](https://github.com/hairyhenderson/gomplate) is a good fit for this
renderer. It can load
[JSON from files, stdin, HTTP, or S3](https://docs.gomplate.ca/datasources/)
and fails on missing map keys by default. A narrow render command can be:

```sh
mi schema validate schemas/daily-brief-v1.json result.json
mi render-context build \
  --request request.json \
  --state state.json \
  --result result.json \
  --out /work/render-context.json
mi schema validate schemas/render-context-v1.json /work/render-context.json
gomplate --missing-key error \
  --context .=file:///work/render-context.json \
  --file templates/daily-brief.storage.tmpl \
  --out /work/body.storage.xml
mi confluence-storage validate --fragment /work/body.storage.xml
```

Keep each result JSON Schema as the canonical input contract. Gomplate does not
replace schema validation. The validated composite render context supplies the
request, result, aggregation completeness, routing, provenance, package/result
URIs, page links, and dependency fingerprint currently used by the TypeScript
renderer. Remove per-template TypeScript interfaces that duplicate these JSON
Schemas.

Confluence storage format is
[XHTML-based proprietary markup](https://confluence.atlassian.com/doc/confluence-storage-format-790796544.html),
not standalone browser HTML. A good workflow is:

1. design a representative page in Confluence;
2. retrieve its storage body once;
3. replace dynamic regions with Gomplate expressions;
4. commit the `.storage.tmpl` file and adjacent rules/templates for dynamic
   title, labels, destination, and result-schema version; and
5. test the rendered body against fixtures and a non-production Confluence
   space.

Do not fetch an editable Confluence template at runtime as the only template
source unless the page version is pinned and archived. That would give up
determinism and reviewability.

Gomplate uses Go `text/template`, not context-aware `html/template`.
[Escaping functions must be applied explicitly](https://docs.gomplate.ca/functions/)
to untrusted text and attributes for their XML context. Escaping does not make
a URL safe: allow-list URL schemes/hosts or construct links from trusted
components as well. Raw storage-format fragments must come only from the
trusted template. This should be an acceptance test: the current TypeScript
Daily Brief template inserts `team.progress` and `team.plans` through `pRaw`,
so the migration is also an opportunity to close an existing escaping gap.

Confluence storage bodies are fragments and may use `ac:` and `ri:` prefixes
without standalone namespace declarations. The validator must wrap the
fragment with the expected namespace bindings, or validate it by posting to a
non-production space; a generic standalone XML parse can reject valid storage
markup.

## Resulting infrastructure

The target can be managed in one Terraform root and consists primarily of:

- the versioned S3 artifacts bucket and lifecycle rules;
- Secrets Manager credentials;
- three or four CodeBuild projects sharing a pinned image;
- EventBridge Scheduler schedules, including one fixed named-time-zone schedule
  per configured Daily Brief deadline and one Scheduler invocation DLQ;
- IAM roles scoped by source system and S3 prefix;
- CloudWatch log groups, build-failure/stale-work metrics, and SNS alerts; and
- optionally ECR for the pinned CLI image and a minimal verified Zoom
  OAuth/deauthorization endpoint when the app configuration requires it.

The custom domain event bus, Step Functions workflow, pipeline Lambda module,
stage queue modules/handoff queues, DynamoDB tables, per-stage SSM service
ARNs, and most of the specialized queue dashboard can be removed after
migration.

This topology is operationally simpler, but it is not guaranteed to have the
lowest AWS bill. CodeBuild is billed by build duration and has startup
overhead; frequent mostly-empty runs can cost more than idle Lambda/event
resources. Measure a representative 5-, 10-, and 15-minute reconciliation
schedule. If CodeBuild cost or startup time is poor, the same CLI/container and
S3 state model can run as a scheduled ECS Fargate task or a single reconciler
Lambda without restoring the distributed event framework.

## What is lost, and when to keep the current model

| Simplified batch model | Current event-driven model |
| --- | --- |
| Fewer deployment units and contracts | Better per-stage isolation |
| One observable reconciliation pass | Detailed per-execution Step Functions history |
| Natural repair of missing or stale deterministic outputs | Lower latency after each individual handoff |
| Simple S3-based replay | Native queue backpressure and per-message redrive |
| Coarser scaling and failure domains | Independent scaling and ownership per service |
| Scheduled latency and CodeBuild startup | More infrastructure and dual-write coordination |

Retain or reintroduce queues/workflows when at least one of these becomes a
measured requirement:

- sub-minute end-to-end latency;
- sustained volume that makes bounded S3 scans or one reconciler too slow;
- independent teams owning stages with separate deployment and availability
  objectives;
- multiple independent consumers of the domain events;
- explicit backpressure/rate isolation between stages;
- regulatory need for durable, per-transition workflow history; or
- Transcribe/package executions that regularly exceed the batch operating
  window.

None of those requirements is documented in the repository today. If they
exist outside the repository, they should be recorded with target numbers
before choosing the more complex topology.

## Migration plan

### 1. Prove the contracts

- Define acceptable source-to-report latency, recovery time, daily volume, and
  maximum artifact size.
- Confirm whether SharePoint content is required by any current AI profile or
  report. Disable it if not.
- Confirm there are no domain-event consumers outside this repository.
- Reduce persisted schemas to canonical source, package, analysis state,
  result, render-context, and receipt contracts. Treat removed or renamed v1
  fields as a versioned v2 migration, keep readers/adapters for historical v1
  artifacts, and do not overwrite old documents in place.

### 2. Extract a reusable CLI without changing behavior

- Move existing pure/domain functions behind subcommands.
- Keep deterministic IDs, paths, metadata normalization, Jira analytics,
  labels, result validation, and Confluence deduplication.
- Make each subcommand runnable locally against fixture directories and a test
  bucket.

### 3. Pilot external tools

- If SharePoint is retained, shadow one profile with pinned rclone plus Graph
  version lookup and compare immutable content, versions, and receipts.
- Shadow one Zoom General OAuth identity with the thin discovery adapter and,
  if acceptable, a separately pinned `dlzoom` pilot. Verify authorization
  bootstrap, serialized refresh-token rotation and secret writeback,
  reauthorization/invalid-grant alarms, deauthorization notification and
  data-deletion obligations, identity coverage, cross-host download redirects,
  pagination, absent file IDs/sizes, split segments, false starts, retries, and
  files larger than normal CodeBuild scratch space.
- Render both current report types with Gomplate and compare the semantic
  Confluence output, including adversarial escaping fixtures.

### 4. Introduce the reconciler in shadow mode

- Read the existing S3 layout and calculate actions without publishing.
- Compare package selection, Daily Brief membership, AI-result validation, and
  rendered output against the existing pipeline.
- Change a source manifest, Jira/SharePoint selection, AI page, schema, profile,
  and template independently; verify that dependency fingerprints rebuild
  exactly the affected downstream artifacts.
- Exercise terminal Transcribe jobs, old pending work, corrected quarantined
  input, duplicate/zero aggregation inputs, and crashes after every external
  side effect. Confirm that the next run repairs state without duplication.

### 5. Cut over by phase

An isolated stage cannot simply stop publishing its legacy event while its
downstream consumer is still active. Use explicit compatibility boundaries:

1. Cut over each scheduled source job independently while preserving the v1
   manifest/path contract used by package construction.
2. Cut over package/transcription reconciliation and temporarily emit the
   existing `meeting.package.ready` event until every analysis profile has
   moved, or cut it together with all of its downstream consumers.
3. For each analysis profile, atomically switch the connected slice from
   direct/aggregation request construction through Confluence exchange,
   retrieval, rendering, and publication. Do not run two publishers for the
   same deterministic request ID.
4. Drain legacy-owned requests or explicitly import their DynamoDB state and
   Confluence page IDs into S3 before changing ownership. Remove the temporary
   package event only after no legacy slice consumes it.

Disable each old schedule/rule before enabling its authoritative replacement.
Record which system owns every request ID. A rollback should route only
unstarted work back to the old path while the current owner completes or is
explicitly migrated; it must not allow both paths to publish. Keep S3
versioning and the old infrastructure deployable during the rollback window.

### 6. Remove obsolete infrastructure

- Remove downstream queues/Lambdas and the analysis-request table after all
  outstanding requests have receipts.
- Remove aggregation state and dynamic schedules after the fixed deadline job
  has run successfully through multiple complete and partial Daily Briefs.
- Remove Step Functions last, after long-running and failed transcription
  cases have passed the reconciler acceptance tests.
- Remove compatibility events, envelope parsing, and the custom bus only after
  every internal slice has migrated and external consumers have been ruled
  out.

## Decision

Proceed with a time-boxed batch/CLI proof of concept.

The proof should target one Zoom series, one direct report, and one Daily Brief
date. Adopt the simplified architecture if it demonstrates:

- no duplicate source, request, or Confluence pages under repeated runs;
- recovery from an interrupted build at every external side-effect boundary;
- safe General OAuth refresh-token rotation, an actionable reauthorization
  path, and proven deauthorization/data-deletion handling;
- correct stage-specific invalidation after changed
  source/config/schema/template inputs and continued recovery of work older
  than the discovery window;
- bounded recovery for terminal Transcribe jobs and automatic re-evaluation of
  corrected quarantined input;
- correct partial aggregation at the named-time-zone deadline, with late inputs
  recorded but not republished without an explicit revision;
- equivalent or intentionally improved report output;
- explicit XML-context escaping and URL allow-listing for untrusted values;
- actionable failure and stale-work alarms;
- acceptable 95th-percentile latency and operating cost; and
- materially fewer deployment units and operational runbook steps.

The likely end state is not “no custom code.” It is a smaller, testable set of
domain commands that delegates file transfer and rendering to established
tools, with S3 expressing durable desired state and scheduled CodeBuild jobs
converging the system toward it.
