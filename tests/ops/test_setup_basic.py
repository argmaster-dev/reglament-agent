from pathlib import Path
import runpy
import stat

import pytest

SETUP = Path(__file__).resolve().parents[2] / "deploy/setup-basic.py"
TEST_HASH = "$2a$10$" + "a" * 53


def test_provision_keeps_existing_config_and_quotes_bcrypt(tmp_path):
    path = tmp_path / ".env"
    path.write_text("APP_PORT=8000\nSTAND_BASIC_USER=judge\nSTAND_BASIC_PASSWORD=\n")
    path.chmod(0o600)
    setup = runpy.run_path(SETUP)
    provision = setup["provision"]
    provided = []

    def fake_hash(password):
        provided.append(password)
        return TEST_HASH

    provision.__globals__["bcrypt"] = fake_hash
    assert provision(path)
    output = path.read_text()
    assert "APP_PORT=8000\n" in output
    assert "STAND_BASIC_USER=judge\n" in output
    assert f"STAND_BASIC_PASSWORD_HASH='{TEST_HASH}'\n" in output
    assert "STAND_BASIC_PASSWORD=" + provided[0] + "\n" in output
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not provision(path)
    assert len(provided) == 1


def test_provision_refuses_partial_hash_or_shared_env(tmp_path):
    path = tmp_path / ".env"
    initial = f"STAND_BASIC_PASSWORD_HASH='{TEST_HASH}'\n"
    path.write_text(initial)
    provision = runpy.run_path(SETUP)["provision"]
    path.chmod(0o644)
    with pytest.raises(RuntimeError, match="permissions 600"):
        provision(path)
    path.chmod(0o600)
    with pytest.raises(RuntimeError, match="valid bcrypt hash and its password"):
        provision(path)
    assert path.read_text() == initial
