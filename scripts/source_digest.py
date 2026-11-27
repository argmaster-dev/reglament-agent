#!/usr/bin/env python3
"""Print the same source digest that the stand reports in /health."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reglament_agent.ops.provenance import source_inventory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--write-manifest", type=Path)
    args = parser.parse_args()
    try:
        snapshot = source_inventory(args.root.resolve())
        if args.write_manifest:
            args.write_manifest.write_text(
                json.dumps(snapshot.manifest(), ensure_ascii=True, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"source digest error: {exc}\n")
    print(snapshot.digest)


if __name__ == "__main__":
    main()
