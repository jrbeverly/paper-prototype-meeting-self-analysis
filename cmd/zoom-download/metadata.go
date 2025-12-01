package main

import (
	"fmt"
	"regexp"
	"sort"
	"strings"
)

var (
	slugPattern        = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,63}$`)
	jiraProjectPattern = regexp.MustCompile(`^[A-Z][A-Z0-9]{1,9}$`)
)

type MeetingMetadata struct {
	Scope                   string            `json:"scope"`
	Team                    string            `json:"teamId,omitempty"`
	MeetingType             string            `json:"meetingType"`
	ArtifactClass           string            `json:"artifactClass"`
	JiraProject             string            `json:"jiraProject,omitempty"`
	TranscriptionVocabulary string            `json:"transcriptionVocabulary,omitempty"`
	SharePointProfile       string            `json:"sharePointProfile,omitempty"`
	Pipeline                string            `json:"pipeline"`
	AnalysisProfile         string            `json:"analysisProfile"`
	OutputTemplate          string            `json:"outputTemplate"`
	AggregationProfile      string            `json:"aggregationProfile,omitempty"`
	Raw                     map[string]string `json:"raw"`
}

type routingDefaults struct {
	pipeline           string
	analysisProfile    string
	outputTemplate     string
	aggregationProfile string
	scope              string
}

var meetingDefaults = map[string]routingDefaults{
	"daily-standup": {
		pipeline:           "aggregation",
		analysisProfile:    "daily-brief-team-input",
		outputTemplate:     "daily-brief",
		aggregationProfile: "daily-brief",
		scope:              "team",
	},
	"sprint-planning": {
		pipeline:        "direct",
		analysisProfile: "sprint-planning",
		outputTemplate:  "meeting-analysis",
		scope:           "team",
	},
	"sprint-review": {
		pipeline:        "direct",
		analysisProfile: "sprint-review",
		outputTemplate:  "meeting-analysis",
		scope:           "team",
	},
	"backlog-refinement": {
		pipeline:        "direct",
		analysisProfile: "backlog-refinement",
		outputTemplate:  "meeting-analysis",
		scope:           "team",
	},
	"central-backlog-refinement": {
		pipeline:        "direct",
		analysisProfile: "central-backlog-refinement",
		outputTemplate:  "meeting-analysis",
		scope:           "central",
	},
	"scrum-of-scrums": {
		pipeline:        "direct",
		analysisProfile: "scrum-of-scrums",
		outputTemplate:  "meeting-analysis",
		scope:           "central",
	},
	"symposium": {
		pipeline:        "direct",
		analysisProfile: "symposium",
		outputTemplate:  "meeting-analysis",
		scope:           "central",
	},
}

type MetadataValidationError struct {
	Issues []string
}

func (e *MetadataValidationError) Error() string {
	return "invalid meeting metadata: " + strings.Join(e.Issues, "; ")
}

// ParseMeetingMetadata treats a Zoom agenda as configuration: blank lines and
// comments are ignored, keys are case-insensitive, and later duplicate keys
// win. Malformed and incomplete data is returned as an error for quarantine.
func ParseMeetingMetadata(description string) (MeetingMetadata, error) {
	raw := make(map[string]string)
	var issues []string
	for lineNumber, line := range strings.Split(strings.ReplaceAll(description, "\r\n", "\n"), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		key = strings.ToLower(strings.TrimSpace(key))
		value = strings.TrimSpace(value)
		if !ok || key == "" || value == "" {
			issues = append(issues, fmt.Sprintf("line %d is not non-empty key=value data", lineNumber+1))
			continue
		}
		raw[key] = value
	}

	scope := raw["scope"]
	if scope != "team" && scope != "central" {
		issues = append(issues, `scope must be "team" or "central"`)
	}
	team := raw["team"]
	if scope == "team" {
		if !validSlug(team) {
			issues = append(issues, "team is required for team scope and must be a lowercase kebab-case slug")
		}
	} else if scope == "central" && team != "" {
		issues = append(issues, "team must be omitted for central scope")
	}

	meetingType := raw["meeting_type"]
	defaults, knownMeetingType := meetingDefaults[meetingType]
	if !knownMeetingType {
		allowed := make([]string, 0, len(meetingDefaults))
		for value := range meetingDefaults {
			allowed = append(allowed, value)
		}
		sort.Strings(allowed)
		issues = append(issues, "meeting_type must be one of "+strings.Join(allowed, ", "))
	} else if scope != "" && defaults.scope != scope {
		issues = append(issues, fmt.Sprintf("meeting_type %q requires scope=%s", meetingType, defaults.scope))
	}

	artifactClass := raw["artifact_class"]
	if artifactClass == "" {
		artifactClass = "temporary"
	}
	if artifactClass != "temporary" && artifactClass != "permanent" {
		issues = append(issues, `artifact_class must be "temporary" or "permanent"`)
	}
	if jiraProject := raw["jira_project"]; jiraProject != "" && !jiraProjectPattern.MatchString(jiraProject) {
		issues = append(issues, "jira_project must be an uppercase Jira project key")
	}
	for _, key := range []string{
		"transcription_vocabulary",
		"analysis_profile",
		"output_template",
		"aggregation_profile",
		"sharepoint_profile",
	} {
		if value := raw[key]; value != "" && !validSlug(value) {
			issues = append(issues, key+" must be a lowercase kebab-case slug")
		}
	}

	pipeline := raw["pipeline"]
	if pipeline == "" && knownMeetingType {
		pipeline = defaults.pipeline
	}
	if pipeline != "direct" && pipeline != "aggregation" {
		issues = append(issues, `pipeline must be "direct" or "aggregation"`)
	}
	analysisProfile := firstNonEmpty(raw["analysis_profile"], defaults.analysisProfile)
	outputTemplate := firstNonEmpty(raw["output_template"], defaults.outputTemplate)
	aggregationProfile := firstNonEmpty(raw["aggregation_profile"], defaults.aggregationProfile)
	if analysisProfile == "" {
		issues = append(issues, "analysis_profile is required")
	}
	if outputTemplate == "" {
		issues = append(issues, "output_template is required")
	}
	if pipeline == "aggregation" && aggregationProfile == "" {
		issues = append(issues, "aggregation_profile is required when pipeline=aggregation")
	}
	if pipeline == "direct" && raw["aggregation_profile"] != "" {
		issues = append(issues, "aggregation_profile must be omitted when pipeline=direct")
		aggregationProfile = ""
	}

	if len(issues) > 0 {
		return MeetingMetadata{Raw: raw}, &MetadataValidationError{Issues: issues}
	}
	if pipeline != "aggregation" {
		aggregationProfile = ""
	}
	return MeetingMetadata{
		Scope:                   scope,
		Team:                    team,
		MeetingType:             meetingType,
		ArtifactClass:           artifactClass,
		JiraProject:             raw["jira_project"],
		TranscriptionVocabulary: raw["transcription_vocabulary"],
		SharePointProfile:       raw["sharepoint_profile"],
		Pipeline:                pipeline,
		AnalysisProfile:         analysisProfile,
		OutputTemplate:          outputTemplate,
		AggregationProfile:      aggregationProfile,
		Raw:                     raw,
	}, nil
}

func validSlug(value string) bool {
	return slugPattern.MatchString(value)
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return ""
}
