import pytest


@pytest.fixture(autouse=True)
def configured_expert_login(monkeypatch):
    # Admin fixtures supply signed demo identities, so demo expert login must be enabled.
    monkeypatch.setenv("CHAT_EXPERT_PASSWORD", "configured-expert-test-password")
