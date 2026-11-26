# Requires DATABASE_URL (see .env.example). EMBEDDER=ollama|fake (default ollama).
.PHONY: demo-reset demo-reset-without-td test load run

# WIPES the whole database schema and reloads the demo regulations.
demo-reset:
	uv run python -m reglament_agent.ops demo-reset --yes

# Same, but without the Technical Directorate regulation (to demo adding it live).
demo-reset-without-td:
	uv run python -m reglament_agent.ops demo-reset --without td-project --yes

test:
	uv run pytest

load:
	uv run python -m reglament_agent.ingest load docx/

# Chainlit app on the host. src/reglament_agent/chat/app.py comes with the chat branch
# and may be absent on older checkouts. Reads .env if present.
run:
	set -a; [ -f .env ] && . ./.env; set +a; \
	uv run chainlit run src/reglament_agent/chat/app.py --headless --host 127.0.0.1 --port $${APP_PORT:-8000}
