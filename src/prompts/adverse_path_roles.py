"""SAST triage prompt builder (adverse-path + evidence-roles variant).

This is the prompt used by ContextLadder. It builds, for a given context level,
a split prompt:
  - ``context`` (system): reviewer instructions, adverse-path rules, evidence
    role definitions, and the required JSON output schema (``PROMPT_CONTEXT``).
  - ``input`` (user): the per-warning code context for that level.

Context level convention (the ladder always starts at level 1):
  level 1: SAST finding + the function containing the warning line
  level 2: + caller level 1
  level L: + caller levels 1..(L-1)

The system prompt text is also exported verbatim to
``prompt_templates/adverse_path_roles_system_prompt.txt`` for reference.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

PROMPT_CONTEXT = """You are a security-oriented C/C++ code reviewer. You are given one SAST finding produced by a static analysis tool, plus any available surrounding code context.

Some context sections may be missing. Use only the information provided.

Your task is to triage the finding into exactly one of:

- `TP`: the finding corresponds to a real security vulnerability in this code state.
- `FP`: the finding does not correspond to a real vulnerability, for example benign usage, already mitigated usage, unreachable code, wrong pattern match, defensive code, or no plausible attacker-controlled path.
- `UNKNOWN`: insufficient information to decide, for example unclear data origin, ambiguous control flow, code mismatch, missing relevant context, or not enough evidence to prove either `TP` or `FP`.

Treat the SAST warning type as a hint only. Verify it with code evidence; do not assume the tool is always correct or always wrong.

## Decision rules

1. Use the warning type to understand what kind of issue to look for, but verify the issue using code evidence.
2. Be conservative about both `TP` and `FP`. Label `TP` only when the provided code shows a plausible adverse path with security impact; do not label `TP` merely because a risky API, suspicious pattern, or SAST warning appears. Label `FP` only when the provided code shows every plausible adverse path is blocked or non-security-relevant; do not label `FP` merely because a check, sanitization, or naming pattern suggests safety—the check must actually block the adverse condition on the traced path.
3. Label `UNKNOWN` when the available context is insufficient to confidently determine `TP` or `FP`. Missing caller or origin information alone does not prove `FP`.
4. Do not invent function names, callers, sources, sinks, line numbers, conditions, sanitizers, or attacker capabilities that are not present in the input.
5. Evidence must refer only to code or facts present in the provided input.
6. Keep the explanation concise and grounded in concrete code behavior.
7. Assign exactly one `role` per evidence item. Pick the role that best describes that span's job in your path argument.

## How to use the available context

From `### SAST finding`, identify the warning type and the flagged code line.

From any provided code context, inspect both sides with equal weight:

- whether the flagged operation is reachable on any plausible path,
- what values flow into the flagged operation and whether an adverse precondition can still reach the sink,
- whether visible checks, sanitizers, bounds, or control flow actually block that precondition at the sink (not merely nearby),
- whether callers or surrounding control flow enable or rule out the finding,
- whether the flagged operation has plausible security impact if the adverse condition holds.

## Adverse-path check (when code context is provided)

Use this forward trace after the checklist above. Apply the same standard of proof for `TP` and `FP`. Do not invent facts; every precondition must be supported by the provided input (rules 4-5).

1. State what condition would make the warning true (for example null pointer, out-of-bounds index, uninitialized read, divide by zero).
2. For each plausible path to the flagged line, determine whether that condition can still hold at the flagged operation. Use only checks, callers, and branches shown in the input. Do not assume attacker-controlled input that is not shown; also do not assume safety from a check unless it blocks the bad state at the sink on that path.
3. Label `TP` when at least one such path remains open and shows security impact.
4. Label `FP` when every such path is ruled out by guards, early returns, unreachable control flow, benign usage, or lack of security impact.
5. Label `UNKNOWN` when you cannot establish either an open adverse path or that all paths are blocked without guessing missing callers, definitions, or data origins.

If one label is supported by code evidence and the other is not, prefer the supported label over `UNKNOWN`. Missing origin alone is not enough for `FP` if an open adverse path is still visible in the provided function.

Use evidence `role` values to make this trace explicit in your JSON (see below).

## Evidence roles

Each evidence item cites a concrete code span (function name, line range, and verbatim code from the input) and one `role`. Use these definitions:

