#!/usr/bin/env python3
"""Provision the loopback gateway's credentials without displaying the password."""

import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile

CADDY_IMAGE = "caddy:2.8-alpine@sha256:af32e97399febea808609119bb21544d0265c58a02836576e32a2d082c262c17"
FIELDS = ("STAND_BASIC_USER", "STAND_BASIC_PASSWORD", "STAND_BASIC_PASSWORD_HASH")
BCRYPT = re.compile(r"\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}")


def bcrypt(password: str) -> str:
    command = [
        "docker", "run", "--rm", "--network", "none", "--pull", "never", "-i",
        "--memory", "64m", "--memory-swap", "64m", "--cpus", "0.25",
        CADDY_IMAGE, "caddy", "hash-password",
    ]
    result = subprocess.run(command, input=password + "\n", text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError("Caddy could not hash the password. Check the pinned local gateway image.")
    digest = result.stdout.strip()
    if not BCRYPT.fullmatch(digest):
        raise RuntimeError("Invalid bcrypt output from Caddy.")
    return digest


def basic_fields(lines: list[str]) -> dict[str, str]:
    result = {}
    for line in lines:
        key, _, value = line.partition("=")
        if key not in FIELDS:
            continue
        if key in result:
            raise RuntimeError(f"Duplicate {key} in .env.")
        result[key] = value.strip().strip("'\"")
    return result


def provision(path: Path) -> bool:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise RuntimeError("Create .env as a regular file with permissions 600 first.")
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    configured = basic_fields(lines)
    user = configured.get("STAND_BASIC_USER") or "stand"
    password = configured.get("STAND_BASIC_PASSWORD")
    digest = configured.get("STAND_BASIC_PASSWORD_HASH")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,48}", user):
        raise RuntimeError("STAND_BASIC_USER accepts letters, digits, dots, hyphens and underscores.")
    if password and not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", password):
        raise RuntimeError("Use a 20–64 character alphanumeric, hyphen or underscore password.")
    if digest:
        if not password or not BCRYPT.fullmatch(digest):
            raise RuntimeError("Set both a valid bcrypt hash and its password, or remove both values.")
        if configured.get("STAND_BASIC_USER"):
            return False
    else:
        password = password or secrets.token_hex(24)
        digest = bcrypt(password)
    kept = [line for line in lines if line.partition("=")[0] not in FIELDS]
    kept.extend(("", f"STAND_BASIC_USER={user}", f"STAND_BASIC_PASSWORD={password}",
                 f"STAND_BASIC_PASSWORD_HASH='{digest}'"))
    created = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as output:
            created = Path(output.name)
            output.write("\n".join(kept) + "\n")
        if path.read_text(encoding="utf-8") != original:
            raise RuntimeError(".env changed during credential setup. Run the command again.")
        os.replace(created, path)
    finally:
        if created is not None:
            created.unlink(missing_ok=True)
    return True


if __name__ == "__main__":
    try:
        created = provision(Path(".env"))
    except (OSError, RuntimeError) as error:
        raise SystemExit(str(error)) from None
    print("Gateway credentials provisioned in .env." if created else "Gateway credentials already configured.")
