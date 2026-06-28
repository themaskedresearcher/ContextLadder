"""Clone and check out the project sources referenced by the warning dataset.

Real-world warnings ship only `project_url` + `commit_id` (not source). This
module fetches each referenced project at the exact revision so a warning's
`file_path` resolves on disk:

  - Git projects:     clone `project_url`, then `checkout <commit_id>`.
  - Tarball projects: download `project_url` (e.g. *.tar.xz) and extract it
                      (used by projects distributed as release archives, with
                      no commit hash).

Notes
-----
* Some projects appear at MORE THAN ONE commit in the dataset, so checkouts are
  keyed by ``(project, commit)`` -- each distinct revision gets its own
  directory ``<clone_root>/<project>__<short_commit>/``. Tarball projects (no
  commit) use ``<clone_root>/<project>/``.
* Every git invocation uses the "safe directory" fix
  (``git -c safe.directory=<path>``) to avoid "fatal: detected dubious
  ownership" errors when the clone lives on a mount/filesystem whose ownership
  differs from the current user. This is applied per-invocation and does NOT
  modify the user's global git config.
* Only the Python standard library is required.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TARBALL_SUFFIXES = (".tar.xz", ".tar.gz", ".tgz", ".tar.bz2", ".tar")


# --------------------------------------------------------------------------- #
# Git command helpers (with the safe.directory fix)
# --------------------------------------------------------------------------- #
def git_trust_repo_args(repo: Path) -> List[str]:
    """git prefix that trusts `repo` for this invocation only.

    Avoids 'fatal: detected dubious ownership' without touching global config.
    """
    try:
        safe = str(repo.resolve())
    except OSError:
        safe = str(repo.expanduser().absolute())
    return ["git", "-c", f"safe.directory={safe}"]


def run_cmd(args: List[str]) -> None:
    completed = subprocess.run(args)
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed ({completed.returncode}): {' '.join(args)}")


def run_cmd_capture(args: List[str], *, cwd: Optional[Path] = None) -> str:
    completed = subprocess.run(
        args, cwd=str(cwd) if cwd else None, capture_output=True, text=True
    )
    if completed.returncode != 0:
        err = (completed.stderr or "").strip() or (completed.stdout or "").strip()
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {' '.join(args)}\n{err}"
        )
    return (completed.stdout or "").strip()


def git_head_commit(repo: Path) -> str:
    return run_cmd_capture(
        [*git_trust_repo_args(repo), "-C", str(repo), "rev-parse", "HEAD"]
    )


def peeled_commit(repo: Path, ref: str) -> str:
    return run_cmd_capture(
        [*git_trust_repo_args(repo), "-C", str(repo), "rev-parse", f"{ref}^{{commit}}"]
    )


# --------------------------------------------------------------------------- #
# Checkout primitives
# --------------------------------------------------------------------------- #
def is_tarball_url(project_url: str) -> bool:
    return project_url.lower().rstrip("/").endswith(TARBALL_SUFFIXES)


def checkout_dir_name(project: str, commit_id: Optional[str]) -> str:
    """Directory name for a given (project, commit) checkout.

    Single source of truth for per-checkout naming (also used by
    `config.checkout_key` / `config.call_graph_path`).
    """
    safe_project = re.sub(r"[^A-Za-z0-9._-]", "_", project)
    if commit_id and commit_id.strip():
        return f"{safe_project}__{commit_id.strip()[:12]}"
    return safe_project


def checkout_git_project(
    *, project_url: str, commit_id: str, dest: Path, force_clone: bool, blobless: bool
) -> str:
    commit_id = commit_id.strip()
    if not commit_id:
        raise ValueError("commit_id is empty")

    if force_clone and dest.exists():
        shutil.rmtree(dest, ignore_errors=True)

    if dest.exists() and (dest / ".git").is_dir():
        try:
            if git_head_commit(dest) == peeled_commit(dest, commit_id):
                return git_head_commit(dest)
        except RuntimeError:
            pass
        run_cmd([*git_trust_repo_args(dest), "-C", str(dest), "fetch", "--all", "--tags"])
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        clone_cmd = [*git_trust_repo_args(dest), "clone"]
        if blobless:
            clone_cmd.append("--filter=blob:none")
        clone_cmd.extend([project_url, str(dest)])
        run_cmd(clone_cmd)

    run_cmd([*git_trust_repo_args(dest), "-C", str(dest), "checkout", commit_id])
    expected = peeled_commit(dest, commit_id)
    actual = git_head_commit(dest)
    if actual != expected:
        raise RuntimeError(
            f"After clone/checkout HEAD is {actual}, expected {expected} for {commit_id}."
        )
    return actual


def download_and_extract_tarball(project_url: str, dest: Path, *, force: bool) -> None:
    if dest.exists():
        if force:
            shutil.rmtree(dest)
        else:
            return
    dest.parent.mkdir(parents=True, exist_ok=True)

    archive_path = dest.parent / Path(project_url.rstrip("/").split("/")[-1])
    if not archive_path.exists():
        print(f"  downloading {project_url}")
        urllib.request.urlretrieve(project_url, archive_path)

    tmp_extract = dest.parent / f".{dest.name}.extract_tmp"
    if tmp_extract.exists():
        shutil.rmtree(tmp_extract)
    tmp_extract.mkdir(parents=True, exist_ok=True)

    print(f"  extracting {archive_path.name}")
    with tarfile.open(archive_path, "r:*") as tf:
        tf.extractall(tmp_extract)

    top_level = [p for p in tmp_extract.iterdir() if p.name not in {".", ".."}]
    extracted_root = top_level[0] if len(top_level) == 1 and top_level[0].is_dir() else tmp_extract
    if dest.exists():
        shutil.rmtree(dest)
    extracted_root.rename(dest)
    shutil.rmtree(tmp_extract, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Dataset-driven driver
# --------------------------------------------------------------------------- #
def load_warnings(jsonl_path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in
            jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def resolve_warning_file(repo_root: Path, file_path: Optional[str]) -> Optional[str]:
    """Resolve a warning's `file_path` to an existing repo-relative path.

    Most records store a full repo-relative path that resolves directly. A few
    store only a basename or a path rooted at a build subdirectory (e.g.
    ``options.c`` for ``src/options.c``). For those we search the checkout and
    accept the match only when it is unambiguous.

    Returns the resolved POSIX path relative to `repo_root`, or None.
    """
    if not repo_root or not file_path:
        return None
    target = file_path.replace("\\", "/")
    if (repo_root / target).is_file():
        return target

    base = target.rsplit("/", 1)[-1]
    matches = [p for p in repo_root.rglob(base) if p.is_file()]
    # Prefer matches whose relative path ends with the recorded suffix.
    rels = [p.relative_to(repo_root).as_posix() for p in matches]
    suffix = [r for r in rels if r == target or r.endswith("/" + target)]
    chosen = suffix or rels
    return chosen[0] if len(chosen) == 1 else None


def unique_checkouts(
    warnings: List[Dict[str, Any]], only_projects: Optional[List[str]] = None
) -> List[Tuple[str, str, Optional[str]]]:
    """Distinct (project, project_url, commit_id) triples to fetch."""
    selected = set(only_projects or [])
    seen = []
    for w in warnings:
        project = w.get("project")
        url = w.get("project_url")
        commit = (w.get("commit_id") or None)
        if selected and project not in selected:
            continue
        if not url:
            continue
        triple = (project, url, commit)
        if triple not in seen:
            seen.append(triple)
    return seen


def clone_dataset(
    *,
    warnings_path: Path,
    clone_root: Path,
    force_clone: bool = False,
    blobless: bool = False,
    only_projects: Optional[List[str]] = None,
    manifest_path: Optional[Path] = None,
) -> Dict[str, Any]:
    warnings = load_warnings(warnings_path)
    clone_root.mkdir(parents=True, exist_ok=True)

    # repo root per (project, commit) key.
    repo_root_for: Dict[Tuple[str, Optional[str]], Path] = {}
    checkouts: List[Dict[str, Any]] = []
    ok = fail = 0

    for project, url, commit in unique_checkouts(warnings, only_projects):
        dest = clone_root / checkout_dir_name(project, commit)
        repo_root_for[(project, commit)] = dest
        entry: Dict[str, Any] = {
            "project": project, "url": url, "commit": commit,
            "dest": str(dest), "type": "tarball" if is_tarball_url(url) else "git",
            "checked_out_commit": None, "ok": False,
        }
        try:
            if is_tarball_url(url):
                download_and_extract_tarball(url, dest, force=force_clone)
                entry["checked_out_commit"] = None
            else:
                if not commit:
                    raise ValueError(f"{project}: git project missing commit_id")
                entry["checked_out_commit"] = checkout_git_project(
                    project_url=url, commit_id=commit, dest=dest,
                    force_clone=force_clone, blobless=blobless,
                )
            entry["ok"] = True
            ok += 1
            print(f"[ok] {project} @ {commit or 'tarball'} -> {dest.name}")
        except Exception as exc:  # noqa: BLE001 - report and continue
            entry["error"] = str(exc)
            fail += 1
            print(f"[fail] {project} @ {commit or 'tarball'}: {exc}")
        checkouts.append(entry)

    # Per-warning resolution + existence check.
    warning_entries: List[Dict[str, Any]] = []
    files_ok = files_missing = 0
    for w in warnings:
        if only_projects and w.get("project") not in set(only_projects):
            continue
        key = (w.get("project"), w.get("commit_id") or None)
        repo_root = repo_root_for.get(key)
        rel = w.get("file_path")
        resolved = resolve_warning_file(repo_root, rel) if repo_root else None
        files_ok += bool(resolved)
        files_missing += (not resolved)
        warning_entries.append({
            "warning_id": w.get("warning_id"),
            "repo_root": str(repo_root) if repo_root else None,
            "file_path": rel,
            "resolved_file_path": resolved,
            "file_exists": bool(resolved),
        })

    manifest = {
        "warnings_jsonl": str(warnings_path.resolve()),
        "clone_root": str(clone_root.resolve()),
        "checkouts": checkouts,
        "warnings": warning_entries,
        "summary": {
            "checkouts_total": len(checkouts), "checkouts_ok": ok, "checkouts_failed": fail,
            "warning_files_resolved": files_ok, "warning_files_missing": files_missing,
        },
    }
    manifest_path = manifest_path or (clone_root / "checkout_manifest.json")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nWrote manifest: {manifest_path}")
    print(f"Checkouts: {ok} ok, {fail} failed | "
          f"warning files: {files_ok} resolved, {files_missing} missing")
    return manifest
