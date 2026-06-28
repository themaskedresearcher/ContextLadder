# Source modules

Reusable library code for the ContextLadder pipeline. The command-line entry
points in [`../scripts`](../scripts) are thin wrappers around these modules.
Everything here is importable as `src.<module>` from the package root.

```
src/
├── config.py             # central paths / output layout for every stage
├── repo_clone.py         # clone + checkout project sources (safe-directory fix)
├── context_extraction.py # resolve warnings + extract leveled callers + bodies
├── ladder.py             # stabilization state machine + N-vote walks + aggregation
├── llm_runner/           # minimal multi-provider LLM client
│   └── runner.py         # setup_client / send_prompt (OpenAI + Anthropic)
├── prompts/              # prompt builders
│   └── adverse_path_roles.py  # adverse-path + evidence-roles SAST triage prompt
└── extract_call_graph/   # tree-sitter C/C++ call-graph extractor
    ├── AST.py            # Function / DataType data model
    ├── utils.py          # BaseProfile (decl + ancestor store) + preprocessing
    └── TS.py             # TS / UnitTS — the parser and call-graph builder
```

---

## `config.py`

Single source of truth for **where inputs live and where each stage writes its
outputs**, so every step knows where the previous step left its artifacts.

- **Input constants:** `REAL_WORLD_WARNINGS`, `JULIET_WARNINGS`, `JULIET_SRC_DIR`.
- **Output layout:** `OUTPUT_DIR` (override with the `CONTEXTLADDER_OUTPUT` env
  var) and its subdirectories `CLONES_DIR`, `CALL_GRAPHS_DIR` (+ `JULIET_CALL_GRAPHS_DIR`),
  `CONTEXT_DIR`, `RUNS_DIR`, `RESULTS_DIR`, `LOGS_DIR`. Named artifact paths:
  `CHECKOUT_MANIFEST`, `BUILD_MANIFEST` / `JULIET_BUILD_MANIFEST`,
  `REAL_WORLD_CONTEXT` / `JULIET_CONTEXT`.
- **Helpers:** `ensure_output_dirs()`, `checkout_key(project, commit)`,
  `clone_dir(...)`, `call_graph_path(...)`, `juliet_call_graph_path(testcase)`,
  `runs_dir_for_model(model)`, `runs_logs_dir(model)`,
  `warning_output_path(model, warning_id)`.

`checkout_key` delegates to `repo_clone.checkout_dir_name`, so a checkout
directory and its pickled call graph always share the same `<project>__<commit>`
name.

## `repo_clone.py`

Clones/checks out the project sources referenced by the warning dataset and
resolves each warning to an on-disk file. Backs `scripts/clone_projects.py`.

- **Git command helpers:** `git_trust_repo_args` (the **safe-directory fix** —
  `git -c safe.directory=…`, per-invocation, no global config change),
  `run_cmd`, `run_cmd_capture`, `git_head_commit`, `peeled_commit`.
- **Checkout primitives:** `checkout_git_project` (clone + checkout a commit,
  idempotent, asserts HEAD), `download_and_extract_tarball` (release archives),
  `is_tarball_url`, `checkout_dir_name`.
- **Driver:** `clone_dataset(...)` reads the warnings JSONL, fetches each unique
  `(project, commit)`, and writes the checkout manifest;
  `resolve_warning_file(...)` maps a recorded `file_path` to an existing
  repo-relative path (handling basenames / build-subdir paths).

Depends only on the Python standard library.

## `context_extraction.py`

Resolves each warning to its enclosing function in a pickled call graph and
extracts the leveled **caller** neighborhood **with function bodies**. Backs
`scripts/extract_context.py`, and fuses what were two stages in the source repo
(level enumeration + body attachment) into a single pass.

- **Loading:** `load_call_graph(pickle)` (tolerates legacy module names),
  `get_decl_info(obj)`.
- **Resolution:** `find_decl_file_key(...)` maps a warning's `file_path` to a
  graph file key; `resolve_enclosing_node(warning, ...)` pins the enclosing
  function by line number (narrowing by `enclosing_function` name when needed).
- **Graph + traversal:** `build_callers_graph(decl_info)` → caller adjacency
  over concrete function definitions (callee edges are computed as an
  intermediate step and inverted; static symbols are file-scoped);
  `bfs_levels(start, graph, depth)` walks the neighborhood by level.
- **Bodies & identifiers:** `read_body(fn, ...)` slices the function's source by
  line range; `identifier_for(node, id_root)` builds the portable
  `<rel-path>::<signature_key>` identifier.
- **Driver:** `extract_for_unit(warnings, decl_info, source_root, id_root, depth)`
  resolves + extracts context for all warnings sharing one call graph and
  returns the enriched rows plus resolve-status stats.

## `ladder.py`

The ContextLadder triage engine. Backs `scripts/run_contextladder.py`.

- **Verdict parsing:** `extract_output_label(text)` robustly pulls `TP`/`FP`/
  `UNKNOWN` out of a model response (JSON first, regex fallback).
