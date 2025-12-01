package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"
)

const sourceAnnotationName = "mi.source"

var ErrAnnotationNotFound = errors.New("S3 object annotation not found")

type AnnotationStore interface {
	Get(context.Context, string, string, string) ([]byte, error)
	Put(context.Context, string, string, string, string, []byte) error
}

type CommandRunner interface {
	Run(context.Context, string, ...string) (stdout []byte, stderr []byte, err error)
}

type ExecCommandRunner struct{}

func (ExecCommandRunner) Run(ctx context.Context, name string, args ...string) ([]byte, []byte, error) {
	command := exec.CommandContext(ctx, name, args...)
	var stdout, stderr strings.Builder
	command.Stdout = &stdout
	command.Stderr = &stderr
	err := command.Run()
	return []byte(stdout.String()), []byte(stderr.String()), err
}

// CLIAnnotationStore isolates the new S3 Object Annotations operations behind
// a small interface. It intentionally uses a current AWS CLI until the
// CodeBuild-pinned Go SDK exposes the operations.
type CLIAnnotationStore struct {
	AWSCLI              string
	Region              string
	ExpectedBucketOwner string
	Runner              CommandRunner
}

func (s *CLIAnnotationStore) Get(
	ctx context.Context,
	bucket string,
	key string,
	name string,
) ([]byte, error) {
	output, err := os.CreateTemp("", "mi-s3-annotation-get-*")
	if err != nil {
		return nil, fmt.Errorf("create annotation output file: %w", err)
	}
	path := output.Name()
	if err := output.Close(); err != nil {
		os.Remove(path)
		return nil, fmt.Errorf("close annotation output file: %w", err)
	}
	defer os.Remove(path)

	args := []string{
		"s3api", "get-object-annotation",
		"--bucket", bucket,
		"--key", key,
		"--annotation-name", name,
	}
	args = append(args, s.globalArgs()...)
	args = append(args, path)
	_, stderr, runErr := s.runner().Run(ctx, s.command(), args...)
	if runErr != nil {
		if strings.Contains(string(stderr), "(NoSuchAnnotation)") {
			return nil, ErrAnnotationNotFound
		}
		return nil, fmt.Errorf("get S3 annotation %s on s3://%s/%s: %w: %s",
			name, bucket, key, runErr, strings.TrimSpace(string(stderr)))
	}
	payload, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read annotation payload: %w", err)
	}
	return payload, nil
}

func (s *CLIAnnotationStore) Put(
	ctx context.Context,
	bucket string,
	key string,
	name string,
	objectETag string,
	payload []byte,
) error {
	if strings.TrimSpace(objectETag) == "" {
		return errors.New("object ETag is required for conditional annotation write")
	}
	if len(payload) == 0 || len(payload) > 1<<20 {
		return fmt.Errorf("annotation payload must be between 1 byte and 1 MiB; got %d bytes", len(payload))
	}
	input, err := os.CreateTemp("", "mi-s3-annotation-put-*")
	if err != nil {
		return fmt.Errorf("create annotation input file: %w", err)
	}
	path := input.Name()
	defer os.Remove(path)
	if _, err := input.Write(payload); err != nil {
		input.Close()
		return fmt.Errorf("write annotation input file: %w", err)
	}
	if err := input.Close(); err != nil {
		return fmt.Errorf("close annotation input file: %w", err)
	}

	args := []string{
		"s3api", "put-object-annotation",
		"--bucket", bucket,
		"--key", key,
		"--annotation-name", name,
		"--annotation-payload", path,
		"--object-if-match", quoteETag(objectETag),
	}
	args = append(args, s.globalArgs()...)
	_, stderr, runErr := s.runner().Run(ctx, s.command(), args...)
	if runErr != nil {
		return fmt.Errorf("put S3 annotation %s on s3://%s/%s: %w: %s",
			name, bucket, key, runErr, strings.TrimSpace(string(stderr)))
	}
	return nil
}

func (s *CLIAnnotationStore) command() string {
	if s.AWSCLI != "" {
		return s.AWSCLI
	}
	return "aws"
}

func (s *CLIAnnotationStore) runner() CommandRunner {
	if s.Runner != nil {
		return s.Runner
	}
	return ExecCommandRunner{}
}

func (s *CLIAnnotationStore) globalArgs() []string {
	args := []string{"--no-cli-pager", "--output", "json"}
	if s.Region != "" {
		args = append(args, "--region", s.Region)
	}
	if s.ExpectedBucketOwner != "" {
		args = append(args, "--expected-bucket-owner", s.ExpectedBucketOwner)
	}
	return args
}

func quoteETag(etag string) string {
	etag = strings.TrimSpace(etag)
	if strings.HasPrefix(etag, `"`) && strings.HasSuffix(etag, `"`) {
		return etag
	}
	return `"` + strings.Trim(etag, `"`) + `"`
}
