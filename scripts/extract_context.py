#!/usr/bin/env python3
"""Extract leveled caller context (with function bodies) for warnings.

For each warning, this resolves its enclosing function in the project's pickled
call graph (from `build_call_graphs.py`) and emits, up to ``--depth`` levels:
``enclosing_function_bodies`` and ``callers_by_level`` (each level maps a
function identifier to its source body). This regenerates the context
deliberately omitted from the shipped "thin" dataset.

Datasets (``--dataset``):

* ``realworld`` — groups warnings by ``(project, commit)``, using the checkouts
  in ``output/clones`` and graphs in ``output/call_graphs``. Writes
  ``output/context/real_world_context.jsonl``.
* ``juliet`` — groups warnings by testcase folder, using the per-testcase graphs
  in ``output/call_graphs/juliet``. Writes
  ``output/context/juliet_context.jsonl``.

Example
-------
    python scripts/extract_context.py                       # real-world, depth 2
    python scripts/extract_context.py --dataset juliet
    python scripts/extract_context.py --depth 3 --projects leptonica
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

PKG_ROOT = Path(__file__).resolve().parents[1]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from src import config  # noqa: E402
from src.context_extraction import (  # noqa: E402
    extract_for_unit, get_decl_info, load_call_graph,
)


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def load_build_manifest(path: Path, step_hint: str) -> Dict[str, Dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"Build manifest not found: {path}\nRun {step_hint} first.")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    return {e["key"] if "key" in e else e.get("checkout_key"): e
            for e in manifest.get("call_graphs", [])}


def write_jsonl(rows: List[Dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def empty_context_row(warning: Dict[str, Any], status: str) -> Dict[str, Any]:
    row = dict(warning)
    row["resolve_status"] = status
    row["enclosing_function_identifier"] = None
    row["enclosing_function_bodies"] = []
    row["callers_by_level"] = {}
    return row


def run_realworld(args: argparse.Namespace) -> Path:
    warnings = load_jsonl(config.REAL_WORLD_WARNINGS)
    only = set(args.projects or [])
    if only:
        warnings = [w for w in warnings if w.get("project") in only]
    graphs = load_build_manifest(config.BUILD_MANIFEST, "build_call_graphs.py")

    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for w in warnings:
        grouped[config.checkout_key(w.get("project"), w.get("commit_id"))].append(w)

    out_rows: List[Dict[str, Any]] = []
    stats_total: Dict[str, int] = defaultdict(int)
    for key, group in grouped.items():
        entry = graphs.get(key)
        pickle_path = Path(entry["pickle"]) if entry else None
        if not pickle_path or not pickle_path.is_file():
            out_rows.extend(empty_context_row(w, "no_call_graph") for w in group)
            stats_total["no_call_graph"] += len(group)
            continue
        decl_info = get_decl_info(load_call_graph(pickle_path))
        repo_root = Path(entry["repo_root"])
        rows, stats = extract_for_unit(group, decl_info, source_root=repo_root,
                                       id_root=repo_root, depth=args.depth)
        out_rows.extend(rows)
        for k, v in stats.items():
            stats_total[k] += v
        print(f"[{key}] {len(group)} warnings -> {dict(stats)}")

    write_jsonl(out_rows, config.REAL_WORLD_CONTEXT)
    print(f"\nResolve status totals: {dict(stats_total)}")
    return config.REAL_WORLD_CONTEXT


def run_juliet(args: argparse.Namespace) -> Path:
    warnings = load_jsonl(config.JULIET_WARNINGS)
    graphs = load_build_manifest(config.JULIET_BUILD_MANIFEST,
                                 "build_call_graphs.py --dataset juliet")

    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for w in warnings:
        grouped[Path(w["file_path"]).parent.name].append(w)

    out_rows: List[Dict[str, Any]] = []
    stats_total: Dict[str, int] = defaultdict(int)
    for i, (key, group) in enumerate(sorted(grouped.items()), 1):
        entry = graphs.get(key)
        pickle_path = Path(entry["pickle"]) if entry else None
        if not pickle_path or not pickle_path.is_file():
            out_rows.extend(empty_context_row(w, "no_call_graph") for w in group)
            stats_total["no_call_graph"] += len(group)
            continue
        decl_info = get_decl_info(load_call_graph(pickle_path))
        # Juliet warning file_path is relative to data/juliet; identifiers too.
        rows, stats = extract_for_unit(group, decl_info, source_root=config.JULIET_SRC_DIR,
                                       id_root=config.JULIET_SRC_DIR, depth=args.depth)
        out_rows.extend(rows)
        for k, v in stats.items():
            stats_total[k] += v
        if i % 50 == 0:
            print(f"  ...processed {i}/{len(grouped)} testcases")

    write_jsonl(out_rows, config.JULIET_CONTEXT)
    print(f"\nResolve status totals: {dict(stats_total)}")
    return config.JULIET_CONTEXT


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset", choices=["realworld", "juliet"], default="realworld",
                   help="Which dataset to extract context for (default: realworld)")
    p.add_argument("--depth", type=int, default=2,
                   help="Caller neighborhood depth (default: 2; 0 = unlimited)")
    p.add_argument("--projects", nargs="*",
                   help="[realworld] subset of project keys to process (default: all)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.depth < 0:
        raise ValueError("--depth must be >= 0 (0 means unlimited)")
    config.ensure_output_dirs()

    out = run_juliet(args) if args.dataset == "juliet" else run_realworld(args)
    n = sum(1 for _ in out.open(encoding="utf-8"))
    print(f"Wrote {n} context rows: {out}")


if __name__ == "__main__":
    main()
