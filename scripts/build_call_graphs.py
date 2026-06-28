#!/usr/bin/env python3
"""Build tree-sitter call graphs for the dataset projects.

Two datasets are supported via ``--dataset``:

* ``realworld`` (default) — reads the checkout manifest produced by
  ``clone_projects.py`` and builds one call graph per checked-out
  ``(project, commit)``, pickled to
  ``output/call_graphs/<project>__<commit>.pickle``. A ``build_manifest.json``
  is written under ``output/call_graphs/``.

* ``juliet`` — Juliet sources are vendored under ``data/juliet`` (no cloning).
  Each Juliet testcase folder is self-contained (it ships its own
  ``io.c`` / ``std_testcase.h`` support files), so one call graph is built per
  testcase folder, pickled to ``output/call_graphs/juliet/<testcase>.pickle``,
  with a ``build_manifest_juliet.json``.

The next step (`extract_context.py`) reads these manifests to find each graph.

Example
-------
    python scripts/build_call_graphs.py
    python scripts/build_call_graphs.py --projects leptonica --force
    python scripts/build_call_graphs.py --dataset juliet
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PKG_ROOT = Path(__file__).resolve().parents[1]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from src import config  # noqa: E402
from src.extract_call_graph import TS  # noqa: E402


def count_functions(ts: Any) -> int:
    """Number of declared entries across all parsed files (sanity metric)."""
    try:
        return sum(len(d) for d in ts.decl_info.values())
    except Exception:  # noqa: BLE001
        return -1


def build_one(repo_root: Path, pickle_path: Path, force: bool) -> Tuple[str, Optional[int]]:
    if pickle_path.exists() and not force:
        return "skipped", None
    ts = TS(str(repo_root))
    pickle_path.parent.mkdir(parents=True, exist_ok=True)
    with pickle_path.open("wb") as f:
        pickle.dump(ts, f)
    return "built", count_functions(ts)


def _build_loop(units: List[Dict[str, Any]], force: bool) -> Tuple[List[Dict[str, Any]], int, int, int]:
    """Build each unit {key, repo_root, pickle, ...extra}. Returns (entries, built, skipped, failed)."""
    entries: List[Dict[str, Any]] = []
    built = skipped = failed = 0
    for i, u in enumerate(units, 1):
        dest, pickle_path = Path(u["repo_root"]), Path(u["pickle"])
        entry = {**u, "repo_root": str(dest), "pickle": str(pickle_path),
                 "status": "failed", "n_functions": None}
        print(f"[{i}/{len(units)}] {u['key']} ... ", end="", flush=True)
        t0 = time.time()
        try:
            if not dest.is_dir():
                raise FileNotFoundError(f"source dir missing: {dest}")
            status, n_funcs = build_one(dest, pickle_path, force)
            entry["status"], entry["n_functions"] = status, n_funcs
            if status == "built":
                built += 1
                print(f"built ({n_funcs} functions, {time.time() - t0:.1f}s)")
            else:
                skipped += 1
                print("skipped (pickle exists)")
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed += 1
            entry["error"] = str(exc)
            print(f"FAILED: {exc}")
        entries.append(entry)
    return entries, built, skipped, failed


def realworld_units(manifest_path: Path, only: set) -> List[Dict[str, Any]]:
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"Checkout manifest not found: {manifest_path}\nRun clone_projects.py first.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    units = []
    for c in manifest.get("checkouts", []):
        if not c.get("ok") or (only and c.get("project") not in only):
            continue
        units.append({
            "key": config.checkout_key(c["project"], c.get("commit")),
            "project": c["project"], "commit": c.get("commit"),
            "repo_root": c["dest"],
            "pickle": str(config.call_graph_path(c["project"], c.get("commit"))),
        })
    return units


def juliet_units(warnings_path: Path) -> List[Dict[str, Any]]:
    if not warnings_path.is_file():
        raise FileNotFoundError(f"Juliet warnings not found: {warnings_path}")
    rows = [json.loads(l) for l in warnings_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    # One unit per unique testcase folder (parent dir of each warning file).
    folders = sorted({Path(r["file_path"]).parent.as_posix() for r in rows})
    units = []
    for rel_folder in folders:
        leaf = Path(rel_folder).name
        units.append({
            "key": leaf, "testcase_folder": rel_folder,
            "repo_root": str(config.JULIET_SRC_DIR / rel_folder),
            "pickle": str(config.juliet_call_graph_path(leaf)),
        })
    return units


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset", choices=["realworld", "juliet"], default="realworld",
                   help="Which dataset to build call graphs for (default: realworld)")
    p.add_argument("--manifest", default=str(config.CHECKOUT_MANIFEST),
                   help="[realworld] checkout manifest (default: output/checkout_manifest.json)")
    p.add_argument("--warnings", default=str(config.JULIET_WARNINGS),
                   help="[juliet] warnings JSONL (default: data/juliet_warnings.jsonl)")
    p.add_argument("--projects", nargs="*",
                   help="[realworld] subset of project keys to process (default: all)")
    p.add_argument("--force", action="store_true",
                   help="Rebuild even if a pickle already exists")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    config.ensure_output_dirs()

    if args.dataset == "juliet":
        units = juliet_units(Path(args.warnings))
        out_path, src_ref = config.JULIET_BUILD_MANIFEST, str(config.JULIET_WARNINGS)
    else:
        units = realworld_units(Path(args.manifest), set(args.projects or []))
        out_path, src_ref = config.BUILD_MANIFEST, str(Path(args.manifest).resolve())

    if not units:
        print("No units to process (check manifest / dataset / --projects).")
        raise SystemExit(1)

    entries, built, skipped, failed = _build_loop(units, args.force)

    build_manifest = {
        "dataset": args.dataset,
        "source": src_ref,
        "call_graphs_dir": str(config.CALL_GRAPHS_DIR),
        "call_graphs": entries,
        "summary": {"total": len(entries), "built": built,
                    "skipped": skipped, "failed": failed},
    }
    out_path.write_text(json.dumps(build_manifest, indent=2), encoding="utf-8")
    print(f"\nWrote build manifest: {out_path}")
    print(f"Call graphs: {built} built, {skipped} skipped, {failed} failed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
