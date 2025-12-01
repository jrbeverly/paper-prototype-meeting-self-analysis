# Platform Contracts

These are the binding external and durable-state interfaces for the
cron-driven implementation. They replace the Lambda environment, event
envelope, queue, Step Functions, SSM fan-out, and DynamoDB contracts from the
previous system.

JSON examples use camelCase exactly as stored. Unknown fields may be retained
for forward compatibility, but writers must not change the meaning of a
versioned field.

## Commands and exit behavior

| Command | Invocation |
| --- | --- |
| Zoom download | `go run ./cmd/zoom-download` or built `zoom-download` |
| Jira snapshot | `python -m meeting_intelligence.cli jira-snapshot --config PATH` |
| Meeting reconcile | `python -m meeting_intelligence.cli meeting-reconcile --config PATH` |
| Analysis finalize | `python -m meeting_intelligence.cli analysis-finalize --config PATH` |

The Python `--config` flag is optional when one of the documented
`MI_CONFIG_*` sources is set.

Each command emits a JSON summary on standard output. Configuration/usage
errors exit non-zero without starting work. The Zoom command specifically uses
exit status 2 for configuration errors and 1 for operational failures after
continuing other independent occurrences. Invalid Zoom routing metadata is a
successful quarantine result, not a guessed route.

## S3 key space

The configured artifact bucket uses:

```text
meetings/{occurrence-id}/{safe-recording-file-id}.mp4
meetings/{occurrence-id}/{safe-recording-file-id}.vtt
quarantine/zoom/{source-hash}/{source-and-description-hash}.json
jira/{team-id}/snapshots/{UTC-interval-slot}.json
jira/{team-id}/sprints/{sprint-id}/baseline.json
aggregates/{aggregation-profile-id}/{business-date}.json
{configured inbound SharePoint prefixes}/...
```

`meetings/` and `quarantine/` are configurable. An unsafe Zoom recording file
ID is path-escaped and suffixed with a hash; consumers use the original ID in
`mi.source`, never reverse-engineer identity from the safe filename.

There is intentionally no occurrence manifest and no S3 analysis-result JSON.

## Zoom General OAuth broker

The downloader does not refresh Zoom tokens. It calls the organization's
existing General OAuth broker.

### Configuration

| Variable/flag | Contract |
| --- | --- |
| `GENERAL_OAUTH_TOKEN_URL` / `--token-url` | Required HTTPS broker endpoint |
| `GENERAL_OAUTH_TOKEN_METHOD` / `--token-method` | `POST` by default; `GET` is also accepted |
| `GENERAL_OAUTH_AUTHORIZATION` | Optional complete `Authorization` header value |
| `GENERAL_OAUTH_HEADERS_JSON` | Optional JSON object of additional string headers |
| `MI_ZOOM_OAUTH_SECRET_JSON` | Optional broker JSON from Secrets Manager; shape below |

The secret JSON shape is:

```json
{
  "tokenUrl": "https://oauth-broker.example.internal/v1/token/zoom",
  "method": "POST",
  "authorization": "Bearer secret",
  "headers": {"X-Broker-Tenant": "meeting-intelligence"}
}
```

`--token-url`/`--token-method` override their environment-backed defaults.
For those defaults, `GENERAL_OAUTH_TOKEN_URL` and
`GENERAL_OAUTH_TOKEN_METHOD` take precedence over the corresponding secret
fields. Secret `headers` form the base header map;
`GENERAL_OAUTH_HEADERS_JSON` is merged over matching names. Finally,
`GENERAL_OAUTH_AUTHORIZATION`, or secret `authorization` when the environment
value is absent, wins over any case-insensitive `Authorization` entry in that
map. Header JSON values must all be strings.

For a `POST`, the exact request body is `{}` and the default headers are:

```http
Accept: application/json
Content-Type: application/json
```

Configured headers are applied after the defaults and may override them.
`GENERAL_OAUTH_AUTHORIZATION`, when set, supplies the authorization header.
For a `GET`, there is no request body and no default `Content-Type`.

