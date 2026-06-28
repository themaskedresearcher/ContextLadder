# Pipeline scripts

Each script is a thin, standalone command-line entry point for one stage of the
ContextLadder pipeline. Stages are meant to run in order — every stage reads the
output of the previous one. All input/output locations are defined centrally in
[`src/config.py`](../src/config.py); by default everything generated lands under
`output/` (override with the `CONTEXTLADDER_OUTPUT` environment variable).

Run any script from the package root, e.g.:

```bash
cd replication_package
python scripts/clone_projects.py --help
```

Pipeline order and data flow:

```
                 REAL-WORLD                              JULIET
        data/real_world_warnings.jsonl          data/juliet_warnings.jsonl
                    │                              (sources vendored in
                    ▼                               data/juliet/, no clone)
[1] clone_projects.py                                       │
        ──► output/clones/<project>__<commit>/              │
            output/checkout_manifest.json                   │
                    │                                       │
                    ▼                                       ▼
[2] build_call_graphs.py                    [2] build_call_graphs.py --dataset juliet
        ──► output/call_graphs/                     ──► output/call_graphs/juliet/
            <project>__<commit>.pickle                  <testcase>.pickle
            build_manifest.json                         build_manifest_juliet.json
                    │                                       │
                    ▼                                       ▼
[3] extract_context.py                      [3] extract_context.py --dataset juliet
        ──► output/context/                         ──► output/context/
            real_world_context.jsonl                    juliet_context.jsonl
                    │                                       │
                    ▼                                       ▼
[4] run_contextladder.py                    [4] run_contextladder.py --dataset juliet
        ──► output/runs/<model>/                     ──► output/runs/<model>/
            votes_<model>_<warning_id>.json              votes_<model>_<warning_id>.json
```

---

## 1. `clone_projects.py`

**Purpose.** Fetch the project sources referenced by the warning dataset so that
each warning's `file_path` resolves on disk. Git projects are cloned and checked
out at the exact `commit_id`; projects distributed as release archives (a
tarball `project_url` with no commit) are downloaded and extracted instead.

Because a project can appear at more than one commit, checkouts are keyed by
`(project, commit)` and placed in `output/clones/<project>__<short_commit>/`
(tarball projects, having no commit, use `output/clones/<project>/`).

Every git invocation uses the **safe-directory fix** (`git -c safe.directory=…`)
to avoid "detected dubious ownership" errors on foreign-owned filesystems,
without modifying your global git config.

**Inputs.** A warnings JSONL (default `data/real_world_warnings.jsonl`).

**Outputs.**
- `output/clones/<project>__<commit>/` — one checkout per `(project, commit)`.
- `output/checkout_manifest.json` — maps every warning to its on-disk
  `repo_root` and a `resolved_file_path` (the recorded `file_path` resolved
  against the checkout, since some paths are basenames or rooted at a build
  subdirectory).

**Arguments.**

| Argument | Required | Default | Description |
| --- | --- | --- | --- |
| `--warnings PATH` | no | `data/real_world_warnings.jsonl` | Warnings JSONL to clone the projects for. |
| `--clone-root PATH` | no | `output/clones/` | Directory where checkouts are created. |
| `--force-clone` | no | off | Remove an existing checkout directory before re-fetching. |
| `--blobless` | no | off | `git clone --filter=blob:none` (faster; may miss very old commits). |
| `--projects KEY [KEY …]` | no | all | Restrict to a subset of project keys. |

**Examples.**

```bash
# Clone/checkout every project in the real-world dataset
python scripts/clone_projects.py

# Only a couple of projects, forcing a fresh checkout
python scripts/clone_projects.py --projects leptonica nbd --force-clone
```

Exits non-zero if any checkout fails.

---

## 2. `build_call_graphs.py`

**Purpose.** Build a tree-sitter call graph for each project using the ported
extractor in [`src/extract_call_graph`](../src/extract_call_graph). The resulting
`TS` object (function declarations, signatures, and caller relationships) is
pickled for reuse by later stages, so each project's sources are parsed only once.

Two datasets are supported via `--dataset`:

- **`realworld`** (default) — builds one graph per checked-out `(project, commit)`
  from the checkout manifest. Skips checkouts not marked `ok`.
- **`juliet`** — Juliet sources are vendored under `data/juliet` (no cloning).
  Each testcase folder is self-contained (it ships its own `io.c` /
  `std_testcase.h`), so one graph is built **per testcase folder**.

**Inputs.**
- `realworld`: the checkout manifest from stage 1 (default `output/checkout_manifest.json`).
- `juliet`: the Juliet warnings JSONL (default `data/juliet_warnings.jsonl`).

