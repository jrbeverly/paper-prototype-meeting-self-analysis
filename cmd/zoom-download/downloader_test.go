package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"sort"
	"strings"
	"testing"
	"time"
)

type fakeZoom struct {
	meetings      []ZoomMeeting
	detail        ZoomMeetingDetail
	listCalls     int
	detailCalls   int
	downloadCalls []string
	bodies        map[string][]byte
}

func (f *fakeZoom) ListRecordings(
	context.Context,
	string,
	time.Time,
	time.Time,
) ([]ZoomMeeting, error) {
	f.listCalls++
	return append([]ZoomMeeting(nil), f.meetings...), nil
}

func (f *fakeZoom) GetMeeting(context.Context, string) (ZoomMeetingDetail, error) {
	f.detailCalls++
	return f.detail, nil
}

func (f *fakeZoom) Download(_ context.Context, endpoint string) (io.ReadCloser, int64, error) {
	f.downloadCalls = append(f.downloadCalls, endpoint)
	payload, exists := f.bodies[endpoint]
	if !exists {
		return nil, 0, errors.New("unexpected download")
	}
	return io.NopCloser(bytes.NewReader(payload)), int64(len(payload)), nil
}

type fakeObjectStore struct {
	objects map[string]ObjectInfo
	payload map[string][]byte
	json    map[string][]byte
}

func newFakeObjectStore() *fakeObjectStore {
	return &fakeObjectStore{
		objects: make(map[string]ObjectInfo),
		payload: make(map[string][]byte),
		json:    make(map[string][]byte),
	}
}

func (f *fakeObjectStore) Head(_ context.Context, bucket, key string) (ObjectInfo, error) {
	object, exists := f.objects[bucket+"/"+key]
	if !exists {
		return ObjectInfo{}, nil
	}
	return object, nil
}

func (f *fakeObjectStore) Upload(
	_ context.Context,
	bucket string,
	key string,
	body io.Reader,
	_ int64,
) error {
	payload, err := io.ReadAll(body)
	if err != nil {
		return err
	}
	fullKey := bucket + "/" + key
	f.payload[fullKey] = payload
	f.objects[fullKey] = ObjectInfo{
		Exists: true,
		Size:   int64(len(payload)),
		ETag:   `"etag-` + shortSHA(key)[:8] + `"`,
	}
	return nil
}

func (f *fakeObjectStore) PutJSON(_ context.Context, bucket, key string, payload []byte) error {
	f.json[bucket+"/"+key] = append([]byte(nil), payload...)
	return nil
}

type fakeAnnotations struct {
	payload map[string][]byte
	puts    []annotationPut
	putErr  error
}

type annotationPut struct {
	bucket  string
	key     string
	name    string
	etag    string
	payload []byte
}

func newFakeAnnotations() *fakeAnnotations {
	return &fakeAnnotations{payload: make(map[string][]byte)}
}

func (f *fakeAnnotations) Get(_ context.Context, bucket, key, name string) ([]byte, error) {
	payload, exists := f.payload[bucket+"/"+key+"/"+name]
	if !exists {
		return nil, ErrAnnotationNotFound
	}
	return append([]byte(nil), payload...), nil
}

func (f *fakeAnnotations) Put(
	_ context.Context,
	bucket string,
	key string,
	name string,
	etag string,
	payload []byte,
) error {
	if f.putErr != nil {
		return f.putErr
	}
	f.puts = append(f.puts, annotationPut{
		bucket: bucket, key: key, name: name, etag: etag, payload: append([]byte(nil), payload...),
	})
	f.payload[bucket+"/"+key+"/"+name] = append([]byte(nil), payload...)
	return nil
}

type fakeQuarantine struct {
	records []QuarantineRecord
}

func (f *fakeQuarantine) Put(_ context.Context, record QuarantineRecord) error {
	f.records = append(f.records, record)
	return nil
}

func validMeeting(files ...ZoomRecordingFile) ZoomMeeting {
	return ZoomMeeting{
		ID:        "987654321",
		UUID:      "meeting-occurrence-uuid",
		Topic:     "Payments daily",
		StartTime: "2026-07-23T13:00:00Z",
		Agenda: "scope=team\n" +
			"team=payments\n" +
			"meeting_type=daily-standup\n" +
			"jira_project=PAY\n",
		RecordingFiles: files,
	}
}

