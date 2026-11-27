"""Shared provenance for the checked-out sources and the deployed service."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from threading import Lock

MANIFEST_NAME = ".source-files.json"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
SOURCE_DIRS = ("src", "migrations")
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
_SOURCE_LOCK = Lock()


@dataclass(frozen=True)
class SourceInventory:
    files: tuple[str, ...]
    digest: str

    def manifest(self) -> dict:
        return {"schema": 1, "files": self.files, "source_digest": self.digest}


def _generated(name: str) -> bool:
    return "__pycache__" in name.split("/") or name.endswith(".pyc")


def _walk_error(exc: OSError) -> None:
    raise exc


def _discover_files(root: Path) -> dict[str, Path]:
    lock = root / "uv.lock"
    if lock.is_symlink() or not lock.is_file():
        raise FileNotFoundError("A regular uv.lock is required")
    files = {"uv.lock": lock}
    for name in SOURCE_DIRS:
        directory = root / name
        if directory.is_symlink() or not directory.is_dir():
            raise FileNotFoundError(f"A regular {name}/ directory is required")
        before = len(files)
        for parent, dirs, names in os.walk(directory, onerror=_walk_error):
            dirs[:] = [dirname for dirname in dirs if dirname != "__pycache__"]
            for dirname in dirs:
                if (Path(parent) / dirname).is_symlink():
                    raise ValueError("Source inventory must not contain symlinks")
            for filename in names:
                path = Path(parent) / filename
                relative = path.relative_to(root).as_posix()
                if _generated(relative):
                    continue
                if path.is_symlink() or not path.is_file():
                    raise ValueError("Source inventory must contain only regular files")
                files[relative] = path
        if len(files) == before:
            raise ValueError(f"Source inventory requires at least one file under {name}/")
    return files


def _git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(
        ["git", "--no-optional-locks", "-C", str(root), *args],
        stderr=subprocess.DEVNULL, timeout=3,
    )


def source_inventory(root: Path) -> SourceInventory:
    """Hash file names and raw bytes, validating the known source inventory."""
    files = _discover_files(root)
    if (root / ".git").exists():
        tracked = _git(root, "ls-files", "-z", "--", *SOURCE_DIRS, "uv.lock")
        tracked_paths = (os.fsdecode(item) for item in tracked.split(b"\0") if item)
        if any(name not in files for name in tracked_paths if not _generated(name)):
            raise ValueError("Source inventory is missing Git-tracked files")

    ordered = tuple(sorted(files))
    manifest_path = root / MANIFEST_NAME
    manifest = None
    if manifest_path.exists() or manifest_path.is_symlink():
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("Source inventory manifest must be a regular file")
        manifest = json.loads(manifest_path.read_bytes())
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema") != 1
            or manifest.get("files") != list(ordered)
        ):
            raise ValueError("Source inventory differs from the image manifest")

    hasher = hashlib.sha256()
    for name in ordered:
        for part in (os.fsencode(name), files[name].read_bytes()):
            # Prefix each item with its length so file boundaries are unambiguous.
            hasher.update(len(part).to_bytes(8, "big"))
            hasher.update(part)
    digest = hasher.hexdigest()
    if manifest is not None and manifest.get("source_digest") != digest:
        raise ValueError("Source digest differs from the image manifest")
    return SourceInventory(ordered, digest)


@dataclass(frozen=True)
class BuildIdentity:
    sha: str | None
    tag: str | None
    time: str | None
    dirty: bool | None


def _dirty_value(value: str) -> bool | None:
    match value.lower():
        case "1" | "true" | "yes":
            return True
        case "0" | "false" | "no":
            return False
        case _:
            return None


@cache
def build_identity() -> BuildIdentity:
    build_sha = os.getenv("BUILD_SHA", "").strip()
    if build_sha:
        return BuildIdentity(
            build_sha if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", build_sha) else None,
            os.getenv("BUILD_TAG", "").strip() or None,
            os.getenv("BUILD_TIME", "").strip() or None,
            _dirty_value(os.getenv("BUILD_DIRTY", "").strip()),
        )
    if (PROJECT_ROOT / ".git").exists():
        try:
            sha = _git(PROJECT_ROOT, "rev-parse", "HEAD").decode().strip()
            dirty = bool(_git(PROJECT_ROOT, "status", "--porcelain"))
            return BuildIdentity(sha, None, None, dirty)
        except (OSError, subprocess.SubprocessError):
            pass
    return BuildIdentity(None, None, None, None)


def release_info() -> dict[str, str | bool | None]:
    build = build_identity()
    return {"tag": build.tag, "sha7": build.sha[:7] if build.sha else None, "dirty": build.dirty is True}


@cache
def _runtime_source() -> tuple[str | None, str]:
    root = PROJECT_ROOT
    manifest = root / MANIFEST_NAME
    checkout = (root / ".git").exists()
    if not checkout and not manifest.exists() and not manifest.is_symlink():
        if os.getenv("BUILD_SHA", "").strip():
            return None, "invalid"
        if not (root / "src").exists() or not (root / "migrations").exists():
            return None, "unavailable"
    try:
        source = source_inventory(root)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, "invalid"
    return source.digest, "verified" if manifest.is_file() else "computed"


def build_details() -> tuple[dict, str]:
    identity = build_identity()
    with _SOURCE_LOCK:
        digest, status = _runtime_source()
    return {
        "sha": identity.sha, "tag": identity.tag, "time": identity.time,
        "dirty": identity.dirty, "source_digest": digest,
    }, status


def valid_sha256_digest(value: object) -> bool:
    return isinstance(value, str) and _HEX_SHA256.fullmatch(value.removeprefix("sha256:")) is not None


def deployment_image_digests() -> dict[str, dict[str, str]]:
    """Read the locally verified deployment record without Docker API access."""
    try:
        snapshot = json.loads(os.getenv("REGULATIONS_IMAGE_DIGESTS") or "{}")
    except (ValueError, TypeError):
        return {}
    if not isinstance(snapshot, dict):
        return {}
    result = {}
    for name in ("app", "migrate", "sender", "postgres", "mailpit", "gateway"):
        evidence = snapshot.get(name)
        if not isinstance(evidence, dict):
            continue
        digests = {key: value for key, value in evidence.items()
                   if key in {"image_id", "manifest_digest"} and valid_sha256_digest(value)}
        if digests:
            result[name] = digests
    return result