- **Ladder geometry:** `compute_max_level(record, cap)` — the deepest prompt
  level is `1 + (contiguous non-empty caller levels)`; `levels_plan(max_level)`
  → `[1, 2, …, max_level]` (the ladder always starts at level 1 — the enclosing
  function — there is no level 0).
- **Stabilization state machine:** `stabilized_walk(levels, evaluate)` runs one
  walk and applies the rule (decisive verdict opens a confirmation window;
  three consecutive agreeing levels = stable and stop early; disagreement opens
  a new window; `UNKNOWN`/failure voids the window; exhaustion with a pending
  verdict still counts as stable).
- **Per-warning orchestration:** `run_warning(record, evaluate, num_votes, …)`
  runs `num_votes` independent walks and `aggregate_walks(...)` combines them
  into `label` / `is_stable` / `majority_label` (the dataset's ground-truth
  `label` is preserved as `gold_label` to avoid collision). If the record has no
  usable enclosing-function context (failed context extraction) it raises
  `ContextExtractionError` — **no verdict is produced**, rather than defaulting
  to `UNKNOWN`.
- **Model glue:** `make_level_evaluator(record, client, model, blind=…)` builds
  the prompt for a level (blind mode strips comments + uses the bias-prevention
  prompt), queries the model, and returns the per-level record.
- **Reliability:** `RollingTokenThrottle` (rolling input-TPM budget),
  `send_prompt_with_retries` (exponential backoff on rate limits),
  `is_rate_limit_error`, `estimate_input_tokens`.

The stabilization logic is provider-agnostic and unit-testable: `evaluate` is an
injected `level -> {level, label, …}` callable, so the state machine can be
driven by scripted labels without any API calls.

## `llm_runner/`

`runner.py` — a minimal multi-provider client. `setup_client(provider)` returns
an OpenAI-compatible or Anthropic client (providers: `openai`, `deepseek`,
`openrouter` via the OpenAI SDK; `anthropic` via the Anthropic SDK). `send_prompt(
client, context, user_input, model)` sends a `(system, user)` pair at
temperature 0 and returns `(answer_text, reasoning_text, usage)`. API keys are
read from the environment (see [`../.env.example`](../.env.example)).

## `prompts/`

`adverse_path_roles.py` — the prompt used by ContextLadder. `PROMPT_CONTEXT` is
the verbatim system prompt (also exported to
[`../prompt_templates/adverse_path_roles_system_prompt.txt`](../prompt_templates/adverse_path_roles_system_prompt.txt));
`construct_sections_from_warning_record(record, level, blind=False)` builds the
split `(system, user)` sections for a warning at a given level — level 1 adds the
enclosing function, level ≥ 2 adds caller levels 1..(level-1). `bug_type` is
rendered from the dataset's list form.

**Blind mode (`blind=True`)** for leakage-prone benchmarks (Juliet):
- `strip_c_comments(code)` removes C/C++ comments while preserving string/char
  literals and line counts; `strip_comments_in_record(record)` applies it to a
  copy of all code bodies + the flagged line (the caller's record is not
  mutated).
- The system prompt switches to `PROMPT_CONTEXT_BLIND` (exported to
  [`../prompt_templates/adverse_path_roles_blind_system_prompt.txt`](../prompt_templates/adverse_path_roles_blind_system_prompt.txt)),
  which adds the "ignore non-semantic cues" block and a mandatory pre-answer
  re-check. An import-time assertion guards the blind transformations against
  drift from the base prompt.

---

## `extract_call_graph/`

Tree-sitter-based extractor that parses a C/C++ project and builds a call graph
(function declarations with signatures, plus caller/ancestor relationships).
Backs `scripts/build_call_graphs.py`. Public API is re-exported from the
package's `__init__.py`: `TS`, `UnitTS`, `Function`, `DataType`, `BaseProfile`.

### `AST.py` — data model
- **`Function`** — a parsed function/declaration: name, return type, parameter
  types, body, source range, and whether it is a definition vs. an `extern`
  declaration. The node type stored throughout the call graph.
- **`DataType`** — enum of declaration kinds used to classify entries.

### `utils.py` — declaration store & preprocessing
- **`BaseProfile`** — base class holding the parsed program: `decl_info`
  (per-file `{key → Function}`), `ref_file_map`, and `ancestor_map`. Provides
  lookup/resolution helpers (`find_entries_by_name`, `resolve_entry`,
  `get_ancestors`, and crucially `get_enclosing_function(file, line)` used to
  locate the function a warning falls inside).
- **`PreprocessUtils`** — source fix-ups that make imperfect/real-world C parse
  more reliably before extraction.

### `TS.py` — parser & call-graph builder
- **`TS`** — the main entry point: `TS(target_dir)` parses an entire project
  directory (`parse_directory`) and computes ancestors (`get_ancestors`),
  producing the picklable call graph consumed by later stages.
- **`UnitTS`** — single-file parsing/trimming utilities (used to isolate and
  clean an individual translation unit, e.g. around a target line).
