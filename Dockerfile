FROM python:3.12-slim AS base
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml constraints.txt ./
# Install dependencies only (the source is copied or mounted separately), at the exact
# versions in constraints.txt.
RUN python -c "import tomllib; p = tomllib.load(open('pyproject.toml', 'rb'))['project']; print('\n'.join(p['dependencies']))" > /tmp/runtime.txt \
    && pip install --no-cache-dir -r /tmp/runtime.txt -c constraints.txt

FROM base AS dev
RUN python -c "import tomllib; p = tomllib.load(open('pyproject.toml', 'rb'))['project']; print('\n'.join(p['optional-dependencies']['dev']))" > /tmp/dev.txt \
    && pip install --no-cache-dir -r /tmp/dev.txt -c constraints.txt
COPY . .

FROM base AS prod
COPY app ./app
COPY scripts/prodcheck.py ./scripts/prodcheck.py
# Run as an unprivileged user. It needs a real HOME and a numba cache dir it can write to
# (librosa compiles functions with caching), and the default data dir must be writable too.
RUN useradd --system --uid 10001 --create-home --home-dir /home/studio studio \
    && mkdir -p /tmp/studio /home/studio/.cache/numba \
    && chown -R studio:studio /tmp/studio /home/studio
ENV HOME=/home/studio \
    NUMBA_CACHE_DIR=/home/studio/.cache/numba
USER studio
ENV PORT=8000
# exec so uvicorn replaces the shell and receives SIGTERM directly (graceful shutdown).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
