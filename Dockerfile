FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY pyproject.toml uv.lock LICENSE README.md ./
COPY .chainlit ./.chainlit
COPY chainlit.md chainlit_ru.md ./
COPY src ./src
COPY migrations ./migrations
COPY docx ./docx
RUN uv sync --locked --no-dev && chown -R app:app /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health', timeout=4)"
CMD ["chainlit", "run", "src/reglament_agent/chat/app.py", "--headless", "--host", "0.0.0.0", "--port", "8000"]
