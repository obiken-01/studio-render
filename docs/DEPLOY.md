# Deploying studio-render to Railway

This is for Ralph. The service is one Docker container. The GitHub repo
`obiken-01/studio-render` already exists (remote `origin`). Nothing below has been done for you:
no push, no Railway service, no DNS change.

## Read this first

- The service must run with **exactly one uvicorn worker**, and Railway must run **one replica**.
  The render queue, the upload registry and the download tokens live in the memory of a single
  process. A second worker or replica would not see them, and uploads and renders would fail at
  random. The Dockerfile already starts uvicorn with `--workers 1`. Keep replicas at 1.
- Uploads and renders live on the container's temp disk (`/tmp/studio`). They are lost on every
  redeploy and restart, by design. Anything not downloaded before a deploy has to be uploaded and
  rendered again. Files also expire on their own after `STUDIO_TTL_HOURS` (24 by default).
- The container runs as an unprivileged user, not root.

## Steps

1. **Merge the code.** Merge `feature/phase-1` into `main` and push `main` to
   `obiken-01/studio-render`.

2. **Add the service.** In the existing Railway project choose New, GitHub Repo, and pick
   `obiken-01/studio-render`. In the service settings:
   - Build: use the `Dockerfile` at the repo root with target `prod`. If the dashboard has no
     target field, set the variable `RAILWAY_DOCKERFILE_TARGET=prod` (check the current Railway
     docs, this setting has moved before). `prod` is also the last stage in the file, so a default
     build gives the same image.
   - Healthcheck path: `/health`.
   - Replicas: 1.
   - Leave the start command empty. The image already has one.

3. **Set the variables** on the service. The names are exact.

   | Variable | Value, and where it comes from |
   | --- | --- |
   | `JWT_SECRET_KEY` | Copy from the API service's `Jwt__SecretKey`. Must be identical to it and at least 32 characters, or the service will not start. |
   | `JWT_ISSUER` | Copy from the API service's `Jwt__Issuer`. Must not be empty. |
   | `JWT_AUDIENCE` | Copy from the API service's `Jwt__Audience`. Must not be empty. |
   | `STUDIO_ALLOWED_USER_IDS` | Your Work user id. Sign in to the tools app, then open `https://api.ralphalcaide.com/api/work/auth/me` in the same browser and read the id from the response. Several ids can be comma-separated. If it is empty, nobody can get in. |
   | `CORS_ALLOWED_ORIGINS` | `https://tools.ralphalcaide.com` (no trailing slash). Add more origins comma-separated if you want a local dev origin. |

   Do not set `PORT`. Railway provides it and the image reads it. Optional limits and their
   defaults: `STUDIO_DATA_DIR` (`/tmp/studio`, leave it), `STUDIO_TTL_HOURS` (24),
   `STUDIO_MAX_FILE_GB` (3), `STUDIO_MAX_PROJECT_GB` (10), `STUDIO_RENDER_TIMEOUT_MIN` (30),
   `STUDIO_QUEUE_MAX` (3). The secret goes into Railway only, never into the repo.

4. **Check what the plan gives you.** After the first deploy, open the service settings and note
   the RAM and disk. The service needs free disk for the uploaded sources plus the render output,
   and it refuses new uploads and renders when less than 2 GB is free. Set
   `STUDIO_MAX_PROJECT_GB` below the real free disk and `STUDIO_MAX_FILE_GB` below that. If
   renders get killed for memory, lower the limits or move to a larger plan.

5. **Generate the public URL.** In the service's Networking tab, generate a domain, for example
   `https://studio-render-production.up.railway.app`. The tools app needs it as `VITE_STUDIO_URL`.
   Set that in the tools app's build environment and redeploy the tools app. The value is baked in
   at build time.

6. **Update the main deployment doc yourself.** This repo does not edit
   `ralph-portfolio/docs/DEPLOYMENT.md`. Add the Studio row there when you are ready.

## After deploy

- [ ] Open `<public URL>/health`. Expect `{"data":{"status":"ok"},"message":null}`.
- [ ] Open `<public URL>/session` without signing in. Expect a 401 with code `unauthorized`,
      which shows auth is on.
- [ ] Sign in at https://tools.ralphalcaide.com and check the Studio tab shows. It is hidden when
      `/session` does not return 200, so a missing tab usually means a wrong
      `STUDIO_ALLOWED_USER_IDS`, a mismatched JWT value, or a wrong `CORS_ALLOWED_ORIGINS`.
- [ ] Upload a small clip and a short song, make a draft, export, and download the result.
- [ ] Look at the service logs once for errors.

## Troubleshooting

- The deploy crashes at start: the log names the bad variable (`jwt_secret_key`, `jwt_issuer` or
  `jwt_audience`). Fix it and redeploy.
- 401 on every call: the key, issuer or audience differs from the API's.
- 403 `forbidden`: the token is not a Work account, or your id is not in
  `STUDIO_ALLOWED_USER_IDS`.
- Browser CORS errors: `CORS_ALLOWED_ORIGINS` must match the tools app origin exactly.
- Uploads or renders vanish after a deploy: expected, see "Read this first".