**Outputs.**
- `realworld`: `output/call_graphs/<project>__<commit>.pickle` +
  `output/call_graphs/build_manifest.json`.
- `juliet`: `output/call_graphs/juliet/<testcase>.pickle` +
  `output/call_graphs/build_manifest_juliet.json`.

Each build manifest maps a graph `key` to its `repo_root`, `pickle` path, build
`status`, and `n_functions` (a sanity metric), so the next stage can locate each
graph.

**Arguments.**

| Argument | Required | Default | Description |
| --- | --- | --- | --- |
| `--dataset {realworld,juliet}` | no | `realworld` | Which dataset to build call graphs for. |
| `--manifest PATH` | no | `output/checkout_manifest.json` | *(realworld)* Checkout manifest from `clone_projects.py`. |
| `--warnings PATH` | no | `data/juliet_warnings.jsonl` | *(juliet)* Juliet warnings JSONL. |
| `--projects KEY [KEY …]` | no | all | *(realworld)* Restrict to a subset of project keys. |
| `--force` | no | off | Rebuild even if a pickle already exists (otherwise existing pickles are skipped). |

**Examples.**

```bash
# Real-world: build call graphs for everything that was cloned
python scripts/build_call_graphs.py

# Real-world: rebuild a single project's graph from scratch
python scripts/build_call_graphs.py --projects leptonica --force

# Juliet: build one graph per testcase folder (432 graphs)
python scripts/build_call_graphs.py --dataset juliet
```

Exits non-zero if any call graph fails to build. For `realworld`, run
`clone_projects.py` first — the script errors out if the checkout manifest is
missing.

---

## 3. `extract_context.py`

**Purpose.** Regenerate the context that the shipped "thin" dataset omits. For
each warning, it resolves the **enclosing function** in the project's pickled
call graph and, up to `--depth` levels, extracts the leveled **caller**
neighborhood **with function bodies**. (This fuses the two original pipeline
stages — level enumeration and body attachment — into one pass.)

**Inputs.** The dataset's warnings JSONL plus the matching build manifest from
stage 2:
- `realworld`: `data/real_world_warnings.jsonl` + `output/call_graphs/build_manifest.json`.
- `juliet`: `data/juliet_warnings.jsonl` + `output/call_graphs/build_manifest_juliet.json`.

**Outputs.** One JSONL row per warning, preserving the original warning fields
and adding:
- `resolve_status` — how the enclosing function was matched (e.g. `line_name_match`),
  or `no_call_graph` / `*_not_resolved` on failure.
- `enclosing_function_identifier` and `enclosing_function_bodies`.
- `callers_by_level` — `{ "<level>": { "<function_identifier>": ["<body>"] } }`.

Written to `output/context/real_world_context.jsonl` or
`output/context/juliet_context.jsonl`. A function identifier is
`<source-relative-path>::<signature_key>`.

**Arguments.**

| Argument | Required | Default | Description |
| --- | --- | --- | --- |
| `--dataset {realworld,juliet}` | no | `realworld` | Which dataset to extract context for. |
| `--depth N` | no | `2` | Caller neighborhood depth (`0` = unlimited, until fixpoint). |
| `--projects KEY [KEY …]` | no | all | *(realworld)* Restrict to a subset of project keys. |

**Examples.**

```bash
# Real-world context at the default depth of 2
python scripts/extract_context.py

# Juliet context
python scripts/extract_context.py --dataset juliet

# Deeper neighborhood for one project
python scripts/extract_context.py --depth 3 --projects leptonica
```

Run `build_call_graphs.py` (for the same dataset) first — the script errors out
if the build manifest is missing.

---

## 4. `run_contextladder.py`

**Purpose.** Run the LLM triage stage — the ContextLadder itself. For each
warning it performs `--num-votes` independent **stabilization walks**. Each walk
climbs the progressive-context ladder:

- **level 1** — the SAST finding + the enclosing function (the ladder always
  starts here; there is no level 0),
- **level 2** — + caller level 1,
- **level L** — + caller levels 1..(L-1).

At each level the model returns `TP`, `FP`, or `UNKNOWN`. The **stabilization
rule** (implemented in [`src/ladder.py`](../src/ladder.py)) decides when to stop
expanding:

1. A decisive verdict (`TP`/`FP`) opens a *stabilization window*; the walk
   expands two more levels to confirm it. Three consecutive agreeing levels =
   **stable**, and the walk stops early.
2. If a later level disagrees with the current window's verdict, a new window
   opens from that level (the streak restarts).
3. `UNKNOWN` (or a failed/unparseable level) voids the current window and forces
   expansion to the next level.
4. If the levels run out while a decisive verdict is still pending, that verdict
   is treated as stable anyway ("nothing we can do"). If no decisive verdict was
   ever reached, the walk is unstable.

