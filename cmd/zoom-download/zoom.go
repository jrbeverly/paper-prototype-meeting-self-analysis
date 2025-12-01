package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"
)

type TokenSource interface {
	Token(context.Context) (string, error)
}

type BrokerTokenSource struct {
	URL     string
	Method  string
	Headers map[string]string
	Client  *http.Client
}

func (s *BrokerTokenSource) Token(ctx context.Context) (string, error) {
	method := strings.ToUpper(strings.TrimSpace(s.Method))
	if method == "" {
		method = http.MethodPost
	}
	var body io.Reader
	if method != http.MethodGet {
		body = bytes.NewReader([]byte("{}"))
	}
	req, err := http.NewRequestWithContext(ctx, method, s.URL, body)
	if err != nil {
		return "", fmt.Errorf("create OAuth broker request: %w", err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	req.Header.Set("Accept", "application/json")
	for key, value := range s.Headers {
		req.Header.Set(key, value)
	}
	resp, err := s.httpClient().Do(req)
	if err != nil {
		return "", fmt.Errorf("request access token from OAuth broker: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", httpStatusError("OAuth broker", resp)
	}
	var payload struct {
		AccessToken      string `json:"access_token"`
		CamelAccessToken string `json:"accessToken"`
		Token            string `json:"token"`
	}
	decoder := json.NewDecoder(io.LimitReader(resp.Body, 1<<20))
	if err := decoder.Decode(&payload); err != nil {
		return "", fmt.Errorf("decode OAuth broker response: %w", err)
	}
	var trailing json.RawMessage
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		if err == nil {
			return "", errors.New("decode OAuth broker response: multiple JSON values")
		}
		return "", fmt.Errorf("decode OAuth broker response: trailing content: %w", err)
	}
	token := ""
	for _, candidate := range []string{payload.AccessToken, payload.CamelAccessToken, payload.Token} {
		if strings.TrimSpace(candidate) != "" {
			token = strings.TrimSpace(candidate)
			break
		}
	}
	if token == "" {
		return "", errors.New("OAuth broker response did not contain access_token")
	}
	return token, nil
}

func (s *BrokerTokenSource) httpClient() *http.Client {
	if s.Client != nil {
		return s.Client
	}
	return http.DefaultClient
}

type ZoomID string

func (id *ZoomID) UnmarshalJSON(data []byte) error {
	if len(data) == 0 {
		return errors.New("empty Zoom ID")
	}
	if data[0] == '"' {
		var value string
		if err := json.Unmarshal(data, &value); err != nil {
			return err
		}
		*id = ZoomID(value)
		return nil
	}
	var number json.Number
	if err := json.Unmarshal(data, &number); err != nil {
		return err
	}
	*id = ZoomID(number.String())
	return nil
}

type ZoomRecordingFile struct {
	ID             string `json:"id"`
	FileType       string `json:"file_type"`
	FileSize       int64  `json:"file_size"`
	Status         string `json:"status"`
	RecordingType  string `json:"recording_type"`
	RecordingStart string `json:"recording_start"`
	RecordingEnd   string `json:"recording_end"`
	DownloadURL    string `json:"download_url"`
}

type ZoomMeeting struct {
	ID             ZoomID              `json:"id"`
	UUID           string              `json:"uuid"`
	Topic          string              `json:"topic"`
	Agenda         string              `json:"agenda"`
	StartTime      string              `json:"start_time"`
	Duration       int64               `json:"duration"`
	RecordingFiles []ZoomRecordingFile `json:"recording_files"`
}

type ZoomMeetingDetail struct {
	Agenda string `json:"agenda"`
}

type ZoomService interface {
	ListRecordings(context.Context, string, time.Time, time.Time) ([]ZoomMeeting, error)
	GetMeeting(context.Context, string) (ZoomMeetingDetail, error)
	Download(context.Context, string) (io.ReadCloser, int64, error)
}

type ZoomClient struct {
	BaseURL string
	Tokens  TokenSource
	Client  *http.Client

	mu    sync.Mutex
	token string
}

func (c *ZoomClient) ListRecordings(
	ctx context.Context,
	user string,
	from time.Time,
	to time.Time,
) ([]ZoomMeeting, error) {
	var meetings []ZoomMeeting
	nextPage := ""
	seenTokens := make(map[string]struct{})
	for page := 0; ; page++ {
		if page >= 10_000 {
			return nil, errors.New("Zoom pagination exceeded 10000 pages")
		}
		endpoint, err := url.Parse(strings.TrimRight(c.BaseURL, "/") + "/users/" + url.PathEscape(user) + "/recordings")
		if err != nil {
			return nil, fmt.Errorf("build Zoom recordings URL: %w", err)
		}
		query := endpoint.Query()
		query.Set("from", from.Format("2006-01-02"))
		query.Set("to", to.Format("2006-01-02"))
		query.Set("page_size", "300")
		if nextPage != "" {
			query.Set("next_page_token", nextPage)
		}
		endpoint.RawQuery = query.Encode()

		resp, err := c.authorizedGet(ctx, endpoint.String())
		if err != nil {
			return nil, err
		}
		if resp.StatusCode < 200 || resp.StatusCode >= 300 {
			err := httpStatusError("Zoom list recordings", resp)
			resp.Body.Close()
			return nil, err
		}
		var pagePayload struct {
			NextPageToken string        `json:"next_page_token"`
			Meetings      []ZoomMeeting `json:"meetings"`
		}
		decodeErr := json.NewDecoder(io.LimitReader(resp.Body, 16<<20)).Decode(&pagePayload)
		closeErr := resp.Body.Close()
		if decodeErr != nil {
			return nil, fmt.Errorf("decode Zoom recordings page: %w", decodeErr)
		}
		if closeErr != nil {
			return nil, fmt.Errorf("close Zoom recordings response: %w", closeErr)
		}
		meetings = append(meetings, pagePayload.Meetings...)
		nextPage = pagePayload.NextPageToken
		if nextPage == "" {
			return meetings, nil
		}
		if _, duplicate := seenTokens[nextPage]; duplicate {
			return nil, fmt.Errorf("Zoom returned repeated next_page_token %q", nextPage)
		}
		seenTokens[nextPage] = struct{}{}
	}
}

func (c *ZoomClient) GetMeeting(ctx context.Context, meetingID string) (ZoomMeetingDetail, error) {
	endpoint := strings.TrimRight(c.BaseURL, "/") + "/meetings/" + url.PathEscape(meetingID)
	resp, err := c.authorizedGet(ctx, endpoint)
	if err != nil {
		return ZoomMeetingDetail{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return ZoomMeetingDetail{}, httpStatusError("Zoom get meeting", resp)
	}
	var detail ZoomMeetingDetail
	if err := json.NewDecoder(io.LimitReader(resp.Body, 2<<20)).Decode(&detail); err != nil {
		return ZoomMeetingDetail{}, fmt.Errorf("decode Zoom meeting: %w", err)
	}
	return detail, nil
}

func (c *ZoomClient) Download(ctx context.Context, downloadURL string) (io.ReadCloser, int64, error) {
	resp, err := c.authorizedGet(ctx, downloadURL)
	if err != nil {
		return nil, 0, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		err := httpStatusError("Zoom recording download", resp)
		resp.Body.Close()
		return nil, 0, err
	}
	return resp.Body, resp.ContentLength, nil
}

func (c *ZoomClient) authorizedGet(ctx context.Context, endpoint string) (*http.Response, error) {
	token, err := c.currentToken(ctx)
	if err != nil {
		return nil, err
	}
	for attempt := 0; attempt < 2; attempt++ {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
		if err != nil {
			return nil, fmt.Errorf("create Zoom request: %w", err)
		}
		req.Header.Set("Accept", "application/json")
		req.Header.Set("Authorization", "Bearer "+token)
		resp, err := c.httpClient().Do(req)
		if err != nil {
			return nil, fmt.Errorf("send Zoom request: %w", err)
		}
		if (resp.StatusCode != http.StatusUnauthorized && resp.StatusCode != http.StatusForbidden) || attempt == 1 {
			return resp, nil
		}
		io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
		resp.Body.Close()
		token, err = c.refreshToken(ctx)
		if err != nil {
			return nil, fmt.Errorf("reauthorize with OAuth broker: %w", err)
		}
	}
	panic("unreachable")
}

func (c *ZoomClient) currentToken(ctx context.Context) (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.token != "" {
		return c.token, nil
	}
	token, err := c.Tokens.Token(ctx)
	if err != nil {
		return "", fmt.Errorf("get access token: %w", err)
	}
	c.token = token
	return token, nil
}

func (c *ZoomClient) refreshToken(ctx context.Context) (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	token, err := c.Tokens.Token(ctx)
	if err != nil {
		return "", err
	}
	c.token = token
	return token, nil
}

func (c *ZoomClient) httpClient() *http.Client {
	if c.Client != nil {
		return c.Client
	}
	return http.DefaultClient
}

func httpStatusError(operation string, resp *http.Response) error {
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<10))
	message := strings.TrimSpace(string(body))
	if message == "" {
		message = http.StatusText(resp.StatusCode)
	}
	return fmt.Errorf("%s returned HTTP %s: %s", operation, strconv.Itoa(resp.StatusCode), message)
}
