# ContextLadder Dataset

This folder contains the warning datasets used in the ContextLadder study, together
with the source needed to reconstruct each warning's surrounding code context.

The datasets cover two evaluation settings:

- **Real-world warnings** (`real_world_warnings.jsonl`) — 174 warnings from 51
  open-source C/C++ projects: 147 verified false positives (FP) and 27 verified true
  positives (TP).
- **Juliet warnings** (`juliet_warnings.jsonl`) — 918 synthetic false positives
  from the [NIST Juliet C/C++ Test Suite](https://samate.nist.gov/SARD/test-suites),
  with the corresponding test-case source vendored under `juliet/`.

Each line of a `.jsonl` file is one self-contained warning object (one JSON document
per line).

## Contents

```text
data/
  README.md
  real_world_warnings.jsonl    # 174 real-world warnings (147 FP + 27 TP)
  juliet_warnings.jsonl        # 918 Juliet synthetic warnings (all FP)
  juliet/
    testcases/                 # vendored Juliet source for the 918 warnings
      <CWE>/<sNN>/<testcase>/...
```

## How a warning locates its source code

- **Real-world warnings** do not ship source code. Each record carries `project_url`
  and `commit_id`; clone the repository at that commit, and `file_path` is then
  relative to the repository root.
- **Juliet warnings** ship their source in this package. `file_path` (which begins
  with `testcases/`) is relative to the `juliet/` folder, e.g.
  `juliet/<file_path>`.

In both cases `line_number` is the 1-based line of the warned statement within
`file_path`, and `enclosing_function` is the function that contains it.

---

## `real_world_warnings.jsonl`

174 warnings: 147 FP (42 projects) + 27 TP (9 projects). The false positives come from
RevBugBench and PrimeVul; the true positives come from LLM4SA. Lines 1–147 are FP,
lines 148–174 are TP.

| Key | Type | Present | Description |
| --- | --- | --- | --- |
| `warning_id` | string | always | Unique warning identifier, formatted `"<project>:<NNNN>"` (e.g. `"wireshark:1000"`). |
| `project` | string | always | Short project name (e.g. `"FFmpeg"`, `"RIOT"`). |
| `project_url` | string | always | Git URL of the project to clone. |
| `commit_id` | string | always | Commit hash to check out before resolving `file_path`. |
| `file_path` | string | always | Path to the file containing the warning, relative to the cloned repository root. |
| `line_number` | integer | always | 1-based line of the warned statement. For false positives this is the human/agent-verified sink line (see note below). |
| `code_line` | string | always | The exact source text at `line_number`. |
| `enclosing_function` | string \| null | always | Name of the function that contains the warning line (may be `null` for some TP records where it was not recorded). |
| `bug_type` | list of strings | always | Warning category/CWE descriptors (e.g. `["CWE-119: Improper Restriction of Operations within the Bounds of a Memory Buffer"]`, or a crash class such as `["SEGV"]`). |
| `fuzz_result` | string | **FP only** | Outcome of fuzzing-based triage for false positives: `"FP"` (confirmed not reproducible, 135) or `"NR"` (not reproduced, 12). Omitted on TP records, which were manually verified rather than fuzzed. |
| `label` | string | always | Ground-truth label: `"FP"` (147) or `"TP"` (27). |

**Note on corrected sink lines (false positives).** Some FP warnings were originally
reported by the SAST tool on a non-semantic line (e.g. a function signature or closing
brace). For those, `line_number` / `file_path` / `code_line` / `enclosing_function`
hold the human-verified corrected sink location, so the warning points at the line
that actually matters for triage.

Example (false positive):

```json
{"warning_id": "tcpdump:1000", "project": "tcpdump",
 "project_url": "https://github.com/the-tcpdump-group/tcpdump",
 "commit_id": "9f0730bee3eb65d07b49fd468bc2f269173352fe",
 "file_path": "util-print.c", "line_number": 543,
 "code_line": "                    if (space_left <= 1)",
 "enclosing_function": "bittok2str_internal",
 "bug_type": ["CWE-119: Improper Restriction of Operations within the Bounds of a Memory Buffer"],
 "fuzz_result": "FP", "label": "FP"}
```

Example (true positive — note no `fuzz_result`):

```json
{"warning_id": "RIOT:0001", "project": "RIOT",
 "project_url": "https://github.com/RIOT-OS/RIOT",
 "commit_id": "f466fce960a1cffdcdaedfc69e3301d262546c7d",
 "file_path": "pkg/wakaama/contrib/lwm2m_client_connection.c", "line_number": 303,
 "code_line": "    _port = strrchr(pos, ':');",
 "enclosing_function": "_parse_host_and_port",
 "bug_type": ["Null Dereference"], "label": "TP"}
```

---

## `juliet_warnings.jsonl`

918 synthetic false positives drawn from the Juliet C/C++ Test Suite. All entries are
the "good" (non-vulnerable) sink variants, so every warning is a false positive.

| Key | Type | Description |
| --- | --- | --- |
| `warning_id` | string | Unique warning identifier, formatted `"juliet:<NNNN>"`. |
| `project` | string | Always `"juliet"`. |
| `file_path` | string | Path to the file containing the warning, relative to the `juliet/` folder (begins with `testcases/`). |
| `line_number` | integer | 1-based line of the warned statement. |
| `code_line` | string | The exact source text at `line_number`. |
| `enclosing_function` | string | Name of the function that contains the warning line (the Juliet sink function). |
| `bug_type` | list of strings | Two descriptors: the Flawfinder finding category and the Juliet CWE folder, e.g. `["buffer/char", "CWE126_Buffer_Overread"]`. |
| `SinkType` | string | Juliet sink variant: `"goodG2BSink"` (648) or `"goodB2GSink"` (270). Both are benign-by-construction, hence false positives. |
| `label` | string | Ground-truth label; always `"FP"`. |

Example:

```json
{"warning_id": "juliet:0001", "project": "juliet",
 "file_path": "testcases/CWE126_Buffer_Overread/s01/CWE126_Buffer_Overread__char_alloca_loop_51/CWE126_Buffer_Overread__char_alloca_loop_51b.c",
 "line_number": 53, "code_line": "        char dest[100];",
 "enclosing_function": "CWE126_Buffer_Overread__char_alloca_loop_51b_goodG2BSink",
 "bug_type": ["buffer/char", "CWE126_Buffer_Overread"],
 "SinkType": "goodG2BSink", "label": "FP"}
```

### `juliet/`

Vendored Juliet source for the 918 warnings: 432 per-test-case folders (one per
distinct warning location), preserving the original
`testcases/<CWE>/<sNN>/<testcase>/` layout. Each folder includes its test-case files
plus the shared support files (`io.c`, `std_testcase.h`, ...) required to compile and
to reconstruct the call graph. Every Juliet warning's `file_path` resolves to a file
under this directory.

---

## Field consistency across datasets

The two datasets share field names and meanings wherever the information is the same
(`warning_id`, `project`, `file_path`, `line_number`, `code_line`,
`enclosing_function`, `bug_type` as a list, `label`). Differences reflect genuine
differences between the settings:

| Field | Real-world | Juliet |
| --- | --- | --- |
| `project_url`, `commit_id` | present (clone at runtime) | absent (source vendored) |
| `fuzz_result` | present on FP only | absent |
| `SinkType` | absent | present |
| `label` values | `FP` and `TP` | `FP` only |
