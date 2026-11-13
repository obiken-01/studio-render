# studio-render

Private FastAPI service that renders videos for Studio. It accepts uploads, detects beats
and renders with FFmpeg. Every endpoint except `/health` requires a signed-in Work user on the
allowlist.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `JWT_SECRET_KEY` | required | HS256 signing key shared with the main API. At least 32 characters, otherwise the service refuses to start. |
| `JWT_ISSUER` | required | Expected `iss` claim. Must not be empty. |
| `JWT_AUDIENCE` | required | Expected `aud` claim. Must not be empty. |
| `STUDIO_ALLOWED_USER_IDS` | empty | Comma-separated Work user ids. Empty means nobody. |
| `CORS_ALLOWED_ORIGINS` | empty | Comma-separated browser origins allowed by CORS. |
| `STUDIO_DATA_DIR` | `/tmp/studio` | Where uploads and renders are stored. |
| `STUDIO_TTL_HOURS` | `24` | Hours before project data expires. |
| `STUDIO_MAX_FILE_GB` | `3` | Largest single upload. |
| `STUDIO_MAX_PROJECT_GB` | `10` | Largest total size per project. |
| `STUDIO_RENDER_TIMEOUT_MIN` | `30` | Render time limit in minutes. |
| `STUDIO_QUEUE_MAX` | `3` | Maximum queued renders. |
| `PORT` | `8000` | Port used by the prod image. |

Copy `.env.example` to `.env` (gitignored) and fill in the secrets. Never commit `.env`.

## Development

Tests and linting run in the dev image, which includes FFmpeg, libsndfile and all dependencies.

```bash
docker build --target dev -t studio-render-dev .
MSYS_NO_PATHCONV=1 docker run --rm -v "$PWD:/app" studio-render-dev pytest -q
MSYS_NO_PATHCONV=1 docker run --rm -v "$PWD:/app" studio-render-dev ruff check .
```

Rebuild the image only when dependencies change.

## Deployment

See [docs/DEPLOY.md](docs/DEPLOY.md). The prod image runs as an unprivileged user with a single
uvicorn worker. Run exactly one replica: the render queue and file registry live in process
memory. Uploads and renders live in `STUDIO_DATA_DIR` (temp disk) and are lost on redeploy.

To run the prod image locally:

```bash
docker build --target prod -t studio-render .
docker run --rm -p 8000:8000 -e JWT_SECRET_KEY=<32+ characters> -e JWT_ISSUER=local \
  -e JWT_AUDIENCE=local -e STUDIO_ALLOWED_USER_IDS=1 studio-render
curl http://localhost:8000/health
```

## API

Success: `{"data": ..., "message": null}`. Error:
`{"data": null, "message": "plain sentence", "code": "machine_code", "details": null}`, where
`details` is an object for some errors and `null` otherwise.

Every route except `GET /health` and the signed download URL needs
`Authorization: Bearer <Work JWT>` (HS256, checked for signature, `iss`, `aud`, `exp` and
`user_type == "Work"`). A missing or bad token gives 401 `unauthorized`. A valid Work token whose
`sub` is not in `STUDIO_ALLOWED_USER_IDS` gives 403 `forbidden`. There is one implicit project:
every file belongs to it.

