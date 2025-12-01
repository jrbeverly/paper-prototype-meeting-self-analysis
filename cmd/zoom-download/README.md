# Zoom recording downloader

`zoom-download` is a bounded reconciler: each invocation scans the configured
date window, uploads every completed MP4 recording segment, attaches
`mi.source`, prints a JSON summary, and exits.

Required configuration:

- `MI_BUCKET` (or `--bucket`)
- `GENERAL_OAUTH_TOKEN_URL` (or `--token-url`), unless supplied in
  `MI_ZOOM_OAUTH_SECRET_JSON`
- `ZOOM_USERS` (or `--users`; defaults to `me`)

Useful configuration includes `ZOOM_FROM`, `ZOOM_TO`,
`ZOOM_LOOKBACK_DAYS`, `MI_RECORDINGS_PREFIX`, `AWS_REGION`,
`GENERAL_OAUTH_TOKEN_METHOD`, `GENERAL_OAUTH_HEADERS_JSON`, and
`GENERAL_OAUTH_AUTHORIZATION`. Run with `--help` for the complete set.
CodeBuild can instead inject `MI_ZOOM_OAUTH_SECRET_JSON` with
`{"tokenUrl":"...","method":"POST","authorization":"...","headers":{}}`;
the individual `GENERAL_OAUTH_*` variables take precedence over that JSON.

The token broker is deliberately the sole owner of General OAuth refresh
tokens. The command makes `POST {}` by default and expects JSON containing
`access_token` (the aliases `accessToken` and `token` are accepted). It asks
the broker again and retries one Zoom request after a 401 or 403. A broker that
uses `GET` can be selected with `GENERAL_OAUTH_TOKEN_METHOD=GET`. The broker
URL must be absolute and use HTTPS.

Zoom assumptions:

- recordings are listed through
  `GET /v2/users/{user}/recordings` with token pagination;
- meeting configuration is read from the listed occurrence's `agenda`, or
  from `GET /v2/meetings/{meetingId}` when it is absent;
- configured users other than `me` require the General app and grant to have
  suitable account/admin recording scopes; and
- recording downloads accept the same OAuth bearer token and may redirect.

S3 Object Annotations require a current AWS CLI v2 in `PATH`. The command uses
`s3api get-object-annotation` and `put-object-annotation`, and every annotation
write supplies `--object-if-match` using the MP4's current ETag. MP4 bodies are
streamed directly into the AWS SDK for Go v2 multipart uploader using ambient
CodeBuild role credentials.

Invalid or incomplete key-value routing metadata is written explicitly under
`s3://$MI_BUCKET/$MI_QUARANTINE_PREFIX/zoom/`; no recording is uploaded for
that occurrence.
