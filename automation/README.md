# Required Confluence Automation / Rovo Rule

The AWS jobs do not invoke Rovo directly. This Confluence Automation rule is a
required runtime component. It must be created and enabled in the same space
as `confluence.spaceKey`.

Atlassian documents that a Confluence `Page labeled` trigger exposes
`{{page}}`, that the `Use Rovo agent` action returns text through
`{{agentResponse}}`, and that a following `Publish new page` action can use
page smart values. The target tenant still needs a proof run because
Automation entitlements, editor fields, and label/template behavior vary.

## Reserved values

| Purpose | Exact value |
| --- | --- |
| Input trigger label | `status-ready-for-ai` |
| Input type label | `automation-ai-request` |
| Result type label | `automation-ai-result` |
| Completion label | `status-analysis-done` |
| Result title | `MI result — {{page.id}}` |
| Result parent | the page configured as `confluence.resultParentId` |
| Result body | `{{agentResponse.asString}}` |

The title uses an em dash (`—`) surrounded by spaces. Do not substitute a
hyphen, append a timestamp, or allow Automation to add a sequential suffix.
The finalizer parses the input page ID from this exact title and quarantines
duplicates.

Input pages are themselves revision-addressed:
`Meeting — {occurrenceId} — r{requestRevision}` or
`Daily brief — {profileId} — {businessDate} — r{requestRevision}`. An
unchanged reconcile reuses the same page. A material request change creates a
new page ID, and therefore a distinct result title, instead of re-triggering
Automation against stale state.

## Build the flow

1. In Atlassian Studio or Confluence space automation, create a flow from
   scratch and scope it to the configured Confluence space.
2. Set **WHEN: Page labeled** and select only
   `status-ready-for-ai`. This is preferable to a broad page-published trigger
   because `meeting-reconcile` adds the state label only after the input page
   body and association property are confirmed.
3. Add a **smart values condition**:
   `{{page.parent.id}}` equals the configured
   `confluence.inputParentId`.
4. Add a second condition that the triggering page has the
   `automation-ai-request` label. If the editor cannot reliably compare the
   `{{page.labels}}` collection, use a CQL condition scoped to
   `id = {{page.id}} AND label = "automation-ai-request"`.
5. Add **THEN: Use Rovo agent**, select the approved Meeting Intelligence
   agent, and paste [rovo-prompt.txt](rovo-prompt.txt) verbatim into its prompt
   field. Do not give the agent page-creation or SharePoint write
   responsibility; an agent invoked in Automation supplies text to the next
   action.
6. Add **THEN: Publish new page**:
   - space: the configured `confluence.spaceKey`;
   - parent: the configured `confluence.resultParentId`;
   - title: `MI result — {{page.id}}`;
   - page content: `{{agentResponse.asString}}`.
7. Ensure the published result page receives
   `automation-ai-result`. The most reliable tenant-specific choices are:
   - select a dedicated result-page template that carries the label; or
   - add a related-entity branch for the exact result title/parent and apply
     **Add label** to that result page.
   Do not add the result label to the triggering input page.
8. Save and enable the flow. Record its owner, Rovo connection, agent ID,
   space, input parent, result parent, and last proof date in the deployment
   record.

Do not configure the rule to remove/re-add `status-ready-for-ai`; a label
transition on each new revision page is the one-shot trigger. If an Automation
retry creates
`MI result — {id} 1` or any other duplicate, disable the rule, resolve the
duplicate, and repeat the idempotency proof before re-enabling it.

## Prompt behavior

The prompt:

- names the triggering page by `{{page.id}}` and tells the agent to analyze
  that page only;
- treats transcript and context text as data, so embedded instructions do not
  override the schema;
- requires raw JSON without Markdown fences or commentary;
- restricts demo source IDs to the exact IDs printed on the input page; and
- requires numeric seconds relative to the beginning of that source segment.

The finalizer still validates the response. A successful Automation execution
does not imply a valid or safe result.

## Proof checklist

Use a disposable input page that has the same parent, labels, association
property, and schema section as a real page.

1. Add `status-ready-for-ai` and confirm exactly one rule execution.
2. Confirm the agent analyzes the triggering page even before broad Confluence
   search has indexed it.
3. Confirm exactly one result page exists at the configured parent.
4. Confirm its title is exactly `MI result — {numeric-input-page-id}`.
5. Confirm its body resolves to one parseable JSON object, with no code fence,
   prose prefix, or HTML text mixed into the extracted representation.
6. Confirm `automation-ai-result` is present and the input page has not
   acquired that label.
7. Retry/replay the rule in the tenant and verify it cannot produce a
   sequentially suffixed duplicate.
8. Run `analysis-finalize` and confirm it can associate the result back to the
   input page and result page version.
9. Rerun reconciliation unchanged and confirm no second analysis occurs; then
   make a controlled instruction/context change and confirm one new
   `… — r{requestRevision}` input page and one independently titled result are
   created.

The repository does not claim that this tenant-level proof has been performed.

## References

- [Atlassian: Automating Rovo agents](https://support.atlassian.com/rovo/docs/agents-in-automations/)
- [Atlassian: Rovo agent tools in Automation](https://support.atlassian.com/rovo/docs/agent-actions/)
- [Atlassian: Confluence Automation triggers](https://support.atlassian.com/cloud-automation/docs/triggers-in-confluence-automation/)
- [Atlassian: Confluence Automation actions](https://support.atlassian.com/cloud-automation/docs/actions-in-confluence-automation/)
- [Atlassian: Confluence smart values](https://support.atlassian.com/cloud-automation/docs/smart-values-in-confluence-automation/)