The broker must respond with HTTP success and one JSON object containing a
non-blank token string. Accepted keys, in precedence order, are:

```json
{"access_token": "..."}
{"accessToken": "..."}
{"token": "..."}
```

Expiry fields are intentionally ignored. The broker owns token validity,
refresh-token rotation, validation, and deauthorization. The downloader
caches the returned token only for its one-shot process. After a Zoom `401` or
`403`, it requests one fresh token and retries the failed Zoom operation once.
No other stage calls the broker.

## Zoom metadata and occurrence identity

The recording-list occurrence's `agenda` is parsed as key-value
configuration. When it is absent, the downloader retrieves the meeting by ID
and uses that agenda. Required:

- `scope=team|central`;
- `meeting_type=<configured type>`; and
- `team=<team id>` exactly when `scope=team` (required for team, forbidden for
  central).

`artifact_class` defaults to `temporary`. Routing defaults are selected from
the meeting type. Optional `pipeline`, `analysis_profile`,
`output_template`, and `aggregation_profile` override those defaults, subject
to these invariants:

- an aggregation route requires `aggregation_profile`;
- a direct route forbids an explicitly supplied `aggregation_profile`; and
- team/central meeting types must agree with `scope`.

Other retained values include `jira_project`, `sharepoint_profile`, and
`transcription_vocabulary`. The writer places normalized camelCase values and
the raw parsed values in `mi.source.metadata`.

Built-in routing defaults:

| `meeting_type` | Scope | Pipeline | Analysis profile | Output template | Aggregation |
| --- | --- | --- | --- | --- | --- |
| `daily-standup` | team | `aggregation` | `daily-brief-team-input` | `daily-brief` | `daily-brief` |
| `sprint-planning` | team | `direct` | `sprint-planning` | `meeting-analysis` | — |
| `sprint-review` | team | `direct` | `sprint-review` | `meeting-analysis` | — |
| `backlog-refinement` | team | `direct` | `backlog-refinement` | `meeting-analysis` | — |
| `central-backlog-refinement` | central | `direct` | `central-backlog-refinement` | `meeting-analysis` | — |
| `scrum-of-scrums` | central | `direct` | `scrum-of-scrums` | `meeting-analysis` | — |
| `symposium` | central | `direct` | `symposium` | `meeting-analysis` | — |

Occurrence ID:

```text
{team-or-central}-{meeting-type}-{UTC-start:YYYY-MM-DDThhmmssZ}-{sha256(Zoom-UUID)[0:10]}
```

The Zoom UUID, meeting ID, and recording file ID remain authoritative external
identifiers.

## `mi.source` annotation

Written on each MP4 after its S3 upload succeeds:

```json
{
  "v": 1,
  "kind": "zoom-recording",
  "occurrenceId": "payments-daily-standup-2026-07-23T130000Z-4c7f41a232",
  "meetingUuid": "Zoom occurrence UUID",
  "meetingId": "123456789",
  "recordingFileId": "recording-file-id",
  "expectedMp4FileIds": ["recording-file-id", "second-segment-id"],
  "recordingSetFingerprint": "sha256:...",
  "recordedAt": "2026-07-23T13:00:00Z",
  "durationSeconds": 1827,
  "topic": "Payments daily",
  "metadata": {
    "scope": "team",
    "teamId": "payments",
    "meetingType": "daily-standup",
    "pipeline": "aggregation",
    "analysisProfile": "daily-brief-team-input",
    "outputTemplate": "daily-brief",
    "aggregationProfile": "daily-brief",
    "raw": {}
  },
  "sourcePrefix": "meetings/payments-daily-standup-2026-07-23T130000Z-4c7f41a232/"
}
```

Invariants:

- `v` is integer `1`; `kind` is exactly `zoom-recording`;
- expected IDs are non-empty, unique, sorted, completed MP4 file IDs;
- `recordingSetFingerprint` is `sha256:` plus the SHA-256 of the
  newline-joined sorted IDs;
