#!/usr/bin/env python3
"""Run the ContextLadder LLM triage stage over extracted-context warnings.

For each warning this runs ``--num-votes`` independent stabilization walks (see
``src/ladder.py``). Each walk starts at level 1 (the enclosing function) and adds
caller levels until a verdict stabilizes (three consecutive agreeing levels) or
the levels run out. Per-warning results are written to
``output/runs/<model>/votes_<model>_<warning_id>.json``.

Inputs (from ``extract_context.py``):
* ``realworld`` -> ``output/context/real_world_context.jsonl``
* ``juliet``    -> ``output/context/juliet_context.jsonl``

The prompt is the adverse-path + evidence-roles variant; its system prompt is
also exported to ``prompt_templates/adverse_path_roles_system_prompt.txt``.

Examples
--------
    python scripts/run_contextladder.py --dry-run
    python scripts/run_contextladder.py --provider anthropic --model claude-sonnet-4-6
    python scripts/run_contextladder.py --dataset juliet --num-votes 3 --workers 4
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

PKG_ROOT = Path(__file__).resolve().parents[1]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from src import config  # noqa: E402
from src import ladder  # noqa: E402
from src.ladder import ContextExtractionError  # noqa: E402
from src.llm_runner.runner import setup_client  # noqa: E402
from src.prompts.adverse_path_roles import construct_sections_from_warning_record  # noqa: E402

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional but recommended
    load_dotenv = None

CONTEXT_INPUTS = {
    "realworld": config.REAL_WORLD_CONTEXT,
    "juliet": config.JULIET_CONTEXT,
}


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def setup_logger(model: str) -> logging.Logger:
    log_dir = config.runs_logs_dir(model)
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = log_dir / f"run_{config._SANITIZE_RE.sub('_', model)}_{timestamp}.log"

    logger = logging.getLogger(f"contextladder.{timestamp}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.info(f"Logging to {log_path}")
    return logger


def warning_id_of(record: Dict[str, Any], index: int) -> str:
    wid = record.get("warning_id")
    if wid:
        return str(wid)
    proj = record.get("project") or "unknown"
    return f"{proj}__{index:04d}"


def run_dry(records: List[Dict[str, Any]], cap: Optional[int], blind: bool,
            logger: logging.Logger) -> None:
    """Validate that prompts build for level 1 and the deepest level; no API calls."""
    ok = no_ctx = build_err = 0
    for i, rec in enumerate(records):
        wid = warning_id_of(rec, i)
        if not rec.get("enclosing_function_bodies"):
            no_ctx += 1
            logger.info(f"[dry] {wid}: no enclosing context (resolve_status="
                        f"{rec.get('resolve_status')}) -> would RAISE (no verdict recorded)")
            continue
        max_level = ladder.compute_max_level(rec, cap)
        try:
            for level in (1, max_level):
                construct_sections_from_warning_record(rec, level=level, blind=blind)
            ok += 1
            logger.info(f"[dry] {wid}: levels 1..{max_level} buildable")
        except Exception as e:  # noqa: BLE001
            build_err += 1
            logger.info(f"[dry] {wid}: prompt build error at deepest level: {e}")
    logger.info(f"[dry] summary: buildable={ok}, no_context={no_ctx}, build_error={build_err}, "
                f"total={len(records)}")


def process_one(rec: Dict[str, Any], wid: str, *, client, args, blind: bool,
                prompt_variant: str, throttle, logger) -> Dict[str, Any]:
    evaluate = ladder.make_level_evaluator(
        rec, client=client, model=args.model, blind=blind, throttle=throttle,
        max_retries=args.max_retries, base_backoff_seconds=args.base_backoff, logger=logger)
    result = ladder.run_warning(rec, evaluate=evaluate, num_votes=args.num_votes,
                                max_level_cap=args.max_level_cap, prompt_variant=prompt_variant)
    result["warning_id"] = wid
    result["model"] = args.model
    result["provider"] = args.provider
    result["blind"] = blind
    out_path = config.warning_output_path(args.model, wid)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset", choices=["realworld", "juliet"], default="realworld")
    p.add_argument("--provider", default="anthropic",
                   help="LLM provider: anthropic | openai | deepseek | openrouter")
    p.add_argument("--model", default="claude-sonnet-4-6")
    p.add_argument("--num-votes", type=int, default=3,
                   help="Independent stabilization walks per warning (default: 3)")
    p.add_argument("--max-level-cap", type=int, default=None,
                   help="Cap the deepest ladder level (default: all available caller levels)")
    p.add_argument("--blind", action=argparse.BooleanOptionalAction, default=None,
                   help="Leakage prevention: strip code comments and add the bias-prevention "
                        "prompt block. Default: on for juliet, off for realworld.")
    p.add_argument("--workers", type=int, default=1,
                   help="Parallel warnings (each warning's calls run sequentially)")
    p.add_argument("--projects", nargs="*",
                   help="[realworld] subset of project keys to process")
    p.add_argument("--limit", type=int, default=None, help="Process at most N warnings")
    p.add_argument("--force", action="store_true",
                   help="Re-run warnings even if an output file already exists")
    p.add_argument("--tpm", type=int, default=0,
                   help="Rolling input tokens-per-minute budget (0 disables throttling)")
    p.add_argument("--max-retries", type=int, default=5,
                   help="Rate-limit retries per request")
    p.add_argument("--base-backoff", type=float, default=2.0,
                   help="Base seconds for exponential rate-limit backoff")
    p.add_argument("--dry-run", action="store_true",
                   help="Validate prompt building without any API calls or output files")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.num_votes < 1:
        raise ValueError("--num-votes must be >= 1")
    config.ensure_output_dirs()

    ctx_path = CONTEXT_INPUTS[args.dataset]
    if not ctx_path.is_file():
        raise FileNotFoundError(
            f"Context file not found: {ctx_path}\nRun extract_context.py --dataset {args.dataset} first.")

    records = load_jsonl(ctx_path)
    if args.projects:
        only = set(args.projects)
        records = [r for r in records if r.get("project") in only]
    if args.limit is not None:
        records = records[: args.limit]

    # Blind mode defaults on for Juliet (synthetic, naming/comment leakage).
    blind = args.blind if args.blind is not None else (args.dataset == "juliet")
    prompt_variant = "adverse_path_roles_blind" if blind else "adverse_path_roles"

    logger = setup_logger(args.model)
    logger.info(f"dataset={args.dataset} model={args.model} provider={args.provider} "
                f"num_votes={args.num_votes} workers={args.workers} blind={blind} "
                f"records={len(records)}")

    if args.dry_run:
        run_dry(records, args.max_level_cap, blind, logger)
        return

    if load_dotenv is not None:
        load_dotenv()
    client = setup_client(args.provider)
    throttle = ladder.RollingTokenThrottle(args.tpm) if args.tpm > 0 else None

    # Resume: skip warnings that already have an output file.
    pending = []
    skipped = 0
    for i, rec in enumerate(records):
        wid = warning_id_of(rec, i)
        if not args.force and config.warning_output_path(args.model, wid).is_file():
            skipped += 1
            continue
        pending.append((rec, wid))
    logger.info(f"pending={len(pending)} skipped_existing={skipped}")

    stats = {"TP": 0, "FP": 0, "UNKNOWN": 0, "stable": 0, "errors": 0, "no_context": 0}

    def handle(result: Dict[str, Any], wid: str) -> None:
        stats[result.get("label", "UNKNOWN")] = stats.get(result.get("label", "UNKNOWN"), 0) + 1
        if result.get("is_stable"):
            stats["stable"] += 1
        logger.info(f"[done] {wid}: label={result.get('label')} "
                    f"is_stable={result.get('is_stable')} majority={result.get('majority_label')} "
                    f"stable_walks={result.get('n_stable_walks')}/{result.get('num_votes')}")

    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(process_one, rec, wid, client=client, args=args, blind=blind,
                            prompt_variant=prompt_variant, throttle=throttle, logger=logger): wid
                for rec, wid in pending
            }
            for fut in as_completed(futures):
                wid = futures[fut]
                try:
                    handle(fut.result(), wid)
                except ContextExtractionError as e:
                    stats["no_context"] += 1
                    logger.error(f"[no-context] {wid}: {e}; no verdict recorded")
                except Exception as e:  # noqa: BLE001
                    stats["errors"] += 1
                    logger.error(f"[error] {wid}: {type(e).__name__}: {e}")
    else:
        for rec, wid in pending:
            try:
                handle(process_one(rec, wid, client=client, args=args, blind=blind,
                                   prompt_variant=prompt_variant, throttle=throttle,
                                   logger=logger), wid)
            except ContextExtractionError as e:
                stats["no_context"] += 1
                logger.error(f"[no-context] {wid}: {e}; no verdict recorded")
            except Exception as e:  # noqa: BLE001
                stats["errors"] += 1
                logger.error(f"[error] {wid}: {type(e).__name__}: {e}")

    logger.info(f"[summary] {stats}")
    logger.info(f"[summary] outputs in {config.runs_dir_for_model(args.model)}")


if __name__ == "__main__":
    main()
