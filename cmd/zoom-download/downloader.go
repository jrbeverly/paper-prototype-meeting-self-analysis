package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/url"
	"path"
	"reflect"
	"sort"
	"strings"
	"time"
)

type DownloadConfig struct {
	Users            []string
	From             time.Time
	To               time.Time
	Bucket           string
	Prefix           string
	QuarantinePrefix string
}

type Summary struct {
	UsersScanned        int `json:"usersScanned"`
	MeetingsSeen        int `json:"meetingsSeen"`
	OccurrencesHandled  int `json:"occurrencesHandled"`
	FilesUploaded       int `json:"filesUploaded"`
	FilesSkipped        int `json:"filesSkipped"`
	AnnotationsRepaired int `json:"annotationsRepaired"`
	Quarantined         int `json:"quarantined"`
}

type SourceAnnotation struct {
	Version                 int             `json:"v"`
	Kind                    string          `json:"kind"`
	OccurrenceID            string          `json:"occurrenceId"`
	MeetingUUID             string          `json:"meetingUuid"`
	MeetingID               string          `json:"meetingId"`
	RecordingFileID         string          `json:"recordingFileId"`
	ExpectedMP4FileIDs      []string        `json:"expectedMp4FileIds"`
	RecordingSetFingerprint string          `json:"recordingSetFingerprint"`
	RecordedAt              string          `json:"recordedAt"`
	DurationSeconds         int64           `json:"durationSeconds"`
	Topic                   string          `json:"topic,omitempty"`
	Metadata                MeetingMetadata `json:"metadata"`
	SourcePrefix            string          `json:"sourcePrefix"`
}

type QuarantineRecord struct {
	Version             int               `json:"v"`
	State               string            `json:"state"`
	Reason              string            `json:"reason"`
	Issues              []string          `json:"issues"`
	MeetingID           string            `json:"meetingId"`
	MeetingUUID         string            `json:"meetingUuid"`
	Topic               string            `json:"topic"`
	StartTime           string            `json:"startTime"`
	Description         string            `json:"description"`
	ParsedMetadata      map[string]string `json:"parsedMetadata,omitempty"`
	CompletedMP4FileIDs []string          `json:"completedMp4FileIds"`
	ObservedAt          string            `json:"observedAt"`
}

type QuarantineSink interface {
	Put(context.Context, QuarantineRecord) error
}

type S3QuarantineSink struct {
	Objects ObjectStore
	Bucket  string
	Prefix  string
}

func (s *S3QuarantineSink) Put(ctx context.Context, record QuarantineRecord) error {
	payload, err := json.Marshal(record)
	if err != nil {
		return fmt.Errorf("encode quarantine record: %w", err)
	}
	identity := record.MeetingUUID
	if identity == "" {
		identity = record.MeetingID + "|" + record.StartTime
	}
	descriptionFingerprint := shortSHA(identity + "|" + record.Description)
	key := path.Join(strings.Trim(s.Prefix, "/"), "zoom", shortSHA(identity), descriptionFingerprint+".json")
	return s.Objects.PutJSON(ctx, s.Bucket, key, payload)
}

type Downloader struct {
	Config      DownloadConfig
	Zoom        ZoomService
	Objects     ObjectStore
	Annotations AnnotationStore
	Quarantine  QuarantineSink
	Logger      *log.Logger
	Now         func() time.Time
}