func completedFile(id, endpoint, start, end, payload string) ZoomRecordingFile {
	return ZoomRecordingFile{
		ID:             id,
		FileType:       "MP4",
		FileSize:       int64(len(payload)),
		Status:         "completed",
		RecordingStart: start,
		RecordingEnd:   end,
		DownloadURL:    endpoint,
	}
}

func testDownloader(
	zoom *fakeZoom,
	objects *fakeObjectStore,
	annotations *fakeAnnotations,
	quarantine *fakeQuarantine,
) *Downloader {
	return &Downloader{
		Config: DownloadConfig{
			Users:            []string{"me"},
			From:             time.Date(2026, 7, 23, 0, 0, 0, 0, time.UTC),
			To:               time.Date(2026, 7, 23, 0, 0, 0, 0, time.UTC),
			Bucket:           "bucket",
			Prefix:           "meetings",
			QuarantinePrefix: "quarantine",
		},
		Zoom:        zoom,
		Objects:     objects,
		Annotations: annotations,
		Quarantine:  quarantine,
		Logger:      log.New(io.Discard, "", 0),
		Now: func() time.Time {
			return time.Date(2026, 7, 24, 0, 0, 0, 0, time.UTC)
		},
	}
}

func TestDownloaderPreservesAllCompletedSplitMP4s(t *testing.T) {
	first := completedFile(
		"recording-file-b", "https://download/b",
		"2026-07-23T13:20:00Z", "2026-07-23T13:40:00Z", "BBBB",
	)
	second := completedFile(
		"recording-file-a", "https://download/a",
		"2026-07-23T13:00:00Z", "2026-07-23T13:20:00Z", "AAAAA",
	)
	zoom := &fakeZoom{
		meetings: []ZoomMeeting{validMeeting(first, second, ZoomRecordingFile{
			ID:       "audio",
			FileType: "M4A",
			Status:   "completed",
		}, ZoomRecordingFile{
			ID:       "processing-video",
			FileType: "MP4",
			Status:   "processing",
		})},
		bodies: map[string][]byte{
			"https://download/a": []byte("AAAAA"),
			"https://download/b": []byte("BBBB"),
		},
	}
	objects := newFakeObjectStore()
	annotations := newFakeAnnotations()
	downloader := testDownloader(zoom, objects, annotations, &fakeQuarantine{})

	summary, err := downloader.Run(context.Background())
	if err != nil {
		t.Fatalf("Run() error = %v", err)
	}
	if summary.FilesUploaded != 2 || len(objects.payload) != 2 || len(annotations.puts) != 2 {
		t.Fatalf("summary = %#v, objects = %d, annotations = %d",
			summary, len(objects.payload), len(annotations.puts))
	}
	sort.Strings(zoom.downloadCalls)
	if got := strings.Join(zoom.downloadCalls, ","); got != "https://download/a,https://download/b" {
		t.Fatalf("downloads = %q", got)
	}

	var sourceAnnotations []SourceAnnotation
	for _, write := range annotations.puts {
		if write.name != sourceAnnotationName || write.etag == "" {
			t.Fatalf("annotation write missing name/ETag: %#v", write)
		}
		var annotation SourceAnnotation
		if err := json.Unmarshal(write.payload, &annotation); err != nil {
			t.Fatal(err)
		}
		var wire map[string]any
		if err := json.Unmarshal(write.payload, &wire); err != nil {
			t.Fatal(err)
		}
		wireMetadata, ok := wire["metadata"].(map[string]any)
		if !ok || wireMetadata["teamId"] != "payments" || wireMetadata["pipeline"] != "aggregation" {
			t.Fatalf("wire metadata is not the flattened consumer contract: %#v", wire["metadata"])
		}
		if _, nestedRouting := wireMetadata["routing"]; nestedRouting {
			t.Fatalf("wire metadata unexpectedly contains nested routing: %#v", wireMetadata)
		}
		if wire["sourcePrefix"] != occurrencePrefix("meetings", annotation.OccurrenceID) {
			t.Fatalf("sourcePrefix = %#v", wire["sourcePrefix"])
		}
		if _, meetingIDIsString := wire["meetingId"].(string); !meetingIDIsString {
			t.Fatalf("meetingId must preserve its exact value as a JSON string: %#v", wire["meetingId"])
		}
		sourceAnnotations = append(sourceAnnotations, annotation)
		if got := strings.Join(annotation.ExpectedMP4FileIDs, ","); got != "recording-file-a,recording-file-b" {
			t.Fatalf("expected files = %q", got)
		}
	}
	if sourceAnnotations[0].RecordingSetFingerprint != sourceAnnotations[1].RecordingSetFingerprint {
		t.Fatal("split file annotations have different recording-set fingerprints")
	}
	if sourceAnnotations[0].RecordingFileID == sourceAnnotations[1].RecordingFileID {
		t.Fatal("split file annotations have the same recording file ID")
	}
}

