import asyncio
from dataclasses import replace

import httpx
import pytest

from reglament_agent.model.settings import ModelSettings
from reglament_agent.ops.health import Settings, health


@pytest.mark.parametrize("canonical", [False, True], ids=["legacy-env", "canonical-env-wins"])
def test_health_uses_client_endpoint_and_model_digests(monkeypatch, canonical):
    for name in ("MODEL_CHAT_MODEL", "MODEL_EMBED_MODEL", "MODEL_CHAT", "MODEL_EMBED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("MAIL_HOST", "127.0.0.1")
    monkeypatch.setenv("MAIL_PORT", "1")
    monkeypatch.setenv("MODEL_OLLAMA_URL", "http://different-ollama.test:8321")
    monkeypatch.setenv("MODEL_CHAT", "legacy-chat:v2")
    monkeypatch.setenv("MODEL_EMBED", "legacy-embed:v2")
    chat, embed = "legacy-chat:v2", "legacy-embed:v2"
    if canonical:
        chat, embed = "actual-chat:v3", "actual-embed:v3"
        monkeypatch.setenv("MODEL_CHAT_MODEL", chat)
        monkeypatch.setenv("MODEL_EMBED_MODEL", embed)

    model = ModelSettings()
    settings = Settings.from_env()
    assert (settings.ollama_url, settings.chat_model, settings.embed_model) == (
        model.ollama_url, chat, embed,
    )
    assert (model.chat_model, model.embed_model) == (chat, embed)

    calls = []

    def handler(request):
        calls.append(request.url)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [
                {"name": chat, "digest": "a" * 64},
                {"name": embed, "digest": "b" * 64},
                {"name": "unrelated:v1", "digest": "c" * 64},
            ]})
        return httpx.Response(200, json={"models": []})

    result = asyncio.run(health(replace(settings, transport=httpx.MockTransport(handler))))
    assert result["checks"]["ollama"]["ok"]
    assert result["model_digests"] == {chat: "a" * 64, embed: "b" * 64}
    assert [str(url) for url in calls] == [
        "http://different-ollama.test:8321/api/tags",
        "http://different-ollama.test:8321/api/ps",
    ]


def test_explicit_model_settings_preserve_pydantic_constructor_precedence(monkeypatch):
    monkeypatch.setenv("MODEL_CHAT_MODEL", "from-environment:latest")
    monkeypatch.setenv("MODEL_EMBED", "legacy-environment:latest")
    model = ModelSettings(chat_model="chat:explicit", embed_model="embed:explicit")
    assert (model.chat_model, model.embed_model) == ("chat:explicit", "embed:explicit")