func (d *Downloader) Run(ctx context.Context) (Summary, error) {
	var summary Summary
	var runErrors []error
	meetingsByIdentity := make(map[string]ZoomMeeting)
	for _, window := range zoomWindows(d.Config.From, d.Config.To) {
		for _, user := range d.Config.Users {
			summary.UsersScanned++
			meetings, err := d.Zoom.ListRecordings(ctx, user, window.from, window.to)
			if err != nil {
				runErrors = append(runErrors, fmt.Errorf("list recordings for user %q (%s through %s): %w",
					user, window.from.Format("2006-01-02"), window.to.Format("2006-01-02"), err))
				continue
			}
			for _, meeting := range meetings {
				summary.MeetingsSeen++
				identity := meetingIdentity(meeting)
				if existing, exists := meetingsByIdentity[identity]; exists {
					meetingsByIdentity[identity] = mergeZoomMeetings(existing, meeting)
				} else {
					meetingsByIdentity[identity] = meeting
				}
			}
		}
	}

	meetings := make([]ZoomMeeting, 0, len(meetingsByIdentity))
	for _, meeting := range meetingsByIdentity {
		meetings = append(meetings, meeting)
	}
	sort.Slice(meetings, func(i, j int) bool {
		if meetings[i].StartTime != meetings[j].StartTime {
			return meetings[i].StartTime < meetings[j].StartTime
		}
		return meetingIdentity(meetings[i]) < meetingIdentity(meetings[j])
	})

	detailCache := make(map[string]ZoomMeetingDetail)
	for _, meeting := range meetings {
		files := completedMP4s(meeting.RecordingFiles)
		if len(files) == 0 {
			continue
		}
		description := meeting.Agenda
		if description == "" {
			meetingID := string(meeting.ID)
			detail, exists := detailCache[meetingID]
			if !exists {
				var err error
				detail, err = d.Zoom.GetMeeting(ctx, meetingID)
				if err != nil {
					runErrors = append(runErrors, fmt.Errorf("get metadata for Zoom meeting %q: %w", meetingID, err))
					continue
				}
				detailCache[meetingID] = detail
			}
			description = detail.Agenda
		}

		metadata, metadataErr := ParseMeetingMetadata(description)
		if metadataErr != nil {
			var validationErr *MetadataValidationError
			if !errors.As(metadataErr, &validationErr) {
				runErrors = append(runErrors, metadataErr)
				continue
			}
			record := d.quarantineRecord(meeting, files, description, metadata.Raw, validationErr.Issues)
			if err := d.Quarantine.Put(ctx, record); err != nil {
				runErrors = append(runErrors, fmt.Errorf("quarantine Zoom occurrence %q: %w", meetingIdentity(meeting), err))
				continue
			}
			summary.Quarantined++
			d.logf("quarantined invalid Zoom metadata meeting_uuid=%q issues=%q", meeting.UUID, validationErr.Issues)
			continue
		}

		if err := validateRecordingFiles(files); err != nil {
			runErrors = append(runErrors, fmt.Errorf("invalid completed MP4 set for meeting %q: %w", meetingIdentity(meeting), err))
			continue
		}
		if err := d.processOccurrence(ctx, meeting, metadata, files, &summary); err != nil {
			runErrors = append(runErrors, fmt.Errorf("process Zoom occurrence %q: %w", meetingIdentity(meeting), err))
			continue
		}
		summary.OccurrencesHandled++
	}
	return summary, errors.Join(runErrors...)
}

func (d *Downloader) processOccurrence(
	ctx context.Context,
	meeting ZoomMeeting,
	metadata MeetingMetadata,
	files []ZoomRecordingFile,
	summary *Summary,
) error {
	if strings.TrimSpace(meeting.UUID) == "" {
		return errors.New("completed recording occurrence has no Zoom meeting UUID")
	}
	start, err := time.Parse(time.RFC3339, meeting.StartTime)
	if err != nil {
		return fmt.Errorf("start_time %q is not RFC3339: %w", meeting.StartTime, err)
	}
	occurrenceID := occurrenceID(meeting, metadata, start)
	sourcePrefix := occurrencePrefix(d.Config.Prefix, occurrenceID)
	expectedIDs := make([]string, len(files))
	for index := range files {
		expectedIDs[index] = files[index].ID
	}
	sort.Strings(expectedIDs)
	setFingerprint := recordingSetFingerprint(expectedIDs)

	sort.Slice(files, func(i, j int) bool {
		if files[i].RecordingStart != files[j].RecordingStart {
			return files[i].RecordingStart < files[j].RecordingStart
		}
		return files[i].ID < files[j].ID
	})
	for _, file := range files {
		key := recordingKey(d.Config.Prefix, occurrenceID, file.ID)
		annotation := makeSourceAnnotation(
			meeting, metadata, file, occurrenceID, sourcePrefix, expectedIDs, setFingerprint,
		)
		payload, err := json.Marshal(annotation)
		if err != nil {
			return fmt.Errorf("encode source annotation for recording %q: %w", file.ID, err)
		}

		object, err := d.Objects.Head(ctx, d.Config.Bucket, key)
		if err != nil {
			return err
		}
		sizeMatches := file.FileSize <= 0 || (object.Exists && object.Size == file.FileSize)
		if object.Exists && sizeMatches {
			existingPayload, annotationErr := d.Annotations.Get(
				ctx, d.Config.Bucket, key, sourceAnnotationName,
			)
			if annotationErr == nil && sourceAnnotationMatches(existingPayload, annotation) {
				summary.FilesSkipped++
				d.logf("recording already complete key=%q recording_file_id=%q", key, file.ID)
				continue
			}
			if annotationErr != nil && !errors.Is(annotationErr, ErrAnnotationNotFound) {
				return annotationErr
			}
			if object.ETag == "" {
				return fmt.Errorf("HeadObject returned no ETag for s3://%s/%s", d.Config.Bucket, key)
			}
			if err := d.Annotations.Put(
				ctx, d.Config.Bucket, key, sourceAnnotationName, object.ETag, payload,
			); err != nil {
				return err
			}
			summary.AnnotationsRepaired++
			d.logf("repaired source annotation key=%q recording_file_id=%q", key, file.ID)
			continue
		}

		if file.DownloadURL == "" {
			return fmt.Errorf("recording file %q needs upload but has no download_url", file.ID)
		}
		body, responseLength, err := d.Zoom.Download(ctx, file.DownloadURL)
		if err != nil {
			return fmt.Errorf("download recording file %q: %w", file.ID, err)
		}
		contentLength := responseLength
		if file.FileSize > 0 {
			contentLength = file.FileSize
		}
		uploadErr := d.Objects.Upload(ctx, d.Config.Bucket, key, body, contentLength)
		closeErr := body.Close()
		if uploadErr != nil {
			return uploadErr
		}
		if closeErr != nil {
			return fmt.Errorf("close Zoom recording %q response: %w", file.ID, closeErr)
		}

		object, err = d.Objects.Head(ctx, d.Config.Bucket, key)
		if err != nil {
			return err
		}
		if !object.Exists {
			return fmt.Errorf("uploaded object s3://%s/%s is not visible to HeadObject", d.Config.Bucket, key)
		}
		if file.FileSize > 0 && object.Size != file.FileSize {
			return fmt.Errorf("uploaded object s3://%s/%s has size %d; expected %d",
				d.Config.Bucket, key, object.Size, file.FileSize)
		}
		if object.ETag == "" {
			return fmt.Errorf("uploaded object s3://%s/%s has no ETag", d.Config.Bucket, key)
		}
		if err := d.Annotations.Put(
			ctx, d.Config.Bucket, key, sourceAnnotationName, object.ETag, payload,
		); err != nil {
			return err
		}
		summary.FilesUploaded++
		d.logf("uploaded recording key=%q recording_file_id=%q", key, file.ID)
	}
	return nil
}