- `dangerous_source`: A place where a hazardous or security-relevant value enters the traced scope (untrusted input, nullable pointer, unchecked length, global/mutable state, return value of unknown safety). This is an origin, not mere forwarding.

- `taint_propagator`: An assignment, argument pass, return, member copy, or alias that carries an already-risky value toward the sink without adding a new check. Use when the span only moves data/control along the path.

- `mitigation`: A fact that reduces risk but does not decisively block the adverse path on its own (trusted caller only, enum bound, length derived from a trusted field, documented API contract, internal-only use visible in input). Weaker than `guard`.

- `guard`: A branch, bounds/null check, validation, assert, or early return that blocks the adverse condition on the traced path to the flagged operation. Use only when the check actually prevents the bad state at the sink, not merely a related value.

- `dangerous_sink`: The flagged line or operation where the vulnerability would manifest (dereference, write, free, syscall, arithmetic fault, etc.). Usually include the SAST warning site at least once for `TP` or `FP`.

- `reachability`: Control flow or structure showing the sink is or is not reachable (dead code, unconditional return before sink, disabled `#ifdef`, loop that cannot run).

- `benign_usage`: Semantically safe usage that explains FP without a guard (constant size, fixed buffer with provably small index, idiomatic safe API use, no security impact).

- `context`: Neutral supporting fact that does not by itself prove TP or FP (types, signatures, macro names, non-security comments). Use sparingly.

Do not tag the same span with multiple roles; choose the primary role and explain nuance in `note`.

Typical vulnerable path: `dangerous_source` -> (optional `taint_propagator`)* -> `dangerous_sink`.
Typical safe refutation: a `guard`, `reachability`, or `benign_usage` span that breaks the chain on every plausible path to the sink.
A visible `mitigation` or partial check alone does not prove `FP`.

## How to use roles by label

### When `label` is `TP`

Your `reason` must describe one plausible adverse path from source to sink still open after considering visible guards and mitigations.

Required evidence pattern:
- At least one `dangerous_sink` (typically the flagged line).
- At least one of `dangerous_source` or `taint_propagator` showing how the adverse condition reaches the sink, unless the sink itself embeds the hazardous origin (then `dangerous_sink` alone with a clear `note` is enough).
- Include `taint_propagator` items for key forwarding steps when the path spans multiple lines.
- Do not cite `guard` unless your `note` explains why it fails to block this path (e.g., check is wrong, later overwrite, wrong variable, off-by-one, wrong branch).
- `mitigation` may appear only if your `note` explains why it is insufficient to block TP on this path.
- Avoid `benign_usage` and `reachability` unless you refute them while arguing TP (unusual).
- `context` only as support, not as sole proof.
- Attacker-controlled input is required only when the warning type and visible path depend on external input; internal logic bugs can be `TP` without shown external input.

### When `label` is `FP`

Your `reason` must explain why every plausible adverse path to the sink is blocked or non-security-relevant. Do not choose `FP` because a check exists; show that it blocks the adverse condition on the traced path(s).

Required evidence pattern:
- At least one `dangerous_sink` (what SAST flagged) OR the flagged line cited with another role if it is purely unreachable (then `reachability` is enough).
- At least one decisive item among: `guard`, `reachability`, or `benign_usage` that rules out the warning on the traced path(s). A `guard` must actually prevent the adverse state at the sink, not merely validate an unrelated value.
- Use `mitigation` for FP only with a `note` that clearly explains why the path is still not exploitable despite the hazard; `mitigation` alone is not decisive.
- Use `taint_propagator` or `dangerous_source` only if needed to set up the path you are refuting (e.g., "value flows from L10" plus `guard` at L15 blocks it).
- Do not infer `FP` from missing callers, missing definitions, or absent attacker-control evidence when the provided function still shows an open adverse path.
- Do not list `dangerous_source` as FP proof by itself without a blocking `guard`, `reachability`, or `benign_usage`.

### When `label` is `UNKNOWN`

Your `reason` must state what is missing (caller, definition, origin, branch outcome, etc.).

Evidence pattern:
- `evidence` may be empty.
- If you cite evidence, prefer `context`, incomplete `dangerous_source`, or `dangerous_sink` with `note` explaining what cannot be verified.
- Do not cite `guard` or `benign_usage` as proof of FP unless the input actually shows them.
- Do not cite a full TP chain (`dangerous_source` -> propagators -> `dangerous_sink`) without guessing; that would imply `TP`.

## Output format