- every segment in an occurrence repeats the same expected set/fingerprint;
- duration is a non-negative number in seconds; and
- the annotation is written conditionally against the MP4 ETag observed by
  the writer.

## Zoom quarantine record

Invalid routing metadata is written to the configured quarantine prefix:

```json
{
  "v": 1,
  "state": "failed",
  "reason": "invalid-routing-metadata",
  "issues": ["human-readable validation issue"],
  "meetingId": "123456789",
  "meetingUuid": "...",
  "topic": "...",
  "startTime": "2026-07-23T13:00:00Z",
  "description": "raw Zoom description",
  "parsedMetadata": {},
  "completedMp4FileIds": ["..."],
  "observedAt": "2026-07-23T13:30:00Z"
}
```

The deterministic key includes a hash of the source identity and a hash of the
identity plus description. Corrected metadata therefore creates a distinct
audit record and does not overwrite the prior failure.

## `mi.transcription` annotation

Written on the MP4 by `meeting-reconcile`:

```json
{
  "v": 1,
  "cfg": "sha256:...",
  "sourceFingerprint": "sha256:...",
  "job": "mi-<64 lowercase hex characters>",
  "attempt": 0,
  "state": "submitted",
  "vtt": "meetings/.../recording-file-id.vtt",
  "updated": "2026-07-23T13:35:00Z"
}
```

`state` is one of:

- `submitted` for queued or in-progress work;
- `failed`, with a non-empty `failure` field, for a terminal attempt;
- `complete`; or
- `complete_no_speech`.

The source fingerprint covers the recording-set fingerprint, original
recording ID, and current MP4 object identity. `cfg` covers configuration ID,
language, and selected vocabulary. The deterministic Transcribe job name
covers bucket, key, object identity, those two fingerprints, and attempt.

A terminal annotation is current only when both fingerprints match and the
named sibling VTT exists. The VTT receives `mi.transcription-source`:

```json
{
  "v": 1,
  "sourceBucket": "artifact-bucket",
  "sourceKey": "meetings/.../recording-file-id.mp4",
  "sourceObjectIdentity": "\"etag\"",
  "sourceFingerprint": "sha256:...",
  "configurationFingerprint": "sha256:...",
  "job": "mi-...",
  "updated": "2026-07-23T13:40:00Z"
}
```

## Jira snapshot JSON

`jira-snapshot` writes one interval-slot object per configured team:

```json
{
  "schemaVersion": "1.0",
  "teamId": "payments",
  "projectKey": "PAY",
  "boardId": "42",
  "capturedAt": "2026-07-23T13:30:00Z",
  "sprints": [
    {
      "id": 7,
      "name": "Sprint 7",
      "state": "active",
      "goal": "...",
      "startDate": "...",
      "endDate": "...",
      "issues": [],
      "analytics": {
        "total": 0,
        "estimates": {"total": 0, "unestimated": 0},
        "byStatus": {},
        "byStatusCategory": {},
        "byEpic": {},
        "unassigned": [],
        "blocked": []
      }
    }
  ],
  "perspectives": {},
  "raw": {
    "activeSprints": {},
    "workItemsBySprint": {}
  },
  "contentFingerprint": "sha256:..."
}
```

All active sprints are retained. Normalized issues include `key`, `summary`,
`status`, `statusCategory`, `issueType`, `assignee`, `storyPoints`,
`parentKey`, resolved `epicKey`, `blocked`, `labels`, and `updatedAt`.
Publication selects the latest parseable snapshot whose `capturedAt` is at or
before the meeting instant.

## Inbound SharePoint-to-S3 contract

The inbound synchronizer is external to this repository. A named
`inboundSharePointProfiles` entry allowlists one or more S3 prefixes.

The consumer discovers only keys ending in `/metadata.json` or
`.metadata.json`. Every candidate sidecar must be a JSON object with
`status="synchronized"` and non-empty `siteId`, `driveId`, `itemId`,
`version`, `name`, `webUrl`, `syncedAt`, and `contentKey`.

The object named by `contentKey` must:

