package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/feature/s3/manager"
	"github.com/aws/aws-sdk-go-v2/service/s3"
)

func main() {
	os.Exit(realMain(os.Args[1:]))
}

func realMain(args []string) int {
	cfg, err := parseConfig(args, time.Now().UTC())
	if err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		fmt.Fprintln(os.Stderr, "zoom-download:", err)
		return 2
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	httpClient := &http.Client{
		Transport: &http.Transport{
			Proxy:                 http.ProxyFromEnvironment,
			DialContext:           (&net.Dialer{Timeout: 15 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
			ForceAttemptHTTP2:     true,
			MaxIdleConns:          32,
			IdleConnTimeout:       90 * time.Second,
			TLSHandshakeTimeout:   15 * time.Second,
			ResponseHeaderTimeout: 30 * time.Second,
		},
	}
	awsOptions := []func(*awsconfig.LoadOptions) error{}
	if cfg.AWSRegion != "" {
		awsOptions = append(awsOptions, awsconfig.WithRegion(cfg.AWSRegion))
	}
	awsCfg, err := awsconfig.LoadDefaultConfig(ctx, awsOptions...)
	if err != nil {
		fmt.Fprintln(os.Stderr, "zoom-download: load AWS configuration:", err)
		return 1
	}
	s3Client := s3.NewFromConfig(awsCfg)
	uploader := manager.NewUploader(s3Client, func(u *manager.Uploader) {
		u.PartSize = cfg.UploadPartSizeMiB * 1024 * 1024
		u.Concurrency = cfg.UploadConcurrency
		u.LeavePartsOnError = false
	})
	objects := &AWSObjectStore{
		Client:              s3Client,
		Uploader:            uploader,
		ExpectedBucketOwner: cfg.ExpectedBucketOwner,
	}
	tokens := &BrokerTokenSource{
		URL:     cfg.TokenURL,
		Method:  cfg.TokenMethod,
		Headers: cfg.TokenHeaders,
		Client:  httpClient,
	}
	zoom := &ZoomClient{BaseURL: cfg.ZoomAPIBase, Tokens: tokens, Client: httpClient}
	annotations := &CLIAnnotationStore{
		AWSCLI:              cfg.AWSCLI,
		Region:              cfg.AWSRegion,
		ExpectedBucketOwner: cfg.ExpectedBucketOwner,
	}
	quarantine := &S3QuarantineSink{
		Objects: objects,
		Bucket:  cfg.Download.Bucket,
		Prefix:  cfg.Download.QuarantinePrefix,
	}
	logger := log.New(os.Stderr, "zoom-download: ", log.Ldate|log.Ltime|log.LUTC)
	downloader := &Downloader{
		Config:      cfg.Download,
		Zoom:        zoom,
		Objects:     objects,
		Annotations: annotations,
		Quarantine:  quarantine,
		Logger:      logger,
	}
	summary, runErr := downloader.Run(ctx)
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(summary); err != nil {
		fmt.Fprintln(os.Stderr, "zoom-download: encode summary:", err)
		return 1
	}
	if runErr != nil {
		fmt.Fprintln(os.Stderr, "zoom-download:", runErr)
		return 1
	}
	return 0
}