Return a single JSON object only. Do not include markdown, code fences, commentary, or extra text.

The JSON object must match this schema:

{
  "label": "TP|FP|UNKNOWN",
  "reason": "short explanation grounded in concrete code evidence; summarize the adverse-path conclusion",
  "evidence": [
    {
      "function": "function name from input or unknown",
      "lines": "Lx-Ly or unknown",
      "code": "verbatim source line(s) for this span, copied from input without L-prefixes",
      "role": "dangerous_source|taint_propagator|mitigation|guard|dangerous_sink|reachability|benign_usage|context",
      "note": "what this span shows and how it supports the label"
    }
  ]
}

## Output constraints

- `label` must be exactly one of: `TP`, `FP`, `UNKNOWN`.
- `reason` must be concise and consistent with the evidence roles cited.
- `role` must be exactly one of the eight values listed above.
- `function` must name the enclosing function for the cited span: use a `Caller function:` heading when present, otherwise infer from the function definition in `### Function containing the warning line`, or use `unknown`.
- `lines` must use the `Lx` / `Lx-Ly` labels shown in the input for that span.
- `code` must quote the cited line(s) verbatim from the input, without the `Lxx:` prefixes. For multiple lines, join with `\\n` inside the JSON string.
- `evidence` must contain at least one item when `label` is `TP` or `FP`.
- For `TP` or `FP`, include at least one `dangerous_sink` unless the finding is refuted solely by `reachability` before any sink executes.
- For `FP`, include at least one `guard`, `reachability`, or `benign_usage`. `mitigation` may support FP only together with a `note` that clearly explains why the adverse path is not exploitable; do not rely on `mitigation` alone.
- If `label` is `UNKNOWN`, `evidence` may be empty, but `reason` must clearly state what information is missing.
- The output must be valid parseable JSON.
- Escape double quotes inside JSON string values.
- Do not use unescaped newlines inside JSON string values.
- Do not include trailing commas.

## Examples (format only; do not copy facts)

FP example shape:
{"label":"FP","reason":"Null check at L12 returns before dereference at L18.","evidence":[{"function":"parse_input","lines":"L18","code":"    *p = val;","role":"dangerous_sink","note":"SAST-flagged deref"},{"function":"parse_input","lines":"L12-L14","code":"    if (!p)\\n        return -1;","role":"guard","note":"returns when p is NULL"}]}

TP example shape:
{"label":"TP","reason":"Unchecked len from L5 reaches memcpy at L30 with no bounds check on this path.","evidence":[{"function":"read_packet","lines":"L5","code":"    size_t len = buf->len;","role":"dangerous_source","note":"len from external buffer"},{"function":"read_packet","lines":"L22","code":"    n = len;","role":"taint_propagator","note":"n copied into size"},{"function":"read_packet","lines":"L30","code":"    memcpy(dst, src, n);","role":"dangerous_sink","note":"memcpy with n bytes"}]}
"""


# --------------------------------------------------------------------------- #
# Blind variant: bias-prevention instructions for leakage-prone benchmarks
# (e.g. Juliet good/bad naming and FIX/FLAW comments).
# --------------------------------------------------------------------------- #
_IGNORE_NON_SEMANTIC_CUES = """
## Ignore non-semantic cues

Do not use the following as evidence for `TP`, `FP`, or `UNKNOWN`:
- Function, variable, file, path, or type **names** (including tokens such as good, bad, safe, unsafe, sink, source, fix, flaw, G2B, B2G, benign, vulnerable, test, mock, or CWE-prefixed identifiers).
- **Comments** of any kind (including `/* FIX */`, `/* POTENTIAL FLAW */`, `/* OMIT */`, etc.).
- Labels or wording that describe author intent rather than runtime behavior.

Base your decision only on **executable semantics**: control flow, memory operations, bounds/null checks, arithmetic, and data flow visible in the code.

If a name or comment suggests safety or vulnerability but the code behavior shows the opposite, follow the **code**, not the name or comment.

Do not cite naming patterns or comments in `reason` or in evidence `note` fields as justification. Evidence must point to statements that would still support the label if all identifiers were renamed and all comments were removed.

## Mandatory re-check before answering

Before you choose `label`, perform this check:

