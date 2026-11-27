from reglament_agent.chat import release_footer, start_page


def test_release_build_has_an_escaped_tag_and_a_link(monkeypatch):
    info = {"tag": 'v2/<draft>"', "sha7": "a123def", "dirty": False}
    monkeypatch.setattr(release_footer.health, "release_info", lambda: info, raising=False)
    footer = release_footer.render_footer()
    assert "Версия v2/&lt;draft&gt;&quot; · a123def" in footer
    assert "<draft>" not in footer
    assert 'href="https://github.com/argmaster-dev/reglament-agent/releases/tag/v2%2F%3Cdraft%3E%22"' in footer
    assert "не релизная сборка" not in footer


def test_dev_build_warns_and_has_no_release_link(monkeypatch):
    info = {"tag": None, "sha7": "a123def", "dirty": False}
    monkeypatch.setattr(release_footer.health, "release_info", lambda: info, raising=False)
    footer = release_footer.render_footer()
    assert "dev · a123def · не релиз" in footer
    assert 'class="release-footer__warning">не релизная сборка' in footer
    assert "<a " not in footer


def test_start_page_uses_the_shared_footer_for_a_dirty_tagged_build(monkeypatch):
    info = {"tag": "v2.0.0", "sha7": "a123def", "dirty": True}
    monkeypatch.setattr(release_footer.health, "release_info", lambda: info, raising=False)
    page = start_page.render_page()
    assert release_footer.render_footer() in page and "не релизная сборка" in page


def test_admin_template_uses_the_shared_footer_and_keeps_status(monkeypatch):
    from fastapi import Request

    from reglament_agent.admin import routes

    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "test-signing-secret")
    info = {"tag": "v2.0.0", "sha7": "a123def", "dirty": False}
    monkeypatch.setattr(release_footer.health, "release_info", lambda: info, raising=False)
    page = routes.render("forbidden.html", Request({"type": "http", "headers": []}), status_code=403)
    assert page.status_code == 403
    assert release_footer.render_footer() in page.body.decode()
