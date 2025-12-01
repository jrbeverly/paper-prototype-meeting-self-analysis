package main

import (
	"errors"
	"testing"
)

func TestParseMeetingMetadataDerivesRouting(t *testing.T) {
	metadata, err := ParseMeetingMetadata(`
# owned configuration
scope=team
team=payments
meeting_type=daily-standup
jira_project=PAY
`)
	if err != nil {
		t.Fatalf("ParseMeetingMetadata() error = %v", err)
	}
	if metadata.Scope != "team" || metadata.Team != "payments" {
		t.Fatalf("unexpected ownership: %#v", metadata)
	}
	if metadata.Pipeline != "aggregation" ||
		metadata.AggregationProfile != "daily-brief" ||
		metadata.AnalysisProfile != "daily-brief-team-input" {
		t.Fatalf("unexpected routing: %#v", metadata)
	}
	if metadata.ArtifactClass != "temporary" {
		t.Fatalf("artifact class = %q, want temporary", metadata.ArtifactClass)
	}
}

func TestParseMeetingMetadataRejectsInvalidOwnershipAndRouting(t *testing.T) {
	tests := []struct {
		name        string
		description string
	}{
		{
			name: "team missing",
			description: `
scope=team
meeting_type=daily-standup
`,
		},
		{
			name: "central has team",
			description: `
scope=central
team=payments
meeting_type=symposium
`,
		},
		{
			name: "wrong scope for meeting type",
			description: `
scope=central
meeting_type=daily-standup
`,
		},
		{
			name: "aggregation missing profile",
			description: `
scope=team
team=payments
meeting_type=sprint-review
pipeline=aggregation
`,
		},
		{
			name: "direct carries aggregation routing",
			description: `
scope=team
team=payments
meeting_type=sprint-review
aggregation_profile=daily-brief
`,
		},
		{
			name: "malformed",
			description: `
scope=team
team=payments
meeting_type=daily-standup
this is prose
`,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, err := ParseMeetingMetadata(test.description)
			var validationErr *MetadataValidationError
			if !errors.As(err, &validationErr) {
				t.Fatalf("error = %v, want MetadataValidationError", err)
			}
			if len(validationErr.Issues) == 0 {
				t.Fatal("validation error has no issues")
			}
		})
	}
}
