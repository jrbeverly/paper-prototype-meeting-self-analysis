# Configuration Reference

Non-secret Python configuration is one JSON object. Credentials are separate
environment values so a config document can be versioned or stored as a
standard (non-SecureString) SSM parameter.

Start from [`config/example.json`](../config/example.json) and
[`.env.example`](../.env.example). Never commit a populated `.env` or a local
config containing secrets.

## Resolution order

The Python commands load the first available source:

1. explicit `--config PATH`;
2. `MI_CONFIG_FILE`;
3. inline `MI_CONFIG_JSON`; or
4. SSM parameter named by `MI_CONFIG_PARAMETER`.

There is no merge across sources. The selected value must be the complete JSON
document. SSM is read with the build's AWS identity in its configured region.

The Go Zoom downloader is intentionally independent of the Python JSON loader.
It uses command flags first and environment variables/defaults second. Its
complete flag/environment reference appears below. `MI_BUCKET` also overrides
`artifactBucket` for Python, keeping all four jobs on one bucket.

## Shared JSON

### Top level

| Key | Required/default | Meaning |
| --- | --- | --- |
| `artifactBucket` | required unless `MI_BUCKET` is set | S3 bucket consumed by Python jobs; `MI_BUCKET` overrides it and also configures Zoom |
| `activePrefix` | `meetings/` | Bounded MP4 discovery prefix; normalized to end in `/` |
| `businessTimeZone` | `America/Toronto` | IANA zone used for aggregation business dates/deadlines |
| `transcription` | required | Amazon Transcribe settings |
| `jira` | defaults to no teams | Atlassian CLI and team registry |
| `inboundSharePointProfiles` | defaults to `{}` | Allowlisted existing SharePoint-to-S3 text feeds |
| `analysisProfiles` | defaults to `{}` | Instructions selected by normalized Zoom routing |
| `aggregationProfiles` | defaults to `[]` | Optional Daily Brief completeness/deadline rules |
| `confluence` | required | Site/space and trusted parent IDs |
| `finalization` | required | Clip safety/tooling and outbound SharePoint destination |

### `transcription`

| Key | Required/default | Meaning |
| --- | --- | --- |
| `configurationId` | required | Operator-controlled revision string; changing it invalidates current transcript state |
| `languageCode` | `en-US` | Amazon Transcribe language code |
| `maxItemsPerRun` | `100`, minimum 1 | Bound on recordings advanced in one reconciliation |
| `retryFailed` | `false` | When true, a current terminal failure advances to a new attempt/name |

The transcription fingerprint also contains the meeting's optional
`transcription_vocabulary` value. Keep `retryFailed=false` normally; enable it
only after correcting the failed attempt's cause.

### `jira`

| Key | Required/default | Meaning |
| --- | --- | --- |
| `acliCommand` | `acli` | Executable path/name used by the CLI wrapper |
| `snapshotIntervalMinutes` | implementation default `30` | UTC slot size for deterministic snapshot keys |
| `required` | `true` | Defer a configured team's input page when no snapshot exists at/before meeting time |
| `teams` | array | Team entries below |

Each team requires:

- `teamId`: normalized ID used in S3 and Zoom metadata;
- `projectKey`: Jira project;
- `boardId`: Scrum board whose active sprints are exported; and
- `storyPointsField`: site-specific custom field ID.

Optional `jqlPerspectives` contains `{name,jql}` entries.
`${project}` in JQL is replaced with that team's `projectKey`. Do not put
untrusted meeting text into a JQL string.

Parallel active sprints are retained. Parent/epic resolution may cause
additional `acli jira workitem view` calls.

### `inboundSharePointProfiles`

Each property name is the exact `sharepoint_profile` value allowed in Zoom
metadata:

```json
{
  "payments-reference": {
    "prefixes": ["sharepoint/payments-reference/"],
    "maxDocuments": 20,
    "maxBytesPerDocument": 200000,
    "maxAgeHours": 168,
    "required": false
  }
}
```

