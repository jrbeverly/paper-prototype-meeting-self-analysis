package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"
)

type AppConfig struct {
	Download            DownloadConfig
	ZoomAPIBase         string
	TokenURL            string
	TokenMethod         string
	TokenHeaders        map[string]string
	AWSRegion           string
	AWSCLI              string
	ExpectedBucketOwner string
	UploadPartSizeMiB   int64
	UploadConcurrency   int
}

func parseConfig(args []string, now time.Time) (AppConfig, error) {
	secret, err := parseOAuthBrokerSecret(os.Getenv("MI_ZOOM_OAUTH_SECRET_JSON"))
	if err != nil {
		return AppConfig{}, fmt.Errorf("MI_ZOOM_OAUTH_SECRET_JSON: %w", err)
	}
	lookbackDefault, err := envInt("ZOOM_LOOKBACK_DAYS", 1)
	if err != nil {
		return AppConfig{}, err
	}
	partSizeDefault, err := envInt64("UPLOAD_PART_SIZE_MIB", 16)
	if err != nil {
		return AppConfig{}, err
	}
	concurrencyDefault, err := envInt("UPLOAD_CONCURRENCY", 3)
	if err != nil {
		return AppConfig{}, err
	}
	defaultTokenURL := firstNonEmpty(os.Getenv("GENERAL_OAUTH_TOKEN_URL"), secret.TokenURL)
	defaultTokenMethod := firstNonEmpty(os.Getenv("GENERAL_OAUTH_TOKEN_METHOD"), secret.Method, "POST")

	flags := flag.NewFlagSet("zoom-download", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	users := flags.String("users", envOr("ZOOM_USERS", "me"), "comma-separated Zoom user IDs (admin scopes required for users other than me)")
	fromValue := flags.String("from", os.Getenv("ZOOM_FROM"), "inclusive recording window start, YYYY-MM-DD")
	toValue := flags.String("to", os.Getenv("ZOOM_TO"), "inclusive recording window end, YYYY-MM-DD")
	lookback := flags.Int("lookback-days", lookbackDefault, "default days before today when --from is omitted")
	bucket := flags.String("bucket", os.Getenv("MI_BUCKET"), "destination S3 bucket")
	prefix := flags.String("prefix", envOr("MI_RECORDINGS_PREFIX", "meetings"), "destination S3 prefix")
	quarantinePrefix := flags.String("quarantine-prefix", envOr("MI_QUARANTINE_PREFIX", "quarantine"), "invalid metadata receipt prefix")
	zoomBase := flags.String("zoom-api-base", envOr("ZOOM_API_BASE", "https://api.zoom.us/v2"), "Zoom REST API base URL")
	tokenURL := flags.String("token-url", defaultTokenURL, "owned General OAuth token-broker endpoint")
	tokenMethod := flags.String("token-method", defaultTokenMethod, "token-broker HTTP method")
	region := flags.String("region", os.Getenv("AWS_REGION"), "AWS region")
	awsCLI := flags.String("aws-cli", envOr("AWS_CLI", "aws"), "path to a current AWS CLI v2")
	expectedOwner := flags.String("expected-bucket-owner", os.Getenv("EXPECTED_BUCKET_OWNER"), "optional expected S3 account ID")
	partSize := flags.Int64("upload-part-size-mib", partSizeDefault, "multipart upload part size in MiB")
	concurrency := flags.Int("upload-concurrency", concurrencyDefault, "multipart upload concurrency")
	if err := flags.Parse(args); err != nil {
		return AppConfig{}, err
	}
	if flags.NArg() != 0 {
		return AppConfig{}, fmt.Errorf("unexpected positional arguments: %s", strings.Join(flags.Args(), " "))
	}
	if strings.TrimSpace(*bucket) == "" {
		return AppConfig{}, errors.New("--bucket or MI_BUCKET is required")
	}
	tokenURLValue := strings.TrimSpace(*tokenURL)
	if tokenURLValue == "" {
		return AppConfig{}, errors.New("--token-url or GENERAL_OAUTH_TOKEN_URL is required")
	}
	brokerURL, err := url.Parse(tokenURLValue)
	if err != nil || brokerURL.Scheme != "https" || brokerURL.Host == "" {
		return AppConfig{}, errors.New("--token-url must be an absolute HTTPS URL")
	}
	method := strings.ToUpper(strings.TrimSpace(*tokenMethod))
	if method != "GET" && method != "POST" {
		return AppConfig{}, errors.New("--token-method must be GET or POST")
	}
	if *lookback < 0 {
		return AppConfig{}, errors.New("--lookback-days cannot be negative")
	}
	if *partSize < 5 {
		return AppConfig{}, errors.New("--upload-part-size-mib must be at least 5")
	}
	if *concurrency < 1 {
		return AppConfig{}, errors.New("--upload-concurrency must be at least 1")
	}
	parsedUsers := splitCSV(*users)
	if len(parsedUsers) == 0 {
		return AppConfig{}, errors.New("at least one Zoom user must be configured")
	}

	today := midnightUTC(now)
	to := today
	if *toValue != "" {
		to, err = parseDate(*toValue)
		if err != nil {
			return AppConfig{}, fmt.Errorf("--to: %w", err)
		}
	}
	from := to.AddDate(0, 0, -*lookback)
	if *fromValue != "" {
		from, err = parseDate(*fromValue)
		if err != nil {
			return AppConfig{}, fmt.Errorf("--from: %w", err)
		}
	}
	if from.After(to) {
		return AppConfig{}, errors.New("--from must be on or before --to")
	}

	headers := make(map[string]string, len(secret.Headers)+1)
	mergeHeaders(headers, secret.Headers)
	if value := os.Getenv("GENERAL_OAUTH_HEADERS_JSON"); value != "" {
		var environmentHeaders map[string]string
		if err := json.Unmarshal([]byte(value), &environmentHeaders); err != nil {
			return AppConfig{}, fmt.Errorf("GENERAL_OAUTH_HEADERS_JSON: %w", err)
		}
		mergeHeaders(headers, environmentHeaders)
	}
	if value := os.Getenv("GENERAL_OAUTH_AUTHORIZATION"); value != "" {
		setHeader(headers, "Authorization", value)
	} else if secret.Authorization != "" {
		setHeader(headers, "Authorization", secret.Authorization)
	}

	return AppConfig{
		Download: DownloadConfig{
			Users:            parsedUsers,
			From:             from,
			To:               to,
			Bucket:           strings.TrimSpace(*bucket),
			Prefix:           strings.Trim(*prefix, "/"),
			QuarantinePrefix: strings.Trim(*quarantinePrefix, "/"),
		},
		ZoomAPIBase:         strings.TrimRight(*zoomBase, "/"),
		TokenURL:            tokenURLValue,
		TokenMethod:         method,
		TokenHeaders:        headers,
		AWSRegion:           *region,
		AWSCLI:              *awsCLI,
		ExpectedBucketOwner: *expectedOwner,
		UploadPartSizeMiB:   *partSize,
		UploadConcurrency:   *concurrency,
	}, nil
}

func mergeHeaders(destination, source map[string]string) {
	keys := make([]string, 0, len(source))
	for key := range source {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		setHeader(destination, key, source[key])
	}
}

func setHeader(headers map[string]string, key, value string) {
	for existing := range headers {
		if strings.EqualFold(existing, key) {
			delete(headers, existing)
		}
	}
	headers[http.CanonicalHeaderKey(key)] = value
}

type oauthBrokerSecret struct {
	TokenURL      string            `json:"tokenUrl"`
	Method        string            `json:"method"`
	Authorization string            `json:"authorization"`
	Headers       map[string]string `json:"headers"`
}

func parseOAuthBrokerSecret(value string) (oauthBrokerSecret, error) {
	if strings.TrimSpace(value) == "" {
		return oauthBrokerSecret{}, nil
	}
	var secret oauthBrokerSecret
	if err := json.Unmarshal([]byte(value), &secret); err != nil {
		return oauthBrokerSecret{}, err
	}
	if secret.Headers == nil {
		secret.Headers = make(map[string]string)
	}
	return secret, nil
}

func parseDate(value string) (time.Time, error) {
	parsed, err := time.Parse("2006-01-02", value)
	if err != nil {
		return time.Time{}, fmt.Errorf("%q must be YYYY-MM-DD: %w", value, err)
	}
	return parsed.UTC(), nil
}

func splitCSV(value string) []string {
	seen := make(map[string]struct{})
	var result []string
	for _, item := range strings.Split(value, ",") {
		item = strings.TrimSpace(item)
		if item == "" {
			continue
		}
		if _, duplicate := seen[item]; duplicate {
			continue
		}
		seen[item] = struct{}{}
		result = append(result, item)
	}
	return result
}

func envOr(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func envInt(key string, fallback int) (int, error) {
	value := os.Getenv(key)
	if value == "" {
		return fallback, nil
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return 0, fmt.Errorf("%s must be an integer: %w", key, err)
	}
	return parsed, nil
}

func envInt64(key string, fallback int64) (int64, error) {
	value := os.Getenv(key)
	if value == "" {
		return fallback, nil
	}
	parsed, err := strconv.ParseInt(value, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("%s must be an integer: %w", key, err)
	}
	return parsed, nil
}