| Method and path | What it does |
| --- | --- |
| `GET /health` | Public liveness check. Returns `{"status": "ok"}`. |
| `GET /session` | Returns `{"allowed": true}` for an allowed Work user, otherwise 401 or 403. |
| `POST /uploads` | Body `{filename, size, kind}`, where `kind` is `video`, `photo` or `audio`. Allowed extensions: mp4, mov (video); jpg, jpeg, png, heic, heif (photo); mp3, m4a, wav (audio). Checks type, size, project total and free disk. Returns `{uploadId, chunkSize, totalChunks}`; the chunk size is 8 MB. |
| `PUT /uploads/{uploadId}/chunks/{n}` | Raw body, one chunk (`n` starts at 0). Resending a chunk overwrites it. Returns `{received: [...]}`. |
| `GET /uploads/{uploadId}` | Returns `{received, totalChunks}`, so an interrupted upload can resume. |
| `POST /uploads/{uploadId}/complete` | Checks that every chunk arrived, probes the file with ffprobe and returns its metadata: `fileId`, `kind`, `filename`, `size`, `durationSec`, `width`, `height`, `fps`, `rotation`. HEIC and HEIF photos are converted to JPEG. An unreadable file is rejected. |
| `POST /projects/analyze` | Body `{musicFileId}`. Returns `{beats, tempo, durationSec}`. Cached per file. |
| `POST /projects/draft` | Body `{fileIds, musicFileId, aspect, pace, length, startSec?, audioMode, seed, template?}`. Returns `{editList, warnings}`. `aspect` is `9:16`, `16:9` or `4:5`; `pace` is `calm`, `medium` or `fast`; `length` is `15`, `30`, `60` or `"all"`; `audioMode` is `music` or `silent`. Unknown fields are rejected. |
| `POST /renders` | Body is an edit list. It is validated, then queued. Returns 202 `{jobId}`. |
| `GET /renders/{jobId}` | Returns `{jobId, status, percent, position, message}`. `status` is `queued` (with `position`), `running` (with `percent`), `done` or `failed` (with `message`). |
| `POST /renders/{jobId}/download-link` | Only for a finished render. Returns `{url, expiresInSec}`: a path with a random token, valid for 300 seconds. |
| `GET /renders/{jobId}/file?token=...` | Streams the mp4 as an attachment. No Bearer header; the token authorises it. The mp4 is deleted 15 minutes after a download completes. |
| `DELETE /files/{fileId}` | Deletes one uploaded file. |
| `DELETE /renders/{jobId}` | Removes one render and its output (cancels it if still queued or running). |
| `DELETE /projects/current` | Clears the whole project: every upload and file, and every render. |

The delete routes return `{"data": null, "message": null}`.

### Draft warnings

`warnings` is a list of codes in a successful response. The edit list is still returned.

| Code | Meaning |
| --- | --- |
| `song_shorter_than_length` | The song is shorter than the requested length, so the video is shorter. |
| `same_clip_back_to_back` | There was not enough footage to avoid the same clip twice in a row. |
| `shots_repeat` | There was not enough footage, so some shots are reused. |
| `photos_left_out` | Some chosen photos did not fit into the slots and were left out. |

### Error codes

| Status | Code | When |
| --- | --- | --- |
| 400 | `bad_chunk` | Chunk number out of range, or the chunk has the wrong size. |
| 401 | `unauthorized` | Missing, invalid or expired token. |
| 403 | `forbidden` | Not a Work account, or not on the allowlist. |
| 403 | `bad_download_link` | Download token wrong, expired or for another render. |
| 404 | `unknown_upload`, `unknown_file`, `unknown_render` | The id does not exist or has expired. |
| 409 | `upload_incomplete` | `complete` was called before all chunks arrived; `details.missing` lists them. |
| 409 | `not_ready` | A download link was requested before the render finished. |
| 413 | `file_too_large`, `project_too_large`, `chunk_too_large` | A size limit was exceeded. |
| 415 | `unsupported_type` | The extension is not allowed for that `kind`. |
| 422 | `invalid_request` | The body failed validation (`details.errors` lists the fields), or the file size is not valid. |
| 422 | `unknown_file`, `wrong_file_kind` | A file id in an analyze, draft or render request is missing or is the wrong kind. |
| 422 | `unreadable_file` | ffprobe or the image decoder cannot read the upload. |
| 422 | `no_beats`, `not_enough_footage`, `template_too_long` | A draft cannot be built from the given music, footage or template. |
| 422 | `bad_project`, `bad_transition`, `bad_trim`, `duration_mismatch`, `timeline_gap`, `unfilled_slots` | The edit list sent to `POST /renders` is not valid. |
| 429 | `queue_full` | `STUDIO_QUEUE_MAX` renders are already waiting. |
| 500 | `server_error` | Unhandled error. No details are returned. |
| 507 | `disk_full` | Not enough free disk. |