Across the `--num-votes` walks, the per-warning `label` is the majority of the
walk labels; `is_stable` is true when a strict majority of walks are stable and
agree with it. When `is_stable` is false, fall back to `majority_label` (majority
of decisive verdicts across every level of every walk).

The prompt is the **adverse-path + evidence-roles** variant. The exact system
prompt is also exported to
[`prompt_templates/adverse_path_roles_system_prompt.txt`](../prompt_templates/adverse_path_roles_system_prompt.txt).

**Blind mode (leakage prevention).** Synthetic benchmarks like Juliet leak the
ground truth through naming (`good*`/`bad*`, `goodG2B`, `badSink`) and comments
(`/* POTENTIAL FLAW */`, `/* FIX */`). With `--blind`, two things happen:

1. **Comments are physically stripped** from every code body (enclosing function
   and all caller bodies) and from the flagged code line before the prompt is
   built. String/char literals are preserved and line counts are kept aligned so
   the `Lxx:` line references stay correct.
2. The system prompt switches to the **bias-prevention variant**
   (`adverse_path_roles_blind`), which adds an "ignore non-semantic cues" block
   and a mandatory pre-answer re-check, exported to
   [`prompt_templates/adverse_path_roles_blind_system_prompt.txt`](../prompt_templates/adverse_path_roles_blind_system_prompt.txt).

Blind mode defaults **on for `--dataset juliet`** and **off for `realworld`**;
override either way with `--blind` / `--no-blind`. The output records the
`prompt_variant` (`adverse_path_roles` vs `adverse_path_roles_blind`) and a
`blind` flag.

**Inputs.** The context JSONL from stage 3
(`output/context/real_world_context.jsonl` or `output/context/juliet_context.jsonl`)
and an API key for the chosen provider (copy `.env.example` to `.env`).

**Outputs.** One JSON file per warning under `output/runs/<model>/`:
`votes_<model>_<warning_id>.json`, containing the aggregated `label`,
`is_stable`, `majority_label`, the per-walk `walk_label` / `stop_reason`, and the
full per-level model responses. Logs go to `output/runs/<model>/logs/`. Existing
output files are skipped on re-run unless `--force` is given.

Warnings whose context extraction failed (no enclosing-function body) **cannot
be triaged**: they raise `ContextExtractionError`, are logged as `[no-context]`,
counted in the run summary's `no_context`, and produce **no** verdict file (no
default `UNKNOWN` is recorded). Re-run after fixing/extending stage 3 to triage
them.

**Arguments.**

| Argument | Required | Default | Description |
| --- | --- | --- | --- |
| `--dataset {realworld,juliet}` | no | `realworld` | Which context JSONL to triage. |
| `--provider NAME` | no | `anthropic` | `anthropic` \| `openai` \| `deepseek` \| `openrouter`. |
| `--model NAME` | no | `claude-sonnet-4-6` | Model id (Claude models require `--provider anthropic`). |
| `--num-votes N` | no | `3` | Independent stabilization walks per warning. |
| `--max-level-cap N` | no | all | Cap the deepest ladder level. |
| `--blind` / `--no-blind` | no | on for juliet, off for realworld | Leakage prevention: strip code comments + use the bias-prevention prompt. |
| `--workers N` | no | `1` | Process N warnings in parallel (calls within a warning stay sequential). |
| `--projects KEY […]` | no | all | *(realworld)* Restrict to a subset of project keys. |
| `--limit N` | no | all | Process at most N warnings. |
| `--force` | no | off | Re-run warnings even if an output file exists. |
| `--tpm N` | no | `0` | Rolling input tokens-per-minute budget (`0` disables throttling). |
| `--max-retries N` | no | `5` | Rate-limit retries per request. |
| `--base-backoff S` | no | `2.0` | Base seconds for exponential rate-limit backoff. |
| `--dry-run` | no | off | Validate prompt building (level 1 + deepest level) without any API calls or output files. |

**Examples.**

```bash
# Validate context/prompts without spending tokens
python scripts/run_contextladder.py --dry-run

# Real-world, default model, 3 votes
python scripts/run_contextladder.py --provider anthropic --model claude-sonnet-4-6

# Juliet with 4 parallel workers and a TPM budget (blind mode is on by default)
python scripts/run_contextladder.py --dataset juliet --workers 4 --tpm 200000

# Force blind mode off for Juliet (e.g. an ablation that keeps comments/names)
python scripts/run_contextladder.py --dataset juliet --no-blind

# A quick single-warning smoke test
python scripts/run_contextladder.py --limit 1
```

Run `extract_context.py` (for the same dataset) first — the script errors out if
the context JSONL is missing.
