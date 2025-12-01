package main

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

type sequenceTokenSource struct {
	mu     sync.Mutex
	tokens []string
	calls  int
}

func (s *sequenceTokenSource) Token(context.Context) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	index := s.calls
	s.calls++
	if index >= len(s.tokens) {
		index = len(s.tokens) - 1
	}
	return s.tokens[index], nil
}

func TestZoomListRecordingsPaginates(t *testing.T) {
	tokens := &sequenceTokenSource{tokens: []string{"valid"}}
	var requests int
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		requests++
		if got := request.Header.Get("Authorization"); got != "Bearer valid" {
			t.Errorf("Authorization = %q", got)
		}
		if request.URL.Path != "/users/alice%40example.com/recordings" &&
			request.URL.Path != "/users/alice@example.com/recordings" {
			t.Errorf("path = %q", request.URL.Path)
		}
		switch request.URL.Query().Get("next_page_token") {
		case "":
			fmt.Fprint(response, `{"next_page_token":"page-two","meetings":[{"id":123,"uuid":"one"}]}`)
		case "page-two":
			fmt.Fprint(response, `{"next_page_token":"","meetings":[{"id":"456","uuid":"two"}]}`)
		default:
			http.Error(response, "bad token", http.StatusBadRequest)
		}
	}))
	defer server.Close()

	client := &ZoomClient{BaseURL: server.URL, Tokens: tokens, Client: server.Client()}
	meetings, err := client.ListRecordings(
		context.Background(),
		"alice@example.com",
		time.Date(2026, 7, 1, 0, 0, 0, 0, time.UTC),
		time.Date(2026, 7, 23, 0, 0, 0, 0, time.UTC),
	)
	if err != nil {
		t.Fatalf("ListRecordings() error = %v", err)
	}
	if requests != 2 {
		t.Fatalf("requests = %d, want 2", requests)
	}
	if len(meetings) != 2 || meetings[0].ID != "123" || meetings[1].ID != "456" {
		t.Fatalf("meetings = %#v", meetings)
	}
	if tokens.calls != 1 {
		t.Fatalf("broker calls = %d, want 1", tokens.calls)
	}
}

func TestZoomDownloadRetriesAuthorizationOnce(t *testing.T) {
	tokens := &sequenceTokenSource{tokens: []string{"expired", "fresh"}}
	var requests int
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		requests++
		switch request.Header.Get("Authorization") {
		case "Bearer expired":
			http.Error(response, "expired", http.StatusUnauthorized)
		case "Bearer fresh":
			response.Header().Set("Content-Length", "5")
			fmt.Fprint(response, "video")
		default:
			http.Error(response, "unexpected token", http.StatusForbidden)
		}
	}))
	defer server.Close()

	client := &ZoomClient{BaseURL: server.URL, Tokens: tokens, Client: server.Client()}
	body, length, err := client.Download(context.Background(), server.URL+"/recording")
	if err != nil {
		t.Fatalf("Download() error = %v", err)
	}
	defer body.Close()
	if length != 5 {
		t.Fatalf("content length = %d, want 5", length)
	}
	if requests != 2 || tokens.calls != 2 {
		t.Fatalf("requests = %d, token calls = %d; want 2, 2", requests, tokens.calls)
	}
}

func TestZoomOnlyRetriesAuthorizationOnce(t *testing.T) {
	tokens := &sequenceTokenSource{tokens: []string{"bad-one", "bad-two", "unused"}}
	var requests int
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		requests++
		http.Error(response, "unauthorized", http.StatusUnauthorized)
	}))
	defer server.Close()

	client := &ZoomClient{BaseURL: server.URL, Tokens: tokens, Client: server.Client()}
	body, _, err := client.Download(context.Background(), server.URL+"/recording")
	if body != nil {
		body.Close()
	}
	if err == nil {
		t.Fatal("Download() error = nil")
	}
	if requests != 2 || tokens.calls != 2 {
		t.Fatalf("requests = %d, token calls = %d; want exactly 2, 2", requests, tokens.calls)
	}
}

func TestBrokerTokenSourcePOSTContractAndPrecedence(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodPost {
			t.Errorf("method = %q, want POST", request.Method)
		}
		if request.Header.Get("Accept") != "application/json" ||
			request.Header.Get("Content-Type") != "application/custom+json" ||
			request.Header.Get("Authorization") != "Broker credential" {
			t.Errorf("headers = %#v", request.Header)
		}
		body, err := io.ReadAll(request.Body)
		if err != nil {
			t.Fatal(err)
		}
		if string(body) != "{}" {
			t.Errorf("body = %q, want {}", body)
		}
		fmt.Fprint(response, `{"access_token":" preferred ","accessToken":"second","token":"third"}`)
	}))
	defer server.Close()

	source := &BrokerTokenSource{
		URL:    server.URL,
		Method: "POST",
		Headers: map[string]string{
			"Content-Type":  "application/custom+json",
			"Authorization": "Broker credential",
		},
		Client: server.Client(),
	}
	token, err := source.Token(context.Background())
	if err != nil {
		t.Fatalf("Token() error = %v", err)
	}
	if token != "preferred" {
		t.Fatalf("token = %q, want preferred", token)
	}
}

func TestBrokerTokenSourceRejectsTrailingJSON(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		fmt.Fprint(response, `{"access_token":"token"} {}`)
	}))
	defer server.Close()

	source := &BrokerTokenSource{URL: server.URL, Method: "GET", Client: server.Client()}
	token, err := source.Token(context.Background())
	if token != "" || err == nil {
		t.Fatalf("Token() = %q, %v; want an error", token, err)
	}
}

func TestZoomDownloadFollowsRedirect(t *testing.T) {
	tokens := &sequenceTokenSource{tokens: []string{"valid"}}
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		switch request.URL.Path {
		case "/download":
			http.Redirect(response, request, "/storage/object", http.StatusFound)
		case "/storage/object":
			if got := request.Header.Get("Authorization"); got != "Bearer valid" {
				t.Errorf("redirected Authorization = %q", got)
			}
			fmt.Fprint(response, "video")
		default:
			http.NotFound(response, request)
		}
	}))
	defer server.Close()

	client := &ZoomClient{BaseURL: server.URL, Tokens: tokens, Client: server.Client()}
	body, _, err := client.Download(context.Background(), server.URL+"/download")
	if err != nil {
		t.Fatalf("Download() error = %v", err)
	}
	payload, err := io.ReadAll(body)
	body.Close()
	if err != nil || string(payload) != "video" {
		t.Fatalf("payload = %q, error = %v", payload, err)
	}
}