1. If you noticed any good/bad/safe/unsafe naming (for example goodG2B, badSink, G2B, B2G), test-suite wording, benchmark metadata, or comment text (for example FIX, FLAW, POTENTIAL FLAW), treat that observation as **disqualified** for the decision.
2. Do **not** use those cues to choose `TP`, `FP`, or `UNKNOWN`, even if they seem helpful.
3. Re-evaluate from executable statements only: assignments, calls, branches, bounds/null checks, loop limits, buffer sizes, and data flow.
4. Your final `label`, `reason`, and every evidence `note` must still hold if every identifier were renamed to neutral names and every comment were deleted.

If the label depends on a name, comment, or test metadata, change the label or mark `UNKNOWN` rather than keeping a name-driven conclusion.
"""

_CONTEXT_ROLE_OLD = (
    "- `context`: Neutral supporting fact that does not by itself prove TP or FP "
    "(types, signatures, macro names, non-security comments). Use sparingly."
)
_CONTEXT_ROLE_NEW = (
    "- `context`: Neutral supporting fact that does not by itself prove TP or FP "
    "(types, signatures). Never use comments, identifier names, or file/path strings as "
    "`context` evidence for a label decision. Use sparingly."
)

_TP_FP_EXTRA = (
    "- Do not cite function/variable names, file paths, or comments as proof for this label.\n"
)

PROMPT_CONTEXT_BLIND = (
    PROMPT_CONTEXT.replace(
        "7. Assign exactly one `role` per evidence item. Pick the role that best describes that span's job in your path argument.\n\n## How to use the available context",
        "7. Assign exactly one `role` per evidence item. Pick the role that best describes that span's job in your path argument.\n"
        + _IGNORE_NON_SEMANTIC_CUES
        + "\n## How to use the available context",
    )
    .replace(_CONTEXT_ROLE_OLD, _CONTEXT_ROLE_NEW)
    .replace(
        "### When `label` is `TP`\n\nYour `reason` must describe one plausible adverse path from source to sink still open after considering visible guards and mitigations.\n\nRequired evidence pattern:",
        "### When `label` is `TP`\n\nYour `reason` must describe one plausible adverse path from source to sink still open after considering visible guards and mitigations.\n\nRequired evidence pattern:\n"
        + _TP_FP_EXTRA,
    )
    .replace(
        "### When `label` is `FP`\n\nYour `reason` must explain why every plausible adverse path to the sink is blocked or non-security-relevant. Do not choose `FP` because a check exists; show that it blocks the adverse condition on the traced path(s).\n\nRequired evidence pattern:",
        "### When `label` is `FP`\n\nYour `reason` must explain why every plausible adverse path to the sink is blocked or non-security-relevant. Do not choose `FP` because a check exists; show that it blocks the adverse condition on the traced path(s).\n\nRequired evidence pattern:\n"
        + _TP_FP_EXTRA,
    )
)

# Guard against silent drift: every blind transformation must actually apply.
assert _IGNORE_NON_SEMANTIC_CUES in PROMPT_CONTEXT_BLIND, "blind cues block was not inserted"
assert _CONTEXT_ROLE_NEW in PROMPT_CONTEXT_BLIND, "blind context-role rewrite did not apply"
assert PROMPT_CONTEXT_BLIND.count(_TP_FP_EXTRA) == 2, "blind TP/FP guidance did not apply twice"


def strip_c_comments(code: str) -> str:
    """Remove C/C++ comments from *code*, preserving strings and line count.

    `//` line comments and `/* ... */` block comments are removed; comment text
    inside string/char literals is left intact. Newlines are preserved (block
    comments keep their internal newlines) so that the per-line ``Lxx:`` numbers
    the model cites stay aligned with the original source lines.
    """
    out: list[str] = []
    i, n = 0, len(code)
    state = "code"  # code | line | block | string | char
    while i < n:
        c = code[i]
        nxt = code[i + 1] if i + 1 < n else ""
        if state == "code":
            if c == "/" and nxt == "/":
                state, i = "line", i + 2
            elif c == "/" and nxt == "*":
                state, i = "block", i + 2
            elif c == '"':
                out.append(c); state, i = "string", i + 1
            elif c == "'":
                out.append(c); state, i = "char", i + 1
            else:
                out.append(c); i += 1
        elif state == "line":
            if c == "\n":
                out.append(c); state = "code"
            i += 1
        elif state == "block":
            if c == "*" and nxt == "/":
                out.append(" "); state, i = "code", i + 2
            else:
                if c == "\n":
                    out.append("\n")
                i += 1
        elif state == "string":
            out.append(c)
            if c == "\\" and nxt:
                out.append(nxt); i += 2
            else:
                if c == '"':
                    state = "code"
                i += 1
        else:  # char literal
            out.append(c)
            if c == "\\" and nxt:
                out.append(nxt); i += 2
            else:
                if c == "'":
                    state = "code"
                i += 1
    return "".join(out)


def _strip_bodies(bodies: Any) -> Any:
    if not isinstance(bodies, list):
        return bodies
    return [strip_c_comments(b) if isinstance(b, str) else b for b in bodies]


def strip_comments_in_record(record: Mapping[str, Any]) -> dict:
    """Return a shallow copy of *record* with comments removed from all code.

    Strips the enclosing function bodies, every caller body in
    ``callers_by_level``, and any explicit warning code line.
    """
    rec = dict(record)
    rec["enclosing_function_bodies"] = _strip_bodies(rec.get("enclosing_function_bodies"))

    cbl = rec.get("callers_by_level")
    if isinstance(cbl, Mapping):
        rec["callers_by_level"] = {
            level: ({fn: _strip_bodies(bodies) for fn, bodies in funcs.items()}
                    if isinstance(funcs, Mapping) else funcs)
            for level, funcs in cbl.items()
        }

    for key in ("warning_code_line", "code_line", "source_line", "warning_line"):
        if isinstance(rec.get(key), str):
            rec[key] = strip_c_comments(rec[key])
    return rec


@dataclass(frozen=True)
class PromptSections:
    context: str
    input: str

    def to_messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.context},
            {"role": "user", "content": self.input},
        ]

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _require_non_empty(value: Optional[str], field_name: str) -> str:
    if value is None or not value.strip():
        raise ValueError(f"{field_name} is required and cannot be empty")
    return value.strip()


def _format_bug_type(value: Any) -> str:
    """Render bug_type. Our datasets store it as a list; join into a string."""
    if value is None:
        return "unknown"
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value if str(v).strip()]
        return ", ".join(parts) if parts else "unknown"
    text = str(value).strip()
    return text or "unknown"


def extract_warning_code_line(record: Mapping[str, Any]) -> Optional[str]:
    """Return the flagged source line from the record's explicit fields."""
    for key in ("warning_code_line", "code_line", "source_line", "warning_line"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip("\n")
    return None


def build_sast_finding_from_warning_record(record: Mapping[str, Any]) -> str:
    warning_type = _format_bug_type(record.get("bug_type"))
    warning_code_line = extract_warning_code_line(record)
    if warning_code_line is None:
        warning_code_line = "unknown; source line was not provided in the record"
    return "\n".join([
        f"Warning type: {warning_type}",
        f"Warning code line: {warning_code_line}",
    ])


def _add_line_numbers(body: str) -> str:
    lines = body.splitlines()
    width = len(str(len(lines)))
    return "\n".join(f"L{str(n).zfill(width)}: {line}" for n, line in enumerate(lines, start=1))


def build_warning_function_from_record(record: Mapping[str, Any]) -> str:
    bodies = record.get("enclosing_function_bodies")
    if not isinstance(bodies, list) or not bodies:
        raise ValueError("record does not contain non-empty enclosing_function_bodies")
    formatted: list[str] = []
    for i, body in enumerate(bodies, start=1):
        if body is None or not str(body).strip():
            continue
        formatted.append(f"// Enclosing function body {i}\n{_add_line_numbers(str(body).strip())}")
    if not formatted:
        raise ValueError("record contains enclosing_function_bodies, but all bodies are empty")
    return "\n\n".join(formatted)


def _sorted_int_keys(mapping: Mapping[str, Any]) -> list[int]:
    keys: list[int] = []
    for key in mapping.keys():
        try:
            keys.append(int(key))
        except (TypeError, ValueError):
            continue
    return sorted(keys)


def validate_required_context_levels(context_by_level: Mapping[str, Any], *,
                                     required_max_level: int,
                                     context_name: str = "Caller") -> None:
    """Ensure levels 1..required_max_level exist and have at least one non-empty body."""
    if required_max_level <= 0:
        return
    if not isinstance(context_by_level, Mapping) or not context_by_level:
        raise ValueError(
            f"requested {context_name.lower()} levels 1..{required_max_level}, "
            f"but {context_name.lower()}s_by_level is missing or empty")

    missing, empty = [], []
    for lvl in range(1, required_max_level + 1):
        level_obj = context_by_level.get(str(lvl))
        if level_obj is None:
            missing.append(lvl)
            continue
        if not isinstance(level_obj, Mapping) or not level_obj:
            empty.append(lvl)
            continue
        has_body = any(
            (any(str(b).strip() for b in bodies) if isinstance(bodies, list)
             else bodies is not None and str(bodies).strip())
            for bodies in level_obj.values())
        if not has_body:
            empty.append(lvl)

    problems = []
    if missing:
        problems.append(f"missing level(s): {missing}")
    if empty:
        problems.append(f"empty level(s): {empty}")
    if problems:
        raise ValueError(
            f"requested {context_name.lower()} levels 1..{required_max_level}, "
            f"but {context_name.lower()} context is incomplete ({'; '.join(problems)})")


def format_context_by_level(context_by_level: Mapping[str, Any], *,
                            max_level: Optional[int] = None,
                            context_name: str = "Caller") -> str:
    if not isinstance(context_by_level, Mapping) or not context_by_level:
        return ""
    chunks: list[str] = []
    for lvl in _sorted_int_keys(context_by_level):
        if max_level is not None and lvl > max_level:
            continue
        level_obj = context_by_level.get(str(lvl))
        if not isinstance(level_obj, Mapping) or not level_obj:
            continue
        chunks.append(f"## {context_name} level {lvl}")
        for func_identifier, bodies in level_obj.items():
            chunks.append(f"### {context_name} function: {func_identifier}")
            if isinstance(bodies, list):
                non_empty = [str(b).strip() for b in bodies if str(b).strip()]
            elif bodies is None:
                non_empty = []
            else:
                non_empty = [str(bodies).strip()]
            if not non_empty:
                chunks.append("// Body not provided")
                continue
            for i, body in enumerate(non_empty, start=1):
                if len(non_empty) > 1:
                    chunks.append(f"// Body variant {i}")
                chunks.append(_add_line_numbers(body))
    return "\n\n".join(chunks).strip()


def build_input_sections(*, level: int, sast_finding: str,
                         warning_function: Optional[str] = None,
                         callers: Optional[str] = None) -> str:
    if level < 1:
        raise ValueError("level must be >= 1 (the ladder starts at the enclosing function)")
    sast_finding = _require_non_empty(sast_finding, "sast_finding")
    sections = ["### SAST finding", sast_finding]
    warning_function = _require_non_empty(warning_function, "warning_function when level >= 1")
    sections.extend(["", "### Function containing the warning line", warning_function])
    if level >= 2:
        callers = _require_non_empty(callers, "callers when level >= 2")
        sections.extend(["", "### Caller Context", callers])
    return "\n".join(sections)


def construct_sections_from_warning_record(record: Mapping[str, Any], *,
                                           level: int,
                                           blind: bool = False,
                                           context: Optional[str] = None) -> PromptSections:
    """Build the (system, user) prompt sections for one warning at one level.

    When ``blind`` is true (leakage prevention, e.g. for Juliet): the system
    prompt is the bias-prevention variant (``PROMPT_CONTEXT_BLIND``) and all code
    has its comments stripped before the prompt is constructed. An explicit
    ``context`` always overrides the default selection.
    """
    if level < 1:
        raise ValueError("level must be >= 1")
    if context is None:
        context = PROMPT_CONTEXT_BLIND if blind else PROMPT_CONTEXT
    if blind:
        record = strip_comments_in_record(record)
    sast_finding = build_sast_finding_from_warning_record(record)
    warning_function = build_warning_function_from_record(record)

    callers: Optional[str] = None
    if level >= 2:
        caller_max_level = level - 1
        callers_by_level = record.get("callers_by_level", {})
        validate_required_context_levels(callers_by_level,
                                         required_max_level=caller_max_level,
                                         context_name="Caller")
        callers = format_context_by_level(callers_by_level,
                                          max_level=caller_max_level,
                                          context_name="Caller")
        if not callers:
            raise ValueError(
                f"level {level} requires caller context through caller level {caller_max_level}, "
                "but no caller bodies were formatted")

    input_sections = build_input_sections(level=level, sast_finding=sast_finding,
                                           warning_function=warning_function, callers=callers)
    return PromptSections(context=context.strip(), input=input_sections.strip())