func completedMP4s(files []ZoomRecordingFile) []ZoomRecordingFile {
	result := make([]ZoomRecordingFile, 0, len(files))
	for _, file := range files {
		if strings.EqualFold(file.FileType, "MP4") && strings.EqualFold(file.Status, "completed") {
			result = append(result, file)
		}
	}
	return result
}

func validateRecordingFiles(files []ZoomRecordingFile) error {
	seen := make(map[string]struct{}, len(files))
	for _, file := range files {
		if strings.TrimSpace(file.ID) == "" {
			return errors.New("completed MP4 has no recording file ID")
		}
		if _, duplicate := seen[file.ID]; duplicate {
			return fmt.Errorf("duplicate recording file ID %q", file.ID)
		}
		seen[file.ID] = struct{}{}
		if file.FileSize < 0 {
			return fmt.Errorf("recording file %q has negative file size", file.ID)
		}
	}
	return nil
}

func makeSourceAnnotation(
	meeting ZoomMeeting,
	metadata MeetingMetadata,
	file ZoomRecordingFile,
	occurrenceID string,
	sourcePrefix string,
	expectedIDs []string,
	setFingerprint string,
) SourceAnnotation {
	recordedAt := file.RecordingStart
	if _, err := time.Parse(time.RFC3339, recordedAt); err != nil {
		recordedAt = meeting.StartTime
	}
	duration := int64(0)
	recordingStart, startErr := time.Parse(time.RFC3339, file.RecordingStart)
	recordingEnd, endErr := time.Parse(time.RFC3339, file.RecordingEnd)
	if startErr == nil && endErr == nil && recordingEnd.After(recordingStart) {
		duration = int64(recordingEnd.Sub(recordingStart).Round(time.Second) / time.Second)
	} else if len(meeting.RecordingFiles) == 1 && meeting.Duration > 0 {
		duration = meeting.Duration * 60
	}
	return SourceAnnotation{
		Version:                 1,
		Kind:                    "zoom-recording",
		OccurrenceID:            occurrenceID,
		MeetingUUID:             meeting.UUID,
		MeetingID:               string(meeting.ID),
		RecordingFileID:         file.ID,
		ExpectedMP4FileIDs:      append([]string(nil), expectedIDs...),
		RecordingSetFingerprint: setFingerprint,
		RecordedAt:              recordedAt,
		DurationSeconds:         duration,
		Topic:                   meeting.Topic,
		Metadata:                metadata,
		SourcePrefix:            sourcePrefix,
	}
}

func sourceAnnotationMatches(payload []byte, expected SourceAnnotation) bool {
	var actual SourceAnnotation
	if err := json.Unmarshal(payload, &actual); err != nil {
		return false
	}
	return reflect.DeepEqual(actual, expected)
}

func occurrenceID(meeting ZoomMeeting, metadata MeetingMetadata, start time.Time) string {
	owner := metadata.Team
	if metadata.Scope == "central" {
		owner = "central"
	}
	return fmt.Sprintf("%s-%s-%s-%s",
		owner,
		metadata.MeetingType,
		start.UTC().Format("2006-01-02T150405Z"),
		shortSHA(meeting.UUID)[:10],
	)
}