func TestDownloaderQuarantinesInvalidMetadataWithoutUploading(t *testing.T) {
	file := completedFile(
		"recording-file", "https://download/video",
		"2026-07-23T13:00:00Z", "2026-07-23T13:10:00Z", "video",
	)
	meeting := validMeeting(file)
	meeting.Agenda = "scope=team\nmeeting_type=symposium\n"
	zoom := &fakeZoom{meetings: []ZoomMeeting{meeting}, bodies: map[string][]byte{}}
	objects := newFakeObjectStore()
	annotations := newFakeAnnotations()
	quarantine := &fakeQuarantine{}
	downloader := testDownloader(zoom, objects, annotations, quarantine)

	summary, err := downloader.Run(context.Background())
	if err != nil {
		t.Fatalf("Run() error = %v", err)
	}
	if summary.Quarantined != 1 || len(quarantine.records) != 1 {
		t.Fatalf("summary = %#v, records = %#v", summary, quarantine.records)
	}
	if len(zoom.downloadCalls) != 0 || len(objects.payload) != 0 || len(annotations.puts) != 0 {
		t.Fatal("invalid occurrence performed recording work")
	}
	record := quarantine.records[0]
	if record.State != "failed" || record.Reason != "invalid-routing-metadata" || len(record.Issues) == 0 {
		t.Fatalf("quarantine record = %#v", record)
	}
}

func TestDownloaderSkipsCompleteObjectAndRepairsMissingAnnotation(t *testing.T) {
	skipFile := completedFile(
		"already-complete", "https://download/skip",
		"2026-07-23T13:00:00Z", "2026-07-23T13:10:00Z", "skip!",
	)
	repairFile := completedFile(
		"annotation-missing", "https://download/repair",
		"2026-07-23T13:10:00Z", "2026-07-23T13:20:00Z", "fixme!",
	)
	meeting := validMeeting(skipFile, repairFile)
	zoom := &fakeZoom{meetings: []ZoomMeeting{meeting}, bodies: map[string][]byte{}}
	objects := newFakeObjectStore()
	annotations := newFakeAnnotations()
	quarantine := &fakeQuarantine{}
	downloader := testDownloader(zoom, objects, annotations, quarantine)

	metadata, err := ParseMeetingMetadata(meeting.Agenda)
	if err != nil {
		t.Fatal(err)
	}
	start, _ := time.Parse(time.RFC3339, meeting.StartTime)
	occurrence := occurrenceID(meeting, metadata, start)
	expectedIDs := []string{"already-complete", "annotation-missing"}
	fingerprint := recordingSetFingerprint(expectedIDs)
	for _, file := range []ZoomRecordingFile{skipFile, repairFile} {
		key := recordingKey("meetings", occurrence, file.ID)
		objects.objects["bucket/"+key] = ObjectInfo{
			Exists: true,
			Size:   file.FileSize,
			ETag:   `"etag-` + file.ID + `"`,
		}
	}
	skipKey := recordingKey("meetings", occurrence, skipFile.ID)
	completeAnnotation := makeSourceAnnotation(
		meeting, metadata, skipFile, occurrence, occurrencePrefix("meetings", occurrence), expectedIDs, fingerprint,
	)
	completePayload, _ := json.Marshal(completeAnnotation)
	annotations.payload["bucket/"+skipKey+"/"+sourceAnnotationName] = completePayload

	summary, err := downloader.Run(context.Background())
	if err != nil {
		t.Fatalf("Run() error = %v", err)
	}
	if summary.FilesSkipped != 1 || summary.AnnotationsRepaired != 1 || summary.FilesUploaded != 0 {
		t.Fatalf("summary = %#v", summary)
	}
	if len(zoom.downloadCalls) != 0 {
		t.Fatalf("download calls = %#v, want none", zoom.downloadCalls)
	}
	if len(annotations.puts) != 1 || annotations.puts[0].etag != `"etag-annotation-missing"` {
		t.Fatalf("annotation repairs = %#v", annotations.puts)
	}
}

