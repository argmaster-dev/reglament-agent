"""Build an approved public tag and optionally start it in the persistent stand."""
import argparse
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

PUBLIC_REPO = "https://github.com/argmaster-dev/reglament-agent.git"
PRODUCT_SERVICES = {"app", "sender", "migrate"}
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
RELEASE_STORE = Path.home() / ".local/share/reglament-stand/releases"


def output(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def runtime_env_file(path):
    path = path.resolve(strict=True)
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("The stand env file must be private (chmod 600).")
    if re.search(r"^\s*(?:export\s+)?(?:BUILD_(?:SHA|TAG|TIME|DIRTY)|REGULATIONS_IMAGE_DIGESTS)\s*=",
                 path.read_text(), re.MULTILINE):
        raise ValueError("Remove release metadata from the stand env file; it comes from the verified image.")
    return path


def build_release(args, env_file):
    release_dir = Path(tempfile.mkdtemp(prefix=f"{args.tag}-", dir=args.store))
    clone = release_dir / "source"
    subprocess.run(["git", "clone", "--quiet", "--depth=1", "--single-branch", "--branch", args.tag,
                    PUBLIC_REPO, str(clone)], check=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    head = output("git", "rev-parse", "HEAD", cwd=clone)
    tag_sha = output("git", "rev-parse", "--verify", f"refs/tags/{args.tag}^{{commit}}", cwd=clone)
    dirty = bool(output("git", "status", "--porcelain", "--ignored", "--untracked-files=all", cwd=clone))
    if head != args.sha or tag_sha != head or dirty:
        raise ValueError("The clean public tag checkout does not match the expected commit.")

    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    image_tag = release_dir.name
    context = dict(os.environ, BUILD_SHA=head, BUILD_TAG=args.tag, BUILD_TIME=now, BUILD_DIRTY="false",
                   REGULATIONS_IMAGE_TAG=image_tag, PYTHONDONTWRITEBYTECODE="1",
                   REGULATIONS_CADDYFILE_SHA256=hashlib.sha256((clone / "deploy/Caddyfile").read_bytes()).hexdigest())
    # Build with placeholders. Production credentials stay outside both the checkout and build context.
    example = clone / ".env.example"
    build_env = dict(context, POSTGRES_PASSWORD="build", CHAINLIT_AUTH_SECRET="build",
                     STAND_BASIC_USER="build", STAND_BASIC_PASSWORD_HASH="build", REGULATIONS_ENV_FILE=str(example))
    compose = ["docker", "compose", "-p", "reglament-stand", "-f", "compose.yaml", "-f", "compose.stand.yaml"]
    build = [*compose, "--env-file", str(example)]
    app_image = "reglament-agent:" + image_tag
    has_buildx = subprocess.run(["docker", "buildx", "version"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL).returncode == 0
    if has_buildx:
        subprocess.run([*build, "build", "app"], cwd=clone, env=build_env, check=True)
    else:
        # The classic Engine builder accepts the same Dockerfile and arguments.
        parameters = [part for key in ("BUILD_SHA", "BUILD_TAG", "BUILD_TIME", "BUILD_DIRTY")
                      for part in ("--build-arg", key)]
        subprocess.run(["docker", "build", "--memory=1536m", "--memory-swap=1536m", *parameters,
                        "--tag", app_image, "."], cwd=clone, env={**build_env, "DOCKER_BUILDKIT": "0"}, check=True)
    image_id = output("docker", "image", "inspect", "--format", "{{.Id}}", app_image)
    if not DIGEST.fullmatch(image_id):
        raise ValueError("Missing config digest of the built app image.")
    labels = json.loads(output("docker", "image", "inspect", "--format", "{{json .Config.Labels}}", image_id))
    if (labels.get("org.opencontainers.image.revision") != head
            or labels.get("org.opencontainers.image.version") != args.tag
            or labels.get("org.opencontainers.image.created") != now):
        raise ValueError("OCI build labels differ from the approved release metadata.")
    digest_cli = ["scripts/source_digest.py", "--root"]
    expected = output(sys.executable, *digest_cli, ".", cwd=clone, env=build_env)
    actual = output("docker", "run", "--rm", "--network=none", "--memory=96m", "--memory-swap=96m",
                    "--entrypoint", "python", image_id, *digest_cli, "/app")
    if actual != expected or not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", expected):
        raise ValueError("Source digest of the built image differs from the verified checkout.")
    receipt = {"sha": head, "tag": args.tag, "time": now, "dirty": dirty, "source_digest": actual,
               "image": app_image, "image_id": image_id}
    receipt_path = release_dir / "build.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"Build checked: {receipt_path}", flush=True)
    if not args.start:
        return
    env = dict(context, REGULATIONS_ENV_FILE=str(env_file))
    command = [*compose, "--env-file", str(env_file)]
    config = json.loads(output(*command, "config", "--format", "json", cwd=clone, env=env))
    external = sorted(config["services"].keys() - PRODUCT_SERVICES)
    images = {name: service["image"] for name, service in config["services"].items()}
    if any(images[name] != app_image for name in PRODUCT_SERVICES):
        raise ValueError("All product services must run the checked app image.")
    for name in external:
        if not DIGEST.fullmatch(images[name].partition("@")[2]):
            raise ValueError(f"No pinned registry digest for {name}.")
    subprocess.run([*command, "pull", "--policy", "missing", *external], cwd=clone, env=env, check=True)
    snapshots = {name: {"image_id": image_id} for name in sorted(PRODUCT_SERVICES)}
    for name in external:
        pinned = images[name].partition("@")[2]
        inspected = json.loads(output("docker", "image", "inspect", images[name]))[0]
        if (not DIGEST.fullmatch(inspected["Id"])
                or not any(ref.endswith("@" + pinned) for ref in inspected.get("RepoDigests") or [])):
            raise ValueError(f"Local manifest or image ID does not match {name}.")
        snapshots[name] = {"manifest_digest": pinned, "image_id": inspected["Id"]}
    env["REGULATIONS_IMAGE_DIGESTS"] = json.dumps(snapshots, separators=(",", ":"))
    subprocess.run([*command, "up", "-d", "--no-build", "--pull", "never", "--wait"], cwd=clone, env=env, check=True)
    for name, snapshot in snapshots.items():
        container = output(*command, "ps", "--all", "--quiet", name, cwd=clone, env=env)
        if output("docker", "inspect", "--format", "{{.Image}}", container) != snapshot["image_id"]:
            raise ValueError(f"Container {name} does not use the recorded image.")
    (release_dir / "images.json").write_text(json.dumps(snapshots, indent=2) + "\n")
    print(f"Running {args.tag} at {head}. Image digests: {release_dir / 'images.json'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="public release tag")
    parser.add_argument("sha", help="approved full 40-character commit SHA")
    parser.add_argument("--store", type=Path, default=RELEASE_STORE, help="persistent directory for clean checkouts")
    parser.add_argument("--env-file", type=Path, help="existing private stand env file, required with --start")
    parser.add_argument("--start", action="store_true", help="start the checked image in Compose project reglament-stand")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,85}", args.tag) or not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        parser.error("Provide a release tag and its full lowercase SHA.")
    if args.start and not args.env_file:
        parser.error("--start needs --env-file")
    os.umask(0o077)
    args.store = args.store.expanduser().resolve()
    args.store.mkdir(mode=0o700, parents=True, exist_ok=True)
    env_file = runtime_env_file(args.env_file) if args.start else None
    # All releases of the same Compose project serialize, including when using different stores.
    lock_dir = Path.home() / ".local/share/reglament-stand"
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (lock_dir / "release.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        build_release(args, env_file)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        sys.exit(str(exc))
