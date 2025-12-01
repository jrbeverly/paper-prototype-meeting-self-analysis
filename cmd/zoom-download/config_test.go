package main

import (
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestParseConfigReadsBrokerSecretAndEnvironmentOverrides(t *testing.T) {
	t.Setenv("MI_ZOOM_OAUTH_SECRET_JSON", `{
		"tokenUrl":"https://broker-from-secret.example/token",
		"method":"GET",
		"authorization":"Secret credential",
		"headers":{"X-From-Secret":"yes","X-Override":"secret"}
	}`)
	t.Setenv("GENERAL_OAUTH_TOKEN_METHOD", "POST")
	t.Setenv("GENERAL_OAUTH_AUTHORIZATION", "Env credential")
	t.Setenv("GENERAL_OAUTH_HEADERS_JSON", `{"X-Override":"env","X-From-Env":"yes","authorization":"must-not-win"}`)
	t.Setenv("MI_BUCKET", "recordings")

	config, err := parseConfig(
		[]string{"--users", "me,alice@example.com,me"},
		time.Date(2026, 7, 24, 12, 0, 0, 0, time.UTC),
	)
	if err != nil {
		t.Fatalf("parseConfig() error = %v", err)
	}
	if config.TokenURL != "https://broker-from-secret.example/token" || config.TokenMethod != "POST" {
		t.Fatalf("token broker config = %#v", config)
	}
	if config.TokenHeaders["Authorization"] != "Env credential" ||
		config.TokenHeaders["X-Override"] != "env" ||
		config.TokenHeaders["X-From-Secret"] != "yes" ||
		config.TokenHeaders["X-From-Env"] != "yes" {
		t.Fatalf("token headers = %#v", config.TokenHeaders)
	}
	if !reflect.DeepEqual(config.Download.Users, []string{"me", "alice@example.com"}) {
		t.Fatalf("users = %#v", config.Download.Users)
	}
	for key := range config.TokenHeaders {
		if key != "Authorization" && strings.EqualFold(key, "Authorization") {
			t.Fatalf("duplicate case-insensitive authorization header: %#v", config.TokenHeaders)
		}
	}
}

func TestParseConfigRejectsMalformedNumericEnvironment(t *testing.T) {
	t.Setenv("ZOOM_LOOKBACK_DAYS", "not-a-number")

	_, err := parseConfig(nil, time.Date(2026, 7, 24, 12, 0, 0, 0, time.UTC))
	if err == nil || !strings.Contains(err.Error(), "ZOOM_LOOKBACK_DAYS must be an integer") {
		t.Fatalf("parseConfig() error = %v", err)
	}
}
