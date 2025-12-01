package main

import (
	"context"
	"errors"
	"os"
	"slices"
	"testing"
)

type fakeCommandRunner struct {
	name   string
	args   []string
	stdout []byte
	stderr []byte
	err    error
	write  []byte
}

func (f *fakeCommandRunner) Run(_ context.Context, name string, args ...string) ([]byte, []byte, error) {
	f.name = name
	f.args = append([]string(nil), args...)
	if len(f.write) > 0 {
		if err := os.WriteFile(args[len(args)-1], f.write, 0o600); err != nil {
			return nil, nil, err
		}
	}
	return f.stdout, f.stderr, f.err
}

func TestCLIAnnotationPutUsesObjectETagPrecondition(t *testing.T) {
	runner := &fakeCommandRunner{}
	store := &CLIAnnotationStore{
		AWSCLI:              "/opt/aws",
		Region:              "ca-central-1",
		ExpectedBucketOwner: "123456789012",
		Runner:              runner,
	}
	if err := store.Put(
		context.Background(), "bucket", "meetings/a.mp4", sourceAnnotationName, "abc123", []byte(`{"v":1}`),
	); err != nil {
		t.Fatalf("Put() error = %v", err)
	}
	if runner.name != "/opt/aws" {
		t.Fatalf("command = %q", runner.name)
	}
	index := slices.Index(runner.args, "--object-if-match")
	if index < 0 || index+1 >= len(runner.args) || runner.args[index+1] != `"abc123"` {
		t.Fatalf("args do not contain quoted ETag precondition: %#v", runner.args)
	}
	payloadIndex := slices.Index(runner.args, "--annotation-payload")
	if payloadIndex < 0 || payloadIndex+1 >= len(runner.args) {
		t.Fatalf("args do not contain annotation payload: %#v", runner.args)
	}
	if _, err := os.Stat(runner.args[payloadIndex+1]); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("temporary payload was not removed: %v", err)
	}
}

func TestCLIAnnotationGetMapsNoSuchAnnotation(t *testing.T) {
	runner := &fakeCommandRunner{
		stderr: []byte("An error occurred (NoSuchAnnotation) when calling GetObjectAnnotation"),
		err:    errors.New("exit status 254"),
	}
	store := &CLIAnnotationStore{Runner: runner}
	_, err := store.Get(context.Background(), "bucket", "key", sourceAnnotationName)
	if !errors.Is(err, ErrAnnotationNotFound) {
		t.Fatalf("Get() error = %v, want ErrAnnotationNotFound", err)
	}
}