- be beneath an allowlisted prefix;
- end in `.txt`, `.md`, `.json`, `.csv`, `.html`, `.htm`, or `.vtt`;
- contain UTF-8 textual content that may be embedded verbatim in the Rovo input
  page (invalid byte sequences are replaced);
- be no larger than the profile's `maxBytesPerDocument`;
- have a stable, version-specific key or S3 version/ETag so
  `objectIdentity` identifies the synchronized revision; and
- encode or accompany sufficient provenance for operators to resolve the
  SharePoint site, drive, item, version, name, and source URL.

The recommended versioned producer layout is:

```text
sharepoint/{profile}/{site-id}/{drive-id}/{item-id}/{version}/
  content.md
  metadata.json
```

with `metadata.json`:

```json
{
  "schemaVersion": "1.0",
  "status": "synchronized",
  "siteId": "...",
  "driveId": "...",
  "itemId": "...",
  "version": "...",
  "name": "source.docx",
  "webUrl": "https://...",
  "contentKey": "sharepoint/.../content.md",
  "contentType": "text/markdown",
  "sourceLastModifiedAt": "2026-07-23T12:00:00Z",
  "syncedAt": "2026-07-23T12:05:00Z"
}
```

`contentType` and `sourceLastModifiedAt` are recommended provenance fields;
the fields enumerated above are the minimum accepted shape. `syncedAt` must be
an ISO 8601 timestamp within the profile's `maxAgeHours`.

The consumer bounds each profile by `maxDocuments`, orders sidecars by S3
last-modified time and key, and publishes content key, metadata key,
object/version identity, site/drive/item/version/name/URL/sync provenance, and
text. If `required=true` and no current valid item remains, input-page
publication is deferred. It does not initiate an inbound sync. Producers must
not place secrets, access tokens, binary source files renamed as text, or
unrelated objects beneath an allowlisted prefix.

## Aggregation receipt

Optional Daily Brief membership is conditionally created once:

```json
{
  "v": 1,
  "profileId": "daily-brief",
  "businessDate": "2026-07-23",
  "expectedTeams": ["payments", "search"],
  "occurrenceIds": ["..."],
  "complete": false,
  "frozenAt": "2026-07-23T14:05:00Z"
}
```

The selected occurrence is the latest `(recordedAt, occurrenceId)` per team.
The receipt is written only when all expected teams are present or the
configured local deadline has passed, and only when at least one team is
present. Existing membership is reused; late arrivals do not mutate it.

## Confluence input page

Revision-addressed titles:

- direct: `Meeting — {occurrenceId} — r{requestRevision}`;
- aggregate:
  `Daily brief — {profileId} — {businessDate} — r{requestRevision}`.

`requestRevision` is the first 12 hexadecimal characters of the SHA-256
fingerprint of the complete rendered Markdown and the exact association below,
before adding `requestRevision` and `contentFingerprint`. Consequently, an
unchanged reconciliation reuses the same title/page, while any changed
transcript, selected Jira or SharePoint context, recording association,
aggregation association, result-title contract, or analysis instruction
creates a distinct page. This new page receives a new Confluence page ID, so
the `Page labeled` Automation trigger runs independently and its result title
cannot collide with an earlier request.

Required labels:

- `automation-ai-request`;
- `status-ready-for-ai`; and
- one normalized `recording-{id}` label per source recording.

The `meeting-intelligence` content property is:

```json
{
  "v": 1,
  "kind": "meeting",
  "bucket": "artifact-bucket",
  "sourcePrefix": "meetings/.../",
  "occurrenceIds": ["..."],
  "recordings": [
    {
      "occurrenceId": "...",
      "recordingId": "...",
      "key": "meetings/.../segment.mp4",
      "objectIdentity": "\"etag\"",
      "durationSeconds": 1827,
      "sourceFingerprint": "sha256:..."
    }
  ],
  "jiraSnapshotKeys": ["jira/payments/snapshots/...json"],
  "sharePointArtifacts": [
    {
      "occurrenceId": "...",
      "key": "sharepoint/.../content.md",
      "metadataKey": "sharepoint/.../metadata.json",
      "objectIdentity": "\"etag\"",
      "name": "source.docx",
      "siteId": "...",
      "driveId": "...",
      "itemId": "...",
      "version": "...",
      "webUrl": "https://...",
      "syncedAt": "2026-07-23T12:05:00Z"
    }
  ],
  "resultTitle": "MI result — {{inputPageId}}",
  "requestRevision": "9f4c2a12d80b",
  "contentFingerprint": "sha256:..."
}
```

