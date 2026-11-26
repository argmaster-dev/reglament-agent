"""Chat service wiring for a configured database or a standalone interface preview."""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass

from reglament_agent.answer import Answerer, Profiles, Sources


@dataclass
class Services:
    answerer: Answerer
    sources: Sources
    profiles: Profiles


def build_services(env: dict[str, str] | None = None) -> Services:
    """A configured factory takes precedence; a database enables the regulation service."""
    env = env if env is not None else dict(os.environ)
    spec = env.get("CHAT_SERVICES_FACTORY")
    if spec:
        mod, _, fn = spec.partition(":")
        return getattr(importlib.import_module(mod), fn)()
    if env.get("DATABASE_URL"):
        from reglament_agent.dialog.services import for_database
        return for_database(env["DATABASE_URL"])
    from reglament_agent.chat.stubs import StubAnswerer, StubProfiles, StubSources

    profiles = StubProfiles()
    return Services(StubAnswerer(profiles), StubSources(), profiles)
