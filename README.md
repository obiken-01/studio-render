# studio-render

Private FastAPI service that renders videos for Studio. It accepts uploads, detects beats
and renders with FFmpeg. Every endpoint except `/health` requires a signed-in Work user on the
allowlist.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `JWT_SECRET_KEY` | required | HS256 signing key shared with the main API. At least 32 characters, otherwise the service refuses to start. |
| `JWT_ISSUER` | required | Expected `iss` claim. |
| `JWT_AUDIENCE` | required | Expected `aud` claim. |
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

## API

All responses use `{"data": ..., "message": null}` on success and
`{"data": null, "message": "...", "code": "...", "details": null}` on error.

- `GET /health` - public liveness check.
- `GET /session` - returns `{"allowed": true}` for an allowed Work user.
