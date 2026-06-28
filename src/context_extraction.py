"""Extract leveled caller context (with function bodies) for warnings.

Given a project's pickled call graph (from `build_call_graphs.py`) and a set of
warnings, this resolves each warning's enclosing function and produces, up to a
configurable depth:

  - ``enclosing_function_identifier`` and ``enclosing_function_bodies``
  - ``callers_by_level``  : {level -> {function_identifier -> [body, ...]}}

A *function identifier* is ``<path>::<signature_key>`` where ``<path>`` is the
source file relative to the dataset root (so output is portable), and bodies are
read from the checked-out source on disk.

This fuses the two original pipeline stages (`warning_call_graph_levels.py` and
`callgraph_levels_to_bodies.py`) into a single pass.
"""

from __future__ import annotations

import os
import pickle
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

DeclInfo = Dict[str, Dict[Tuple[Any, ...], Any]]
Node = Tuple[str, Tuple[Any, ...]]  # (decl_info file key, signature key)


# --------------------------------------------------------------------------- #
# Pickle loading
# --------------------------------------------------------------------------- #
def load_call_graph(pickle_path: Path) -> Any:
    """Load a call-graph pickle, tolerating legacy top-level module names."""
    try:
        with pickle_path.open("rb") as f:
            return pickle.load(f)
    except ModuleNotFoundError as exc:
        missing = str(exc).split("'")
        missing_name = missing[1] if len(missing) >= 2 else ""
        if missing_name not in {"TS", "AST"}:
            raise
        from src.extract_call_graph import TS as ts_mod  # type: ignore
        from src.extract_call_graph import AST as ast_mod  # type: ignore
        sys.modules.setdefault("TS", ts_mod)
        sys.modules.setdefault("AST", ast_mod)
        with pickle_path.open("rb") as f:
            return pickle.load(f)


def get_decl_info(obj: Any) -> DeclInfo:
    if hasattr(obj, "decl_info"):
        return obj.decl_info
    if isinstance(obj, dict):
        return obj.get("decl_info", obj)
    raise TypeError("Pickle must be a TS/BaseProfile object or contain decl_info")


# --------------------------------------------------------------------------- #
# Signature-key helpers
# --------------------------------------------------------------------------- #
def key_ftype(key: Tuple[Any, ...]) -> str:
    return key[0] if isinstance(key, tuple) and len(key) >= 1 else ""


def key_name(key: Tuple[Any, ...]) -> str:
    return key[1] if isinstance(key, tuple) and len(key) >= 2 else ""


def node_is_static(key: Tuple[Any, ...], fn: Any) -> bool:
    if isinstance(key, tuple) and len(key) >= 4:
        return bool(key[3])
    return bool(getattr(fn, "is_static", False))


def normalize_path(path: str) -> str:
    return str(path).replace("\\", "/")


def identifier_for(node: Node, id_root: Optional[Path]) -> str:
    """Stable identifier ``<rel-path>::<signature_key>`` for a graph node."""
    file_key, sig_key = node
    rel = file_key
    if id_root is not None:
        try:
            rel = os.path.relpath(file_key, id_root)
        except (ValueError, OSError):
            rel = file_key
    return f"{normalize_path(rel)}::{repr(sig_key)}"


# --------------------------------------------------------------------------- #
# Warning -> enclosing function resolution
# --------------------------------------------------------------------------- #
def _path_variants(path: str) -> List[str]:
    p = normalize_path(path)
    return list(dict.fromkeys([p, p.lstrip("./"), os.path.basename(p)]))


def find_decl_file_key(decl_info: DeclInfo, warning_file_path: str,
                       source_root: Optional[Path]) -> Optional[str]:
    if not warning_file_path:
        return None
    norm_to_orig = {normalize_path(k): k for k in decl_info.keys()}
    variants = _path_variants(warning_file_path)
    if source_root is not None:
        abs_path = normalize_path(str((source_root / warning_file_path)))
        variants.extend(_path_variants(abs_path))
    variants = list(dict.fromkeys(variants))

    for variant in variants:
        for norm, orig in norm_to_orig.items():
            if norm == variant or norm.endswith("/" + variant) or variant.endswith("/" + norm):
                return orig

    base = os.path.basename(normalize_path(warning_file_path))
    base_matches = [orig for norm, orig in norm_to_orig.items()
                    if os.path.basename(norm) == base]
    return base_matches[0] if len(base_matches) == 1 else None


def resolve_enclosing_node(warning: Dict[str, Any], decl_info: DeclInfo,
                           source_root: Optional[Path]) -> Tuple[Optional[Node], str]:
    file_key = find_decl_file_key(decl_info, str(warning.get("file_path", "")), source_root)
    if file_key is None:
        return None, "file_not_resolved"

    entries = decl_info.get(file_key, {})
    wanted_name = (warning.get("enclosing_function") or "").strip()

    try:
        line_0 = max(0, int(warning.get("line_number")) - 1)
    except (TypeError, ValueError):
        line_0 = None

    if line_0 is not None:
        enclosing: List[Node] = []
        for key, fn in entries.items():
            if key_ftype(key) != "func":
                continue
            start = int(getattr(fn, "start", [10 ** 9])[0])
            end = int(getattr(fn, "end", [-1])[0])
            if start <= line_0 <= end:
                enclosing.append((file_key, key))
        if enclosing:
            if wanted_name:
                named = [n for n in enclosing if key_name(n[1]) == wanted_name]
                if len(named) == 1:
                    return named[0], "line_name_match"
                if len(named) > 1:
                    named.sort(key=lambda n: (str(n[1][1]), str(n[1])))
                    return named[0], "line_name_multiple_first_used"
            if len(enclosing) == 1:
                return enclosing[0], "enclosing_line_match"

            def span_len(node: Node) -> int:
                fn = entries[node[1]]
                return max(0, int(getattr(fn, "end", [-1])[0]) - int(getattr(fn, "start", [10 ** 9])[0]))

            enclosing.sort(key=lambda n: (span_len(n), str(n[1])))
            return enclosing[0], "enclosing_line_multiple_smallest_span"

    if wanted_name:
        by_name = [(file_key, k) for k in entries
                   if key_ftype(k) == "func" and key_name(k) == wanted_name]
        if len(by_name) == 1:
            return by_name[0], "name_match"
        if len(by_name) > 1:
            return by_name[0], "name_multiple_first_used"

    return None, "function_not_resolved"


