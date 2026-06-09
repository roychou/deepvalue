# The Tedium Premium clock image: Python 3.12 + uv-managed deps + supercronic to fire the
# weekly forward session (and a daily heartbeat watchdog). No broker logic baked here — it
# connects to the `gateway` service over the compose network for prices/orders; fundamentals
# come from EDGAR (free). State lives on mounted volumes, not in the image.
FROM python:3.12-slim-bookworm

# uv from its official image — reproducible, no pip bootstrap. Bump the tag deliberately.
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# supercronic: a single-binary cron that runs non-root and logs to stdout — the right
# container scheduler. doppler CLI injects secrets at runtime. tzdata so TZ= works.
ARG SUPERCRONIC_VERSION=v0.2.33
ARG SUPERCRONIC_ARCH=linux-amd64
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates tzdata \
    && curl -fsSL "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-${SUPERCRONIC_ARCH}" \
        -o /usr/local/bin/supercronic \
    && chmod +x /usr/local/bin/supercronic \
    && curl -Ls https://cli.doppler.com/install.sh | sh \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Deps first (without the project) so this layer caches until the lockfile changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Application code + the config the session reads (policy.yaml, screen profiles). config/ is
# REQUIRED — run.py resolves policy.yaml relative to the repo root (/app). Then install the
# deepvalue package itself (src/ is now present).
COPY src/ ./src/
COPY config/ ./config/
RUN uv sync --frozen --no-dev
COPY deploy/app/crontab ./deploy/app/crontab
COPY deploy/app/entrypoint.sh /usr/local/bin/app-entrypoint.sh
RUN chmod +x /usr/local/bin/app-entrypoint.sh

# Wrap in `doppler run` so secrets (EDGAR UA, SMTP/Telegram, Anthropic key for the future
# Deterioration Lead) are fetched from Doppler at runtime and injected as env into the
# scheduler and every job it spawns — never written to disk. Needs DOPPLER_TOKEN (compose).
ENTRYPOINT ["doppler", "run", "--", "/usr/local/bin/app-entrypoint.sh"]
