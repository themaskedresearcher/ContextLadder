#!/usr/bin/env python3
"""Clone/checkout the project sources referenced by the warning dataset.

Reads a thin warnings JSONL (default: data/real_world_warnings.jsonl) and, for
each distinct (project, commit), clones the git repo and checks out the commit
(or downloads+extracts a release tarball for projects with no commit). Writes a
`checkout_manifest.json` mapping every warning to its on-disk repo root.

All git calls use the safe-directory fix (`git -c safe.directory=...`) to avoid
"dubious ownership" errors; the user's global git config is left untouched.

Example
-------
    python scripts/clone_projects.py --clone-root ../_clones
    python scripts/clone_projects.py --clone-root ../_clones --projects leptonica nbd
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[1]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from src import config  # noqa: E402
from src.repo_clone import clone_dataset  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--warnings", default=str(config.REAL_WORLD_WARNINGS),
                   help="Warnings JSONL (default: data/real_world_warnings.jsonl)")
    p.add_argument("--clone-root", default=str(config.CLONES_DIR),
                   help="Directory where project checkouts are created "
                        "(default: output/clones/)")
    p.add_argument("--force-clone", action="store_true",
                   help="Remove existing checkout directories before fetching")
    p.add_argument("--blobless", action="store_true",
                   help="git clone --filter=blob:none (faster; may miss very old commits)")
    p.add_argument("--projects", nargs="*",
                   help="Optional subset of project keys to process (default: all)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    warnings_path = Path(args.warnings)
    if not warnings_path.is_file():
        raise FileNotFoundError(f"Warnings JSONL not found: {warnings_path}")

    config.ensure_output_dirs()
    manifest = clone_dataset(
        warnings_path=warnings_path,
        clone_root=Path(args.clone_root),
        force_clone=args.force_clone,
        blobless=args.blobless,
        only_projects=args.projects,
        manifest_path=config.CHECKOUT_MANIFEST,
    )
    if manifest["summary"]["checkouts_failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