`prefixes` is a required non-empty string array. Prefixes are trust boundaries:
do not use the bucket root. `maxDocuments` (default 20),
`maxBytesPerDocument` (default 200,000), and `maxAgeHours` (default 168)
bound current content embedded in one input page. If `required=true`, an
occurrence that names the profile is deferred until at least one valid,
non-stale synchronized item exists. See the binding producer contract in
[contracts.md](contracts.md#inbound-sharepoint-to-s3-contract).

### `analysisProfiles`

A mapping from normalized `analysis_profile` to:

```json
{"instructions": "Grounded instructions rendered into the input page"}
```

Missing/empty instructions fall back to the renderer's generic analysis
instruction. Instructions are trusted configuration. Transcript and
synchronized document bodies are untrusted data and cannot override them.

### `aggregationProfiles`

Each optional profile contains:

| Key | Meaning |
| --- | --- |
| `id` | Exact normalized `aggregation_profile` route |
| `expectedTeams` | Team IDs required for a complete submission |
| `deadlineLocal` | `HH:MM` in `businessTimeZone` when a partial non-empty group may freeze |

One deterministic S3 receipt freezes membership. The latest
`(recordedAt, occurrenceId)` candidate wins for a team before the receipt is
created. Late arrivals do not mutate the receipt.

### `confluence`

| Key | Meaning |
| --- | --- |
| `baseUrl` | Tenant wiki base, e.g. `https://example.atlassian.net/wiki` |
| `spaceKey` | Space used for all system pages |
| `inputParentId` | Trusted parent for input pages |
| `resultParentId` | Trusted Automation landing parent for Rovo result pages |
| `finalParentId` | Trusted parent for final reports |
| `md2confCommand` | `md2conf` executable name/path; default `md2conf` |

The Automation rule must be configured with the same space and parent IDs.
Page IDs are strings because Confluence identifiers may exceed JavaScript's
safe integer range.

### `finalization`

| Key | Required/default | Meaning |
| --- | --- | --- |
| `sharePoint.siteId` | required | Allowlisted Microsoft Graph site ID |
| `sharePoint.driveId` | required | Destination document-library drive ID |
| `sharePoint.folder` | `Meeting Intelligence` | Root folder; leading/trailing `/` removed |
| `maxDemos` | `20` | Maximum accepted Rovo demo requests |
| `simpleUploadLimitBytes` | `250000000` | Use direct content upload at/below this limit |
| `uploadChunkBytes` | `10485760` | Upload-session chunk size; must be a multiple of 320 KiB |
| `ffmpegCommand` | `ffmpeg` | Executable name/path |
| `ffprobeCommand` | `ffprobe` | Executable name/path |

The finalizer derives all remote paths from trusted values and safe IDs.
Rovo cannot select a site, drive, folder, filename, or executable.

## Secret JSON

### `MI_JIRA_SECRET_JSON`

```json
{
  "site": "https://example.atlassian.net",
  "email": "service-account@example.com",
  "apiToken": "secret"
}
```

These become `ACLI_SITE`, `ACLI_EMAIL`, and `ACLI_TOKEN` only in the child
`acli` environment.

### `MI_CONFLUENCE_SECRET_JSON`

Basic auth:

```json
{"email": "service-account@example.com", "apiToken": "secret"}
```

Aliases `username` and `token` are accepted. If no email/username is supplied,
`token`/`apiToken` is sent as a bearer token. The same values are mapped to
the environment expected by `md2conf`.

### `MI_GRAPH_SECRET_JSON`

```json
{
  "tenantId": "directory tenant ID",
  "clientId": "application ID",
  "clientSecret": "secret"
}
```

The application uses the client-credentials flow for
`https://graph.microsoft.com/.default`. Its application permissions must be
restricted to the configured site/drive where possible.

## Zoom downloader

| Flag | Environment | Default |
| --- | --- | --- |
| `--users` | `ZOOM_USERS` | `me` |
| `--from` | `ZOOM_FROM` | derived from lookback |
| `--to` | `ZOOM_TO` | today |
| `--lookback-days` | `ZOOM_LOOKBACK_DAYS` | `1` |
| `--bucket` | `MI_BUCKET` | required |
| `--prefix` | `MI_RECORDINGS_PREFIX` | `meetings` |
| `--quarantine-prefix` | `MI_QUARANTINE_PREFIX` | `quarantine` |
| `--zoom-api-base` | `ZOOM_API_BASE` | `https://api.zoom.us/v2` |
| `--token-url` | `GENERAL_OAUTH_TOKEN_URL` | required |
| `--token-method` | `GENERAL_OAUTH_TOKEN_METHOD` | `POST` |
| `--region` | `AWS_REGION` | AWS resolution |
| `--aws-cli` | `AWS_CLI` | `aws` |
| `--expected-bucket-owner` | `EXPECTED_BUCKET_OWNER` | unset |
| `--upload-part-size-mib` | `UPLOAD_PART_SIZE_MIB` | `16` (minimum 5) |
| `--upload-concurrency` | `UPLOAD_CONCURRENCY` | `3` (minimum 1) |

`ZOOM_USERS`/`--users` is a comma-separated list. `from`/`to` use
`YYYY-MM-DD`. Multipart part size and concurrency are also configurable; size
must remain compatible with S3 multipart limits for the largest recording.

CodeBuild may inject the complete
`MI_ZOOM_OAUTH_SECRET_JSON={"tokenUrl","method","authorization","headers"}`
value from Secrets Manager; individual `GENERAL_OAUTH_*` variables take
precedence. Broker headers use `GENERAL_OAUTH_AUTHORIZATION` and
`GENERAL_OAUTH_HEADERS_JSON` as defined in
[contracts.md](contracts.md#zoom-general-oauth-broker).

The selected General app authorization must actually cover every listed user.
A user-managed grant does not imply account-wide recording access.
