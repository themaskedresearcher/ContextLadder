"""ContextLadder: progressive-context LLM triage with a stabilization rule.

For one warning we run ``num_votes`` independent *walks*. Each walk climbs the
context ladder, starting at level 1 (the enclosing function), then adding caller
levels one at a time (level 2 = caller level 1, level 3 = caller levels 1..2,
...). At every level the model returns ``TP``, ``FP``, or ``UNKNOWN``.

Stabilization rule (per walk):

  * A decisive verdict (``TP``/``FP``) at some level opens a *stabilization
    window* with that level as its base. We expand two more levels to confirm
    it. If three consecutive levels agree, the verdict is *stable* and the walk
    stops early.
  * If a later level disagrees with the current base verdict, a new window
    opens with that level as the new base (streak resets to 1).
  * ``UNKNOWN`` (or a failed/unparseable level) always voids the current window
    and forces expansion to the next level.
  * If the levels run out while a decisive verdict is pending (window opened but
    not yet confirmed by two more levels), the verdict is still considered
    stable ("nothing we can do"). If no decisive verdict was ever reached, the
    walk is not stable.

Aggregation across walks: the per-warning ``label`` is the majority of the walk
labels. ``is_stable`` is true when a strict majority of walks are stable and
agree with that label. When ``is_stable`` is false, consumers should fall back
to ``majority_label`` (the majority of decisive verdicts across every level of
every walk).
"""

from __future__ import annotations

import collections
import json
import random
import re
import threading
import time
from typing import Callable, Optional

from .llm_runner.runner import send_prompt
from .prompts.adverse_path_roles import construct_sections_from_warning_record

VALID_LABELS = frozenset({"TP", "FP", "UNKNOWN"})
DECISIVE_LABELS = frozenset({"TP", "FP"})


class ContextExtractionError(Exception):
    """Raised when a warning has no usable enclosing-function context.

    Such a warning cannot be triaged at all, so no verdict is recorded; the
    caller is expected to surface the failure rather than emit a default label.
    """

# Warning record fields copied verbatim into the output record (when present).
_METADATA_KEYS = (
    "warning_id", "project", "file_path", "line_number", "bug_type",
    "sink_type", "SinkType", "code_line", "function", "enclosing_function",
    "enclosing_function_identifier", "resolve_status", "commit_id",
    "project_url", "repo_url",
)


# --------------------------------------------------------------------------- #
# Verdict parsing
# --------------------------------------------------------------------------- #
def extract_output_label(response_text: Optional[str]) -> Optional[str]:
    """Pull the ``label`` (TP/FP/UNKNOWN) out of a model response, robustly."""
    if not response_text:
        return None
    try:
        parsed = json.loads(response_text)
        label = parsed.get("label")
        if label is not None:
            label = str(label).upper()
            return label if label in VALID_LABELS else None
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        pass
    match = re.search(r'"label"\s*:\s*"(TP|FP|UNKNOWN)"', response_text, re.IGNORECASE)
    return match.group(1).upper() if match else None


# --------------------------------------------------------------------------- #
# Voting helpers (ported from the original driver)
# --------------------------------------------------------------------------- #
def run_vote_from_level_labels(level_labels: list) -> str:
    """Majority over decisive (FP/TP) level labels; UNKNOWN if none or a tie."""
    decisive = [label for label in level_labels if label in DECISIVE_LABELS]
    if not decisive:
        return "UNKNOWN"
    counts = collections.Counter(decisive)
    top = max(counts.values())
    leaders = [label for label, count in counts.items() if count == top]
    return leaders[0] if len(leaders) == 1 else "UNKNOWN"


def majority_label(vote_labels: list, num_slots: int) -> str:
    """Strict-majority vote over TP/FP/UNKNOWN across walks."""
    counts = collections.Counter(label for label in vote_labels if label in VALID_LABELS)
    threshold = num_slots // 2 + 1
    for label in ("TP", "FP", "UNKNOWN"):
        if counts.get(label, 0) >= threshold:
            return label
    return "UNKNOWN"


