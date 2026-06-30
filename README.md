# ContextLadder

This repository contains the datasets and replication package for the paper:
***ContextLadder: Progressive-Context LLM Triage of Static-Analysis Warnings***.

It ships **thin warning manifests** (warning location + cloning/source info, but
no extracted callers or function bodies) plus the full pipeline code so you can
reproduce the ContextLadder approach end to end:

1. **Clone** each project at the exact commit referenced by a warning.
2. **Build** a tree-sitter call graph for each project.
3. **Extract** the leveled caller context (with function bodies) for each warning.
4. **Run** the progressive-context LLM ladder with the stabilization rule and
   majority voting to triage each warning as `TP` / `FP` / `UNKNOWN`.

The package covers two evaluation settings: **real-world** warnings (cloned from
upstream repositories) and **Juliet** synthetic warnings (sources vendored, no
cloning needed).

---

## Table of contents

- [What the project does](#what-the-project-does)
  - [1. Clone projects](#1-clone-projects)
  - [2. Build call graphs](#2-build-call-graphs)
  - [3. Extract context](#3-extract-context)
  - [4. Run ContextLadder](#4-run-contextladder)
- [The context ladder and stabilization rule](#the-context-ladder-and-stabilization-rule)
- [Blind mode (leakage prevention)](#blind-mode-leakage-prevention)
- [Requirements](#requirements)
- [Setup](#setup)
  - [1. Go to the package root](#1-go-to-the-package-root)
  - [2. Create a virtual environment](#2-create-a-virtual-environment)
    - [Linux/macOS](#linuxmacos)
    - [Windows PowerShell](#windows-powershell)
  - [3. Create the `.env` file](#3-create-the-env-file)
- [Supported providers](#supported-providers)
- [Datasets layout](#datasets-layout)
- [How to run the project](#how-to-run-the-project)
  - [Real-world warnings](#real-world-warnings)
  - [Juliet warnings](#juliet-warnings)
  - [Quick smoke test](#quick-smoke-test)
- [CLI reference](#cli-reference)
- [Outputs](#outputs)

---

## What the project does

The pipeline runs as four ordered stages; each stage reads the previous stage's
output. All input/output locations are defined centrally in
[`src/config.py`](src/config.py). See [`scripts/README.md`](scripts/README.md)
for full per-stage documentation.

```text
            REAL-WORLD                                  JULIET
   data/real_world_warnings.jsonl              data/juliet_warnings.jsonl
              │                                  (sources vendored in
              ▼                                   data/juliet/, no clone)
 [1] clone_projects.py                                    │
        → output/clones/, checkout_manifest.json          │
              │                                            ▼
              ▼                          [2] build_call_graphs.py --dataset juliet
 [2] build_call_graphs.py                       → output/call_graphs/juliet/
        → output/call_graphs/                            │
              │                                          ▼
              ▼                          [3] extract_context.py --dataset juliet
 [3] extract_context.py                         → output/context/juliet_context.jsonl
        → output/context/real_world_context.jsonl        │
              │                                          ▼
              ▼                          [4] run_contextladder.py --dataset juliet
 [4] run_contextladder.py                       → output/runs/<model>/
        → output/runs/<model>/
```

### 1. Clone projects
`python scripts/clone_projects.py` reads `data/real_world_warnings.jsonl`, clones
each project at its `commit_id` (or downloads/extracts release tarballs), and
writes `output/checkout_manifest.json` mapping every warning to its on-disk
repository root and resolved file path. Every git call uses a per-invocation
**safe-directory fix** so it works on foreign-owned filesystems without touching
your global git config. (Juliet needs no cloning — its sources are vendored.)

### 2. Build call graphs
`python scripts/build_call_graphs.py` parses each checked-out project with the
tree-sitter extractor in [`src/extract_call_graph`](src/extract_call_graph) and
pickles one call graph per project. With `--dataset juliet`, it builds one graph
per Juliet testcase folder instead.

### 3. Extract context
`python scripts/extract_context.py` resolves each warning to its enclosing
function in the pickled call graph and extracts the leveled **caller**
neighborhood (with function bodies) up to `--depth`. This regenerates the
context deliberately omitted from the shipped thin dataset, writing
`output/context/real_world_context.jsonl` (or `juliet_context.jsonl`).

### 4. Run ContextLadder
`python scripts/run_contextladder.py` runs the progressive-context LLM ladder
(see below) over the extracted context and writes one representative-report JSON
per warning under `output/runs/<model>/`.

---

## The context ladder and stabilization rule

For each warning, ContextLadder runs `--num-votes` independent **walks**. Each
walk climbs a progressive-context ladder, starting at the enclosing function and
adding caller context one level at a time:

- **level 1** — the SAST finding + the function containing the warning line
  (the ladder always starts here; there is no level 0),
- **level 2** — + caller level 1,
- **level L** — + caller levels 1..(L-1).

At each level the model returns `TP`, `FP`, or `UNKNOWN`. The **stabilization
rule** decides when to stop expanding context:

1. A decisive verdict (`TP`/`FP`) opens a *stabilization window*; the walk
   expands two more levels to confirm it. **Three consecutive agreeing levels =
   stable**, and the walk stops early.
2. If a later level disagrees with the window's verdict, a new window opens from
   that level (the agreement streak restarts).
3. `UNKNOWN` (or a failed/unparseable level) voids the current window and forces
   expansion to the next level.
4. If the levels run out while a decisive verdict is still pending, that verdict
   is treated as stable anyway ("nothing more we can do"). If no decisive verdict
   was ever reached, the walk is unstable.

Across the walks, the per-warning **`final_label`** is the majority of the walk
labels. The package then selects a single **representative walk** and emits only
that walk's per-level responses as the report: among the walks whose label
equals `final_label`, the representative is the one with the **highest
`first_fp_level`** (the deepest level at which it first returned `FP`). Walks
with no `FP` level rank lowest and ties keep the earliest walk, so a majority
that never produced `FP` resolves to the first matching walk.

A warning whose context extraction failed (no enclosing function resolved) is
**not** triaged — it raises an error, is logged, and produces no verdict file
(rather than defaulting to `UNKNOWN`).

---

## Blind mode (leakage prevention)

Synthetic benchmarks like Juliet leak the ground truth through naming
(`good*`/`bad*`, `goodG2B`, `badSink`) and comments (`/* POTENTIAL FLAW */`,
`/* FIX */`). With `--blind`, two things happen:

1. **Comments are physically stripped** from every code body and the flagged
   line before the prompt is built (string/char literals are preserved and line
   counts kept aligned so the `Lxx:` references stay correct).
2. The system prompt switches to the **bias-prevention variant**
   (`adverse_path_roles_blind`), which adds an "ignore non-semantic cues" block
   and a mandatory pre-answer re-check.

Blind mode defaults **on for `--dataset juliet`** and **off for `realworld`**;
override either way with `--blind` / `--no-blind`. Both system prompts are
exported verbatim under [`prompt_templates/`](prompt_templates) for reference.

---

## Requirements

- **Python 3.10+ (3.12 recommended)**
- A virtual environment
- An API key for the LLM provider(s) you want to use

The package includes a pinned `requirements.txt`. Install from it rather than
recreating dependencies manually.

---

## Setup

### 1. Go to the package root

```bash
cd /path/to/replication_package
```

### 2. Create a virtual environment

#### Linux/macOS

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

#### Windows PowerShell

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Create the `.env` file

Copy the example file in the package root to `.env`:

#### Linux/macOS

```bash
cp .env.example .env
```

#### Windows PowerShell

```powershell
Copy-Item .env.example .env
```

Then open `.env` and fill in the key(s) you need:

```env
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
DEEPSEEK_API_KEY=
OPENROUTER_API_KEY=
```

`run_contextladder.py` loads `.env` from the package root automatically.

---

## Supported providers

The code supports these provider names (see
[`src/llm_runner/runner.py`](src/llm_runner/runner.py)):

- `anthropic` — Claude models (e.g. `claude-sonnet-4-6`)
- `openai`
- `deepseek` — e.g. `deepseek-v4-pro`
- `openrouter`

Pass them with `--provider` and `--model`, e.g.
`--provider anthropic --model claude-sonnet-4-6` or
`--provider deepseek --model deepseek-v4-pro`.

---

## Datasets layout

The datasets are documented in detail in [`data/README.md`](data/README.md).
Briefly:

- **`data/real_world_warnings.jsonl`** — 174 real-world warnings (147 verified
  false positives + 27 verified true positives). Each record carries
  `project_url` + `commit_id` so the pipeline can clone the source; `file_path`
  is then relative to the repository root.
- **`data/juliet_warnings.jsonl`** — 918 Juliet synthetic false positives. The
  corresponding source is vendored under `data/juliet/`, and each `file_path`
  (beginning with `testcases/`) resolves relative to `data/juliet/`.

The shipped manifests are **thin**: they contain the warning location, type, and
cloning/source info, but not the callers or function bodies. You regenerate that
context by running stages 1–3 above.

---

## How to run the project

Run all commands from the package root with the virtual environment activated.

### Real-world warnings

```bash
# [1] clone every project referenced by the dataset
python scripts/clone_projects.py

# [2] build a call graph per cloned project
python scripts/build_call_graphs.py

# [3] extract leveled caller context (depth 2 by default)
python scripts/extract_context.py

# [4] run the ladder (3 votes) with Claude
python scripts/run_contextladder.py --provider anthropic --model claude-sonnet-4-6 --num-votes 3
```

### Juliet warnings

Juliet sources are vendored, so skip the clone stage:

```bash
# [2] build one call graph per Juliet testcase folder
python scripts/build_call_graphs.py --dataset juliet

# [3] extract context
python scripts/extract_context.py --dataset juliet

# [4] run the ladder (blind mode is on by default for Juliet)
python scripts/run_contextladder.py --dataset juliet --provider deepseek --model deepseek-v4-pro --num-votes 3
```

### Quick smoke test

```bash
# Validate prompt building without spending any tokens
python scripts/run_contextladder.py --dry-run

# Triage a single warning end to end
python scripts/run_contextladder.py --limit 1
```

---

## CLI reference

Each script supports `--help`. The most important options are summarized below;
see [`scripts/README.md`](scripts/README.md) for the full tables.

### `clone_projects.py`

- `--warnings PATH` — warnings JSONL (default `data/real_world_warnings.jsonl`)
- `--projects KEY [KEY ...]` — restrict to a subset of projects
- `--force-clone`, `--blobless`

### `build_call_graphs.py`

- `--dataset {realworld,juliet}` (default `realworld`)
- `--projects KEY [KEY ...]` — *(realworld)* subset
- `--force` — rebuild even if a pickle exists

### `extract_context.py`

- `--dataset {realworld,juliet}` (default `realworld`)
- `--depth N` — caller neighborhood depth (default `2`; `0` = unlimited)
- `--projects KEY [KEY ...]` — *(realworld)* subset

### `run_contextladder.py`

- `--dataset {realworld,juliet}` (default `realworld`)
- `--provider NAME` (default `anthropic`) and `--model NAME` (default `claude-sonnet-4-6`)
- `--num-votes N` — independent stabilization walks per warning (default `3`)
- `--blind` / `--no-blind` — leakage prevention (default: on for juliet, off for realworld)
- `--max-level-cap N` — cap the deepest ladder level
- `--workers N` — process warnings in parallel
- `--limit N`, `--projects KEY [KEY ...]`, `--force`
- `--tpm N` — rolling input tokens-per-minute budget; `--max-retries N`, `--base-backoff S`
- `--dry-run` — validate prompt building with no API calls

---

## Outputs

Everything is written under `output/` (override with `CONTEXTLADDER_OUTPUT`):

```text
output/
├── clones/                       # [1] checked-out project sources
├── checkout_manifest.json        # [1] warning → repo root / resolved file
├── call_graphs/                  # [2] pickled call graphs (+ build manifests)
│   └── juliet/                   #     per-testcase graphs for Juliet
├── context/                      # [3] per-warning extracted context (JSONL)
│   ├── real_world_context.jsonl
│   └── juliet_context.jsonl
└── runs/<model>/                 # [4] one representative report per warning
    ├── report_<model>_<warning_id>.json
    └── logs/
```

Each `report_<model>_<warning_id>.json` is the slim representative report:

- `warning_id`, `project`, `model`,
- `final_label` — the aggregated majority verdict (`TP` / `FP` / `UNKNOWN`),
- `ground_truth_label` — the dataset's ground-truth label (for evaluation),
- `levels` — the representative walk's per-level records, each with `level`,
  `label`, `response`, and `error`.

The representative walk is the majority-label walk with the highest
`first_fp_level` (see the stabilization rule above). The run log additionally
reports `is_stable` / `majority_label` / stable-walk counts for each warning,
but only the slim report is written to disk.