func TestDownloaderSecondRunIsIdempotent(t *testing.T) {
	file := completedFile(
		"recording-file", "https://download/video",
		"2026-07-23T13:00:00Z", "2026-07-23T13:10:00Z", "video",
	)
	zoom := &fakeZoom{
		meetings: []ZoomMeeting{validMeeting(file)},
		bodies:   map[string][]byte{"https://download/video": []byte("video")},
	}
	objects := newFakeObjectStore()
	annotations := newFakeAnnotations()
	downloader := testDownloader(zoom, objects, annotations, &fakeQuarantine{})

	first, err := downloader.Run(context.Background())
	if err != nil {
		t.Fatalf("first Run() error = %v", err)
	}
	second, err := downloader.Run(context.Background())
	if err != nil {
		t.Fatalf("second Run() error = %v", err)
	}
	if first.FilesUploaded != 1 || second.FilesUploaded != 0 || second.FilesSkipped != 1 {
		t.Fatalf("first = %#v; second = %#v", first, second)
	}
	if len(zoom.downloadCalls) != 1 || len(annotations.puts) != 1 {
		t.Fatalf("downloads = %d, annotation writes = %d; want one total",
			len(zoom.downloadCalls), len(annotations.puts))
	}
}

func TestDownloaderRepairsUploadAnnotationCrashWindow(t *testing.T) {
	file := completedFile(
		"recording-file", "https://download/video",
		"2026-07-23T13:00:00Z", "2026-07-23T13:10:00Z", "video",
	)
	zoom := &fakeZoom{
		meetings: []ZoomMeeting{validMeeting(file)},
		bodies:   map[string][]byte{"https://download/video": []byte("video")},
	}
	objects := newFakeObjectStore()
	annotations := newFakeAnnotations()
	annotations.putErr = errors.New("simulated annotation outage")
	downloader := testDownloader(zoom, objects, annotations, &fakeQuarantine{})

	first, err := downloader.Run(context.Background())
	if err == nil {
		t.Fatal("first Run() error = nil, want simulated crash-window failure")
	}
	if first.FilesUploaded != 0 || len(objects.payload) != 1 || len(zoom.downloadCalls) != 1 {
		t.Fatalf("first = %#v, objects = %d, downloads = %d",
			first, len(objects.payload), len(zoom.downloadCalls))
	}

	annotations.putErr = nil
	second, err := downloader.Run(context.Background())
	if err != nil {
		t.Fatalf("second Run() error = %v", err)
	}
	if second.AnnotationsRepaired != 1 || second.FilesUploaded != 0 {
		t.Fatalf("second = %#v", second)
	}
	if len(zoom.downloadCalls) != 1 {
		t.Fatalf("recording was downloaded again: %#v", zoom.downloadCalls)
	}
}

func TestZoomWindowsSplitsWideRange(t *testing.T) {
	from := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	to := time.Date(2026, 3, 5, 0, 0, 0, 0, time.UTC)
	windows := zoomWindows(from, to)
	if len(windows) != 3 {
		t.Fatalf("windows = %#v", windows)
	}
	for _, window := range windows {
		if days := int(window.to.Sub(window.from).Hours()/24) + 1; days > 30 {
			t.Fatalf("window has %d inclusive days: %#v", days, window)
		}
	}
	if !windows[0].from.Equal(from) || !windows[len(windows)-1].to.Equal(to) {
		t.Fatalf("windows do not cover endpoints: %#v", windows)
	}
}