For an aggregate, `kind=aggregate`, `sourcePrefix=null`, and
`aggregateReceiptKey` plus `aggregationProfile` are present.

`contentFingerprint` covers the rendered Markdown and the association
property including `requestRevision`. The page body must contain transcript
text and selected Jira/SharePoint text; private S3 links alone do not satisfy
the contract.

Each source MP4 then receives `mi.confluence`:

```json
{
  "v": 1,
  "state": "published",
  "pageId": "123456",
  "pageVersion": 4,
  "contentFingerprint": "sha256:...",
  "updated": "2026-07-23T14:10:00Z"
}
```

## Rovo result page

The required Automation contract is in
[`automation/README.md`](../automation/README.md).

- title: exactly `MI result — {inputPageId}`;
- parent: configured `confluence.resultParentId`;
- label: `automation-ai-result`;
- body: one raw JSON object from `{{agentResponse.asString}}`; and
- association: the numeric input page ID in the deterministic title, whose
  `meeting-intelligence` property is authoritative.

### Rovo result JSON

The machine-readable schema is
[`schemas/rovo-result-v1.json`](../schemas/rovo-result-v1.json).

```json
{
  "schemaVersion": "1.0",
  "title": "Optional report title",
  "summary": "Required non-empty summary",
  "decisions": [],
  "actionItems": [],
  "risks": [],
  "demos": [
    {
      "sourceRecordingId": "exact-associated-recording-id",
      "startSeconds": 12.3,
      "endSeconds": 47.8,
      "title": "Required clip title"
    }
  ]
}
```

`schemaVersion`, `summary`, and `demos` are required.
`decisions`, `actionItems`, and `risks` default to empty arrays; their entries
may be strings or JSON objects and are rendered without executing content.
`title` is optional. No more than `finalization.maxDemos` demos are accepted.

Every demo must identify a recording associated with the input page.
`startSeconds` must be a finite number at least zero; `endSeconds` must be
greater and no greater than the observed source duration. Destination paths
come from trusted configuration and deterministic code, never result JSON.
Before downloading, the finalizer also requires the current S3 object identity
(version ID or ETag) to equal the identity frozen on the input page; a
replaced recording invalidates that analysis version.

## SharePoint clip and finalization receipt

Clip path:

```text
{configured-folder}/{occurrence-id}/
  {safe-source-recording-id}-{start-ms}-{end-ms}-r{result-page-version}.mp4
```

Graph must return a `driveItem` with a stable `id`, configured `driveId`, and
browser `webUrl`. A completion receipt is written to the Rovo result page only
after the final Confluence page and every clip are confirmed. Its exact
content-property name is `meeting-intelligence-completion`:

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

The final page title is exactly `Meeting analysis — {inputPageId}` beneath
`confluence.finalParentId`. It has labels `meeting-intelligence-analysis`,
`status-analysis-final`, and the associated recording labels. Its
`meeting-intelligence` property is:

```json
{
  "v": 1,
  "kind": "final-analysis",
  "inputPageId": "123456",
  "resultPageId": "456789",
  "resultVersion": 7,
  "occurrenceIds": ["..."],
  "contentFingerprint": "sha256:..."
}
```

An unsafe result version receives
`meeting-intelligence-finalization={"state":"failed","resultVersion":7,
"reason":"...","updatedAt":"..."}` and is not retried until the result page
gets a new version. `status-analysis-done` is added only after the completion
receipt is written and read back. The receipt, not the label, is exact
completion evidence.