func recordingKey(prefix string, occurrenceID string, recordingFileID string) string {
	return occurrencePrefix(prefix, occurrenceID) + safePathSegment(recordingFileID) + ".mp4"
}

func occurrencePrefix(prefix string, occurrenceID string) string {
	return path.Join(strings.Trim(prefix, "/"), occurrenceID) + "/"
}

func safePathSegment(value string) string {
	escaped := url.PathEscape(value)
	if escaped == value && value != "." && value != ".." {
		return value
	}
	return escaped + "-" + shortSHA(value)[:8]
}

func recordingSetFingerprint(sortedFileIDs []string) string {
	ids := append([]string(nil), sortedFileIDs...)
	sort.Strings(ids)
	sum := sha256.Sum256([]byte(strings.Join(ids, "\n")))
	return "sha256:" + hex.EncodeToString(sum[:])
}

func meetingIdentity(meeting ZoomMeeting) string {
	if meeting.UUID != "" {
		return meeting.UUID
	}
	return string(meeting.ID) + "|" + meeting.StartTime
}

func mergeZoomMeetings(first, second ZoomMeeting) ZoomMeeting {
	merged := first
	if merged.ID == "" {
		merged.ID = second.ID
	}
	if merged.UUID == "" {
		merged.UUID = second.UUID
	}
	if merged.Topic == "" {
		merged.Topic = second.Topic
	}
	if merged.Agenda == "" {
		merged.Agenda = second.Agenda
	}
	if merged.StartTime == "" {
		merged.StartTime = second.StartTime
	}
	if merged.Duration == 0 {
		merged.Duration = second.Duration
	}
	files := make(map[string]ZoomRecordingFile, len(first.RecordingFiles)+len(second.RecordingFiles))
	var withoutID []ZoomRecordingFile
	for _, file := range append(append([]ZoomRecordingFile(nil), first.RecordingFiles...), second.RecordingFiles...) {
		if file.ID == "" {
			withoutID = append(withoutID, file)
			continue
		}
		existing, exists := files[file.ID]
		if !exists || (strings.EqualFold(file.Status, "completed") && !strings.EqualFold(existing.Status, "completed")) {
			files[file.ID] = file
		}
	}
	merged.RecordingFiles = withoutID
	for _, file := range files {
		merged.RecordingFiles = append(merged.RecordingFiles, file)
	}
	sort.Slice(merged.RecordingFiles, func(i, j int) bool {
		return merged.RecordingFiles[i].ID < merged.RecordingFiles[j].ID
	})
	return merged
}

func shortSHA(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

func (d *Downloader) quarantineRecord(
	meeting ZoomMeeting,
	files []ZoomRecordingFile,
	description string,
	raw map[string]string,
	issues []string,
) QuarantineRecord {
	fileIDs := make([]string, 0, len(files))
	for _, file := range files {
		if file.ID != "" {
			fileIDs = append(fileIDs, file.ID)
		}
	}
	sort.Strings(fileIDs)
	now := time.Now
	if d.Now != nil {
		now = d.Now
	}
	return QuarantineRecord{
		Version:             1,
		State:               "failed",
		Reason:              "invalid-routing-metadata",
		Issues:              append([]string(nil), issues...),
		MeetingID:           string(meeting.ID),
		MeetingUUID:         meeting.UUID,
		Topic:               meeting.Topic,
		StartTime:           meeting.StartTime,
		Description:         description,
		ParsedMetadata:      raw,
		CompletedMP4FileIDs: fileIDs,
		ObservedAt:          now().UTC().Format(time.RFC3339),
	}
}

type dateWindow struct {
	from time.Time
	to   time.Time
}

// Zoom's recordings collection accepts at most a 30-day date interval. Split
// a wider configured sweep into overlapping-free inclusive windows.
func zoomWindows(from, to time.Time) []dateWindow {
	var windows []dateWindow
	cursor := midnightUTC(from)
	end := midnightUTC(to)
	for !cursor.After(end) {
		windowEnd := cursor.AddDate(0, 0, 29)
		if windowEnd.After(end) {
			windowEnd = end
		}
		windows = append(windows, dateWindow{from: cursor, to: windowEnd})
		cursor = windowEnd.AddDate(0, 0, 1)
	}
	return windows
}

func midnightUTC(value time.Time) time.Time {
	year, month, day := value.UTC().Date()
	return time.Date(year, month, day, 0, 0, 0, 0, time.UTC)
}

func (d *Downloader) logf(format string, values ...any) {
	if d.Logger != nil {
		d.Logger.Printf(format, values...)
	}
}
