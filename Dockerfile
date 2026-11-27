FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

COPY --from=ghcr.io/astral-sh/uv:0.8@sha256:1d31be550ff927957472b2a491dc3de1ea9b5c2d319a9cea5b6a48021e2990a6 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY pyproject.toml uv.lock LICENSE README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY public ./public
COPY .chainlit ./.chainlit
COPY chainlit.md chainlit_ru.md ./
COPY src ./src
COPY migrations ./migrations
COPY docx ./docx
COPY scripts/source_digest.py ./scripts/
RUN uv sync --locked --no-dev && python scripts/source_digest.py --root /app --write-manifest /app/.source-files.json && chown -R app:app /app

ARG BUILD_SHA
ARG BUILD_TAG
ARG BUILD_TIME
ARG BUILD_DIRTY=true
ENV BUILD_SHA=${BUILD_SHA} BUILD_TAG=${BUILD_TAG} BUILD_TIME=${BUILD_TIME} BUILD_DIRTY=${BUILD_DIRTY}
LABEL org.opencontainers.image.revision=${BUILD_SHA} org.opencontainers.image.version=${BUILD_TAG} org.opencontainers.image.created=${BUILD_TIME}
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health', timeout=4)"
CMD ["chainlit", "run", "src/reglament_agent/chat/app.py", "--headless", "--host", "0.0.0.0", "--port", "8000"]
