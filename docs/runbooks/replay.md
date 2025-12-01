# Replay and Recovery Runbook

The platform has no event replay bus and no workflow execution to restart.
Replay means rerunning the bounded owner command after correcting the input or
configuration. Each owner recomputes deterministic identifiers and reconciles
what already exists.

## First response

1. Identify the occurrence ID, recording file ID, input page ID, or result
   page ID from the build's JSON summary.
2. Inspect the latest CodeBuild log for the owning command.
3. Inspect the durable state:
   - MP4 and sibling VTT object existence/ETag;
   - `mi.source`, `mi.transcription`, and `mi.confluence` annotations;
   - Jira snapshot time relative to the meeting;
   - input/result/final page IDs, versions, labels, and content properties;
   - deterministic SharePoint clip paths and returned `driveItem` IDs.
4. Correct the external condition or use a deliberate revision procedure
   below.
5. Run the same command again. Do not delete a remote artifact merely to make
   a stage retry.

Use the same identity, AWS region, configuration, and secrets as the scheduled
project:

```bash
go run ./cmd/zoom-download --from 2026-07-23 --to 2026-07-24
python -m meeting_intelligence.cli jira-snapshot --config config/local.json
python -m meeting_intelligence.cli meeting-reconcile --config config/local.json
python -m meeting_intelligence.cli analysis-finalize --config config/local.json
```

The Zoom date range is inclusive according to Zoom's recording-list API. Keep
manual replay ranges small and within the API's supported window.

## Symptom guide

