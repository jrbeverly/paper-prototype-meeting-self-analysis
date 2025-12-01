package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/feature/s3/manager"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/smithy-go"
	smithyhttp "github.com/aws/smithy-go/transport/http"
)

type ObjectInfo struct {
	Exists bool
	Size   int64
	ETag   string
}

type ObjectStore interface {
	Head(context.Context, string, string) (ObjectInfo, error)
	Upload(context.Context, string, string, io.Reader, int64) error
	PutJSON(context.Context, string, string, []byte) error
}

type s3HeadPutter interface {
	HeadObject(context.Context, *s3.HeadObjectInput, ...func(*s3.Options)) (*s3.HeadObjectOutput, error)
	PutObject(context.Context, *s3.PutObjectInput, ...func(*s3.Options)) (*s3.PutObjectOutput, error)
}

type multipartUploader interface {
	Upload(context.Context, *s3.PutObjectInput, ...func(*manager.Uploader)) (*manager.UploadOutput, error)
}

type AWSObjectStore struct {
	Client              s3HeadPutter
	Uploader            multipartUploader
	ExpectedBucketOwner string
}

func (s *AWSObjectStore) Head(ctx context.Context, bucket string, key string) (ObjectInfo, error) {
	input := &s3.HeadObjectInput{
		Bucket: aws.String(bucket),
		Key:    aws.String(key),
	}
	if s.ExpectedBucketOwner != "" {
		input.ExpectedBucketOwner = aws.String(s.ExpectedBucketOwner)
	}
	output, err := s.Client.HeadObject(ctx, input)
	if err != nil {
		if isNotFound(err) {
			return ObjectInfo{}, nil
		}
		return ObjectInfo{}, fmt.Errorf("head s3://%s/%s: %w", bucket, key, err)
	}
	return ObjectInfo{
		Exists: true,
		Size:   aws.ToInt64(output.ContentLength),
		ETag:   aws.ToString(output.ETag),
	}, nil
}

func (s *AWSObjectStore) Upload(
	ctx context.Context,
	bucket string,
	key string,
	body io.Reader,
	contentLength int64,
) error {
	input := &s3.PutObjectInput{
		Bucket:      aws.String(bucket),
		Key:         aws.String(key),
		Body:        body,
		ContentType: aws.String("video/mp4"),
	}
	if contentLength >= 0 {
		input.ContentLength = aws.Int64(contentLength)
	}
	if s.ExpectedBucketOwner != "" {
		input.ExpectedBucketOwner = aws.String(s.ExpectedBucketOwner)
	}
	if _, err := s.Uploader.Upload(ctx, input); err != nil {
		return fmt.Errorf("multipart upload s3://%s/%s: %w", bucket, key, err)
	}
	return nil
}

func (s *AWSObjectStore) PutJSON(ctx context.Context, bucket string, key string, payload []byte) error {
	input := &s3.PutObjectInput{
		Bucket:      aws.String(bucket),
		Key:         aws.String(key),
		Body:        bytes.NewReader(payload),
		ContentType: aws.String("application/json"),
	}
	if s.ExpectedBucketOwner != "" {
		input.ExpectedBucketOwner = aws.String(s.ExpectedBucketOwner)
	}
	_, err := s.Client.PutObject(ctx, input)
	if err != nil {
		return fmt.Errorf("put s3://%s/%s: %w", bucket, key, err)
	}
	return nil
}

func isNotFound(err error) bool {
	var responseError *smithyhttp.ResponseError
	if errors.As(err, &responseError) && responseError.HTTPStatusCode() == 404 {
		return true
	}
	var apiError smithy.APIError
	if errors.As(err, &apiError) {
		return apiError.ErrorCode() == "NotFound" || apiError.ErrorCode() == "NoSuchKey"
	}
	return false
}
