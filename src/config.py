"""Central configuration: canonical input/output locations for the pipeline.

Every pipeline step reads its inputs from, and writes its outputs to, the
locations defined here, so each step knows where the previous step left its
artifacts. The layout is::

    replication_package/
      data/                         # shipped datasets (inputs, read-only)
        real_world_warnings.jsonl
        juliet_warnings.jsonl
        juliet/                     # vendored Juliet sources
      output/                       # ALL generated artifacts (git-ignored)
        clones/                     # [1] cloned/checked-out project sources
          <project>__<commit>/
        checkout_manifest.json      # [1] warning -> on-disk repo root mapping
        call_graphs/                # [2] pickled call graphs per checkout
          <project>__<commit>.pickle
        context/                    # [3] per-warning extracted context (JSON)
        runs/                       # [4] raw LLM ladder outputs, per model
          <model>/
        results/                    # [5] aggregated metrics / summaries
        logs/                       # logs from any step

Override the output root by setting the ``CONTEXTLADDER_OUTPUT`` environment
variable (e.g. to put large artifacts on another drive).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]")

# --------------------------------------------------------------------------- #
# Roots
# --------------------------------------------------------------------------- #
PKG_ROOT = Path(__file__).resolve().parents[1]

# Inputs (shipped with the package, read-only).
DATA_DIR = PKG_ROOT / "data"
REAL_WORLD_WARNINGS = DATA_DIR / "real_world_warnings.jsonl"
JULIET_WARNINGS = DATA_DIR / "juliet_warnings.jsonl"
JULIET_SRC_DIR = DATA_DIR / "juliet"

# Output root (override with CONTEXTLADDER_OUTPUT=/some/path).
OUTPUT_DIR = Path(os.environ.get("CONTEXTLADDER_OUTPUT", PKG_ROOT / "output")).resolve()

# Per-step output locations.
CLONES_DIR = OUTPUT_DIR / "clones"            # [1] clone_projects.py
CHECKOUT_MANIFEST = OUTPUT_DIR / "checkout_manifest.json"  # [1]
CALL_GRAPHS_DIR = OUTPUT_DIR / "call_graphs"  # [2] build_call_graphs.py
JULIET_CALL_GRAPHS_DIR = CALL_GRAPHS_DIR / "juliet"  # [2] per-testcase graphs
BUILD_MANIFEST = CALL_GRAPHS_DIR / "build_manifest.json"            # [2] real-world
JULIET_BUILD_MANIFEST = CALL_GRAPHS_DIR / "build_manifest_juliet.json"  # [2] juliet
CONTEXT_DIR = OUTPUT_DIR / "context"          # [3] extract_context.py
REAL_WORLD_CONTEXT = CONTEXT_DIR / "real_world_context.jsonl"  # [3]
JULIET_CONTEXT = CONTEXT_DIR / "juliet_context.jsonl"          # [3]
RUNS_DIR = OUTPUT_DIR / "runs"                # [4] run_contextladder.py
RESULTS_DIR = OUTPUT_DIR / "results"          # [5] aggregate_results.py
LOGS_DIR = OUTPUT_DIR / "logs"

ALL_OUTPUT_DIRS = [
    OUTPUT_DIR, CLONES_DIR, CALL_GRAPHS_DIR, CONTEXT_DIR, RUNS_DIR, RESULTS_DIR, LOGS_DIR,
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ensure_output_dirs() -> None:
    """Create the output directory tree if it does not yet exist."""
    for d in ALL_OUTPUT_DIRS:
        d.mkdir(parents=True, exist_ok=True)


def checkout_key(project: str, commit_id: Optional[str]) -> str:
    """Stable per-(project, commit) identifier used for clone/call-graph names.

    Delegates to `repo_clone.checkout_dir_name` (single source of truth): git
    checkouts get a short-commit suffix; commit-less (tarball) projects use the
    bare project name.
    """
    from .repo_clone import checkout_dir_name
    return checkout_dir_name(project, commit_id)


def clone_dir(project: str, commit_id: Optional[str]) -> Path:
    """Checkout directory for a (project, commit) under CLONES_DIR."""
    return CLONES_DIR / checkout_key(project, commit_id)


def call_graph_path(project: str, commit_id: Optional[str]) -> Path:
    """Pickled call-graph path for a (project, commit) under CALL_GRAPHS_DIR."""
    return CALL_GRAPHS_DIR / f"{checkout_key(project, commit_id)}.pickle"


def juliet_call_graph_path(testcase_key: str) -> Path:
    """Pickled call-graph path for a single Juliet testcase folder."""
    return JULIET_CALL_GRAPHS_DIR / f"{_SANITIZE_RE.sub('_', testcase_key)}.pickle"


def runs_dir_for_model(model: str) -> Path:
    """Raw LLM output directory for a given model under RUNS_DIR."""
    return RUNS_DIR / _SANITIZE_RE.sub("_", model)


def runs_logs_dir(model: str) -> Path:
    """Log directory for a model's ContextLadder run."""
    return runs_dir_for_model(model) / "logs"


def warning_output_path(model: str, warning_id: str) -> Path:
    """Per-warning JSON output path for the LLM ladder stage.

    Warning ids may contain ``:`` (e.g. ``trueprint-5.4:0001``); ``:`` becomes
    ``__`` so the name is filesystem-safe on every OS.
    """
    safe_model = _SANITIZE_RE.sub("_", model)
    safe_id = warning_id.replace(":", "__")
    return runs_dir_for_model(model) / f"votes_{safe_model}_{safe_id}.json"