| Symptom | Owner and recovery |
| --- | --- |
| Completed Zoom occurrence absent from S3 | Run `zoom-download` with an explicit `--from`, `--to`, and, if needed, `--users`. It skips verified segments and repairs an upload that lacks `mi.source`. |
| Zoom meeting was quarantined | Correct its key-value description, then rerun the same date range. Preserve the quarantine record for audit. |
| One split segment is absent | Rerun `zoom-download`. The expected-file set on the present segments keeps the occurrence incomplete until the missing ID arrives. |
| `mi.source` differs among segments | Do not hand-edit one annotation. Confirm the Zoom occurrence/file set, then rerun the downloader so every segment receives the same current fingerprint. |
| Transcription is `submitted` for a long time | Check the deterministic job in Amazon Transcribe, IAM access to the MP4, and service quotas; rerun `meeting-reconcile` after correcting the cause. |
| Transcription is `failed` | Read `failure` from `mi.transcription`. Correct vocabulary/IAM/media/configuration. A new attempt is made only when `transcription.retryFailed` is enabled; use that switch deliberately and turn it off after recovery. |
| Transcribe completed but no VTT is present | Rerun `meeting-reconcile`. It retrieves the existing deterministic job output, writes the sibling VTT, and only then completes the annotation. |
| A no-speech recording never becomes ready | Current code writes a valid empty VTT sentinel and records `complete_no_speech`; rerun the reconciler and check for an older incompatible annotation. |
| No Confluence input page | Verify the occurrence is complete, required Jira/SharePoint inputs satisfy policy, and the parent/space exists. Rerun `meeting-reconcile`. |
| Jira snapshot missing at meeting time | Run `jira-snapshot --team <team>` only if it can produce an eligible historical slot; otherwise restore/import the correct snapshot. With `jira.required=true`, reconciliation deliberately defers rather than using a later snapshot. |
| Multiple input pages for one occurrence | First compare `requestRevision`. Different revision suffixes are intentional immutable requests; the same suffix under the same parent is an ambiguity. For an actual duplicate, stop automated publication, compare association properties, merge/archive the wrong page, and rerun. |
| Input page exists but no Rovo result | Inspect the Confluence Automation audit log. Verify the labels, rule scope, actor permissions, agent availability, and exact deterministic title action in [the Automation guide](../../automation/README.md). |
| Rovo result JSON is invalid | Correct or replace the result page body with JSON that satisfies [the result contract](../contracts.md#rovo-result-json). Do not weaken clip validation to accept unsafe output. |
| Clip upload partially completed | Rerun `analysis-finalize`; it resolves deterministic paths and continues. Do not accept Graph's collision-renamed filename as the desired object. |
| Final page exists but result is not done | Rerun `analysis-finalize`; it rediscovers the page and writes the receipt and `status-analysis-done` label last. |
| Result is labeled done but receipt is absent/stale | The receipt is authoritative. Rerun finalization to verify outputs and repair the property; do not trust the label alone. |
| Scheduler DLQ has a message | Correct the `StartBuild` target/IAM/quota problem, start the affected CodeBuild project once manually, then redrive or delete only the confirmed invocation message. A Scheduler success did not prove the build succeeded. |

## Safe replay rules

- **Do not replace an MP4 to change state.** Annotations are mutable; replacing
  the object changes its identity and intentionally invalidates downstream
  work.
- **Do not invent an annotation by hand.** The payload includes dependency
  fingerprints. Let the owning command write it from observed state.
- **Do not remove a result-page version from a clip filename.** It is the
  boundary between two AI revisions.
- **Do not mark a result done manually.** The completion property certifies a
  final Confluence page and exact SharePoint references.
- **Do not paste credentials into a local config file or replay command.**
  Supply the same secret JSON environment variables used by CodeBuild.

## Deliberate revisions

### Changed meeting or transcription configuration

Changing the source MP4, its ETag/version, recording-set fingerprint,
vocabulary, language, or `transcription.configurationId` produces a different
dependency fingerprint and deterministic Transcribe name. Restrict a rollout
to the intended active prefix/date window; do not silently reprocess the full
archive.

If a failed job should be retried without changing its configuration, set
`transcription.retryFailed` to `true` for the replay. The reconciler increments
`attempt`, so it does not collide with the terminal job name.

### Changed Rovo analysis

Edit or regenerate the deterministic result page through the Automation rule.
A new Confluence result-page version produces versioned clip names. Rerun
`analysis-finalize`; it must not silently overwrite the prior version's
clips. A new version also supersedes a
`meeting-intelligence-finalization` failed receipt for the prior version.
Retain both result-page versions for audit.

### Changed reconciler input or analysis instructions

Rerun `meeting-reconcile`. It fingerprints the complete rendered Markdown and
exact association into `requestRevision`. Unchanged input rediscovers
`… — r{requestRevision}` and does not request another analysis. A changed
transcript, selected Jira/SharePoint context, source association, or analysis
instruction creates a new revision title and Confluence page ID. Adding
`status-ready-for-ai` to that new page triggers an independent Automation run,
whose result remains isolated as `MI result — {newInputPageId}`.

Do not remove and re-add the trigger label on an old input page to request a
new analysis. Retain old input/result pages and their properties for audit.

### Changed final report template

Rerun `analysis-finalize`. The final page is found by its trusted deterministic
identity and updated rather than duplicated. If the completion receipt refers
to the same result-page version, operators must deliberately clear or revise
that receipt through an approved maintenance procedure before expecting
republishing; preserve the old receipt in an audit record.

### Historical replay outside the active scan

Create a temporary copy of the non-secret configuration with an `activePrefix`
that contains only the intended occurrence(s), then run the owner command.
This keeps scan scope and side effects explicit. Do not point an ad hoc replay
at the whole bucket root.

## Crash-recovery invariants

| Last confirmed operation before crash | Durable evidence | Resume action |
| --- | --- | --- |
| S3 upload | MP4 object | downloader repairs `mi.source` |
| Transcribe start | deterministic job | reconciler observes job |
| VTT write | sibling VTT + reverse annotation | reconciler verifies and completes MP4 annotation |
| Input-page create | revision title + `requestRevision` association property | reconciler rediscovers/repairs |
| Result-page create | `MI result — {inputPageId}` + association labels/property | finalizer rediscovers |
| Clip upload | deterministic path + `driveItem` | finalizer reuses/replaces |
| Final-page create | deterministic title/property | finalizer rediscovers |
| Completion property | exact result version/page/clip receipt | finalizer treats version as complete |

When evidence is ambiguous—for example, multiple pages match the same
deterministic identity—stop that item and resolve the ambiguity. Recovery must
never choose a source by recency alone.