# --------------------------------------------------------------------------- #
# Call graph construction + traversal
# --------------------------------------------------------------------------- #
def build_callers_graph(decl_info: DeclInfo) -> Dict[Node, Set[Node]]:
    """Return caller adjacency (node -> set of functions that call it).

    Callee edges are computed as an intermediate step (from each function's
    recorded call names, with static symbols scoped to their file) and then
    inverted to obtain callers.
    """
    function_nodes: Dict[Node, Any] = {}
    by_name_non_static: Dict[str, List[Node]] = defaultdict(list)
    by_name_static: Dict[Tuple[str, str], List[Node]] = defaultdict(list)

    for file_path, entries in decl_info.items():
        for key, fn in entries.items():
            if key_ftype(key) != "func":
                continue
            node = (file_path, key)
            function_nodes[node] = fn
            if node_is_static(key, fn):
                by_name_static[(file_path, key_name(key))].append(node)
            else:
                by_name_non_static[key_name(key)].append(node)

    callers: Dict[Node, Set[Node]] = {node: set() for node in function_nodes}
    for source_node, fn in function_nodes.items():
        source_file, _ = source_node
        call_names = {part.split(";")[0]
                      for part in (getattr(fn, "calls", []) or [])
                      if isinstance(part, str) and part}
        for call_name in call_names:
            targets = (by_name_static.get((source_file, call_name), [])
                       + by_name_non_static.get(call_name, []))
            for target in targets:
                callers[target].add(source_node)
    return callers


def bfs_levels(start: Node, graph: Dict[Node, Set[Node]], depth: int) -> Dict[int, List[Node]]:
    """BFS neighborhood by level. depth == 0 means until fixpoint."""
    visited: Set[Node] = {start}
    frontier: Set[Node] = {start}
    result: Dict[int, List[Node]] = {}
    level = 1
    while frontier and (depth == 0 or level <= depth):
        nxt: Set[Node] = set()
        for node in frontier:
            nxt.update(graph.get(node, set()))
        nxt -= visited
        if not nxt:
            break
        result[level] = sorted(nxt, key=lambda n: (n[0], str(n[1])))
        visited.update(nxt)
        frontier = nxt
        level += 1
    return result


# --------------------------------------------------------------------------- #
# Body extraction
# --------------------------------------------------------------------------- #
def read_body(fn: Any, file_cache: Dict[str, List[str]]) -> Optional[str]:
    file_name = getattr(fn, "file_name", None)
    if not file_name:
        return None
    try:
        start = int(getattr(fn, "start")[0])
        end = int(getattr(fn, "end")[0])
    except Exception:  # noqa: BLE001
        return None
    p = Path(str(file_name))
    if not p.is_file():
        return None
    if file_name not in file_cache:
        file_cache[file_name] = p.read_text(encoding="utf-8", errors="replace").splitlines()
    lines = file_cache[file_name]
    if start < 0 or end < start or start >= len(lines):
        return None
    return "\n".join(lines[start:min(end, len(lines) - 1) + 1])


def _node_bodies(node: Node, decl_info: DeclInfo, file_cache: Dict[str, List[str]]) -> List[str]:
    fn = decl_info.get(node[0], {}).get(node[1])
    if fn is None:
        return []
    body = read_body(fn, file_cache)
    return [body] if body is not None else []


def _levels_with_bodies(levels: Dict[int, List[Node]], decl_info: DeclInfo,
                        id_root: Optional[Path], file_cache: Dict[str, List[str]]
                        ) -> Dict[str, Dict[str, List[str]]]:
    out: Dict[str, Dict[str, List[str]]] = {}
    for level, nodes in levels.items():
        out[str(level)] = {identifier_for(n, id_root): _node_bodies(n, decl_info, file_cache)
                           for n in nodes}
    return out


# --------------------------------------------------------------------------- #
# Per-unit driver
# --------------------------------------------------------------------------- #
def extract_for_unit(
    warnings: List[Dict[str, Any]],
    decl_info: DeclInfo,
    *,
    source_root: Optional[Path],
    id_root: Optional[Path],
    depth: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Resolve + extract leveled caller context for warnings sharing one call graph."""
    callers_graph = build_callers_graph(decl_info)
    file_cache: Dict[str, List[str]] = {}
    stats: Dict[str, int] = defaultdict(int)
    rows: List[Dict[str, Any]] = []

    for warning in warnings:
        node, status = resolve_enclosing_node(warning, decl_info, source_root)
        stats[status] += 1

        row = dict(warning)
        row["resolve_status"] = status
        row["enclosing_function_identifier"] = None
        row["enclosing_function_bodies"] = []
        row["callers_by_level"] = {}

        if node is not None:
            row["enclosing_function_identifier"] = identifier_for(node, id_root)
            row["enclosing_function_bodies"] = _node_bodies(node, decl_info, file_cache)
            row["callers_by_level"] = _levels_with_bodies(
                bfs_levels(node, callers_graph, depth), decl_info, id_root, file_cache)
        rows.append(row)

    return rows, dict(stats)