# --------------------------------------------------------------------------- #
# Rate-limit throttle + retry (ported from the original driver)
# --------------------------------------------------------------------------- #
def estimate_input_tokens(context: str, user_input: str) -> int:
    return max(1, (len(context) + len(user_input)) // 4)


def is_rate_limit_error(error: Exception) -> bool:
    message = f"{type(error).__name__}: {error}".lower()
    return "ratelimiterror" in message or "rate_limit_error" in message or " 429" in message


class RollingTokenThrottle:
    """Keeps estimated input tokens under a rolling per-minute budget."""

    def __init__(self, per_minute_limit: int):
        self.per_minute_limit = per_minute_limit
        self.events: collections.deque = collections.deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self.events and now - self.events[0][0] >= 60.0:
            self.events.popleft()

    def wait_for_budget(self, needed_tokens: int, logger=None) -> None:
        if needed_tokens <= 0:
            return
        if needed_tokens > self.per_minute_limit:
            _emit(logger,
                  f"[throttle] Estimated input tokens ({needed_tokens}) exceed the per-minute "
                  f"budget ({self.per_minute_limit}); skipping pre-wait for this request.")
            return
        while True:
            sleep_seconds = 1.0
            with self._lock:
                now = time.time()
                self._prune(now)
                current_total = sum(tokens for _, tokens in self.events)
                if current_total + needed_tokens <= self.per_minute_limit:
                    self.events.append((time.time(), needed_tokens))
                    return
                excess = current_total + needed_tokens - self.per_minute_limit
                removed = 0
                sleep_until = None
                for ts, tokens in self.events:
                    removed += tokens
                    if removed >= excess:
                        sleep_until = ts + 60.05
                        break
                if sleep_until is not None:
                    sleep_seconds = max(0.1, sleep_until - time.time())
            _emit(logger, f"[throttle] Sleeping {sleep_seconds:.1f}s to stay under input TPM budget.")
            time.sleep(sleep_seconds)


def _emit(logger, message: str) -> None:
    if logger is not None:
        logger.info(message)
    else:
        print(message)


def send_prompt_with_retries(client, context: str, user_input: str, model: str,
                             max_retries: int, base_backoff_seconds: float, logger=None):
    attempt = 0
    while True:
        try:
            return send_prompt(client, context, user_input, model)
        except Exception as e:  # noqa: BLE001 - we re-raise non-rate-limit errors
            if not is_rate_limit_error(e) or attempt >= max_retries:
                raise
            sleep_seconds = (base_backoff_seconds * (2 ** attempt)) + random.uniform(0, 1.0)
            _emit(logger,
                  f"[retry] Rate limit hit on attempt {attempt + 1}/{max_retries + 1}. "
                  f"Sleeping {sleep_seconds:.1f}s before retry.")
            time.sleep(sleep_seconds)
            attempt += 1


# --------------------------------------------------------------------------- #
# Ladder geometry
# --------------------------------------------------------------------------- #
def compute_max_level(record: dict, cap: Optional[int] = None) -> int:
    """Deepest prompt level for a record (level 1 = enclosing function only).

    Caller level k is added at prompt level k+1, so the max prompt level is
    1 + (number of contiguous, non-empty caller levels starting at 1).
    """
    callers_by_level = record.get("callers_by_level") or {}
    n = 0
    while callers_by_level.get(str(n + 1)):
        n += 1
    max_level = n + 1
    if cap is not None:
        max_level = min(max_level, cap)
    return max(1, max_level)


def levels_plan(max_level: int) -> list:
    """Ordered levels for one walk: 1, 2, ..., max_level (no level 0)."""
    return list(range(1, max_level + 1))


# --------------------------------------------------------------------------- #
# Stabilization state machine (one walk)
# --------------------------------------------------------------------------- #
def stabilized_walk(levels: list, evaluate: Callable[[int], dict]) -> dict:
    """Run one stabilization walk over ``levels``.

    ``evaluate(level)`` must return a level record dict containing at least
    ``"level"`` and ``"label"`` (TP/FP/UNKNOWN/None). The walk may stop early
    once a verdict stabilizes; ``evaluate`` is only called for levels visited.
    """
    level_records: list = []
    base_label: Optional[str] = None
    base_level: Optional[int] = None
    streak = 0
    stable = False
    stable_label: Optional[str] = None
    confirmed_at_level: Optional[int] = None
    stop_reason: Optional[str] = None

    for level in levels:
        rec = evaluate(level)
        label = rec.get("label")
        level_records.append(rec)

        if label not in DECISIVE_LABELS:  # UNKNOWN, None, or failed level -> void window
            base_label, base_level, streak = None, None, 0
            continue

        if label != base_label:
            base_label, base_level, streak = label, level, 1
        else:
            streak += 1

        if streak >= 3:
            stable = True
            stable_label = base_label
            confirmed_at_level = level
            stop_reason = "confirmed_two_levels_above"
            break
    else:
        # Levels exhausted without three-in-a-row.
        if base_label is not None:
            stable = True
            stable_label = base_label
            stop_reason = "exhausted_pending_verdict"
        else:
            stable = False
            stop_reason = "exhausted_no_verdict"

    level_labels = [r.get("label") for r in level_records]
    within_majority = run_vote_from_level_labels(level_labels)
    walk_label = stable_label if stable else within_majority

    return {
        "stable": stable,
        "stable_label": stable_label,
        "walk_label": walk_label,
        "stop_reason": stop_reason,
        "base_label_at_stop": base_label,
        "base_level": base_level,
        "confirmed_at_level": confirmed_at_level,
        "stopped_early": len(level_records) < len(levels),
        "levels_evaluated": [r.get("level") for r in level_records],
        "level_labels": level_labels,
        "levels": level_records,
    }


# --------------------------------------------------------------------------- #
# Per-warning orchestration
# --------------------------------------------------------------------------- #
def _metadata(record: dict) -> dict:
    meta = {k: record[k] for k in _METADATA_KEYS if k in record}
    # The dataset's ground-truth label collides with our prediction key.
    if "label" in record:
        meta["gold_label"] = record["label"]
    return meta


def aggregate_walks(record: dict, walks: list, num_votes: int, levels: list,
                    prompt_variant: str = "adverse_path_roles") -> dict:
    walk_labels = [w["walk_label"] for w in walks]
    final_label = majority_label(walk_labels, num_votes)

    threshold = num_votes // 2 + 1
    stable_for_final = [w for w in walks if w["stable"] and w["walk_label"] == final_label]
    is_stable = final_label in DECISIVE_LABELS and len(stable_for_final) >= threshold

    all_level_labels = [lbl for w in walks for lbl in w["level_labels"]]
    maj_label = run_vote_from_level_labels(all_level_labels)

    out = _metadata(record)
    out.update({
        "prompt_variant": prompt_variant,
        "num_votes": num_votes,
        "levels_plan": levels,
        "max_level": levels[-1] if levels else 0,
        "label": final_label,
        "is_stable": is_stable,
        "majority_label": maj_label,
        "walk_labels": walk_labels,
        "n_stable_walks": sum(1 for w in walks if w["stable"]),
        "walks": walks,
    })
    return out


def run_warning(record: dict, *, evaluate: Callable[[int], dict],
                num_votes: int, max_level_cap: Optional[int] = None,
                prompt_variant: str = "adverse_path_roles") -> dict:
    """Run ``num_votes`` independent stabilization walks for one warning.

    ``evaluate(level)`` performs a fresh model call for the given level and
    returns a level record; it is invoked anew for every walk so the votes are
    independent.

    Raises ``ContextExtractionError`` if the record has no usable enclosing
    function context (failed context extraction). No verdict is produced in that
    case — the warning is left unprocessed for the caller to surface.
    """
    if not record.get("enclosing_function_bodies"):
        wid = record.get("warning_id", "<unknown>")
        status = record.get("resolve_status", "unknown")
        raise ContextExtractionError(
            f"warning {wid} has no enclosing-function context "
            f"(resolve_status={status}); cannot triage")

    max_level = compute_max_level(record, max_level_cap)
    levels = levels_plan(max_level)
    walks = [stabilized_walk(levels, evaluate) for _ in range(num_votes)]
    return aggregate_walks(record, walks, num_votes, levels, prompt_variant=prompt_variant)


def make_level_evaluator(record: dict, *, client, model: str,
                         blind: bool = False,
                         throttle: Optional[RollingTokenThrottle] = None,
                         max_retries: int = 5, base_backoff_seconds: float = 2.0,
                         logger=None) -> Callable[[int], dict]:
    """Build an ``evaluate(level)`` that queries the model for one warning.

    When ``blind`` is set, prompts use the bias-prevention system prompt and have
    code comments stripped (leakage prevention for benchmarks like Juliet).
    """

    def evaluate(level: int) -> dict:
        try:
            sections = construct_sections_from_warning_record(record, level=level, blind=blind)
        except Exception as e:  # noqa: BLE001 - missing context for this level
            return {"level": level, "label": None, "response": None, "reasoning": None,
                    "usage": None, "estimated_input_tokens": 0,
                    "error": f"prompt_build_error: {type(e).__name__}: {e}"}

        est = estimate_input_tokens(sections.context, sections.input)
        if throttle is not None:
            throttle.wait_for_budget(est, logger)
        try:
            answer, reasoning, usage = send_prompt_with_retries(
                client, sections.context, sections.input, model,
                max_retries, base_backoff_seconds, logger)
            return {"level": level, "label": extract_output_label(answer),
                    "response": answer, "reasoning": reasoning or None,
                    "usage": usage, "estimated_input_tokens": est, "error": None}
        except Exception as e:  # noqa: BLE001 - record the failure, void the level
            return {"level": level, "label": None, "response": None, "reasoning": None,
                    "usage": None, "estimated_input_tokens": est,
                    "error": f"{type(e).__name__}: {e}"}

    return evaluate
