"""Public copy of the build identifier shared by the three user interfaces."""

from html import escape
from urllib.parse import quote

from reglament_agent.ops import health

RELEASES = "https://github.com/argmaster-dev/reglament-agent/releases/tag/"


def render_footer() -> str:
    info = health.release_info()
    tag = info["tag"]
    sha7 = info["sha7"] or "SHA неизвестен"
    if tag:
        link = escape(RELEASES + quote(tag, safe=""), quote=True)
        label = escape(f"Версия {tag} · {sha7}")
        content = f'<a href="{link}" target="_blank" rel="noopener noreferrer">{label}</a>'
    else:
        content = escape(f"Версия dev · {sha7} · не релиз")

    if info["dirty"] or not tag:
        content += ' <strong class="release-footer__warning">не релизная сборка</strong>'
    return f'<footer class="release-footer" data-release-footer>{content}</footer>'
