# ContextSniper-Codex

This adapter runs the **plain ContextSniper** search/edit baseline with Codex,
then validates the patch against the prepared benchmark contract. It is
self-contained inside the ContextSniper repository: it does not import or
require a sibling Debugging-Framework checkout.

One invocation performs one Codex repair attempt. It does not add
Debugging-Framework retrieval, CodeGraph, Luna handoffs, retry prompts, or
multi-attempt repair behavior.

## Baseline fidelity

The original Claude SWE runner and this Codex runner both source
[`scripts/SWE/contextsniper_plain_defaults.sh`](../scripts/SWE/contextsniper_plain_defaults.sh).
That shared preset is the source of truth for all plain ContextSniper search,
indexing, embedding, weighted-RRF, policy, lifecycle, and filtering settings.
Host-, workspace-, session-, and port-specific values remain isolated per run.

In particular:

- the existing ContextSniper backend, workspace sync, prompt compose hook,
  `search_code`, and `edit_file` implementations are reused;
- native read/bash filtering remains disabled, so this is not the
  `ContextSniper-FILTER` variant;
- the existing ContextSniper code policy is appended to the task;
- Codex auto-approves only the local ContextSniper `search_code` and
  `edit_file` MCP tools; a run is marked `invalid` if either required tool is
  blocked and Codex falls back to native editing;
- the default embedding model is OpenRouter's
  `openai/text-embedding-3-small` through
  `https://openrouter.ai/api/v1`;
- the repair agent is always Codex CLI;
- the default repair model remains `gpt-5.6-sol` with reasoning effort `low`;
- selecting `deepseek/deepseek-v4-flash-0731` keeps Codex CLI as the agent,
  routes model inference through OpenRouter's Responses API, and defaults to
  low reasoning effort for cost-efficient APR runs.

The resolved non-secret preset is recorded in every `run.json`, making an
experiment auditable. Environment overrides still behave like the original
runner because both runners source the same shell preset.

## Ground-truth isolation

The repair agent receives a copied per-run workspace, not the prepared input
directory itself. Evaluation isolation is enforced separately from the plain
ContextSniper preset, so it does not change retrieval, ranking, or editing
behavior:

- Codex shell/file tools can read the minimal system runtime and the copied
  workspace only;
- the ContextSniper MCP server rejects `search_code` or `edit_file` paths that
  resolve outside `CONTEXTSNIPER_WORKSPACE_ROOT`;
- the task prompt forbids inspecting benchmark metadata, reference patches,
  parent directories, and other project copies;
- general web research remains allowed, but a bundled Codex `PreToolUse` hook
  blocks shell/network requests containing the current case/CVE ID, failing
  bug or test ID/name, or benchmark commit hash before they execute;
- `events.jsonl` is audited after the repair. Relative parent escapes and
  access to configured benchmark/prepared-input roots outside the workspace
  without confirmed pre-tool denial make the run `invalid` with
  `validation_error=groundtruth_access_attempt_detected`; an attempted exact
  online benchmark lookup makes it `invalid` with
  `validation_error=benchmark_answer_lookup_attempt_detected`.

System executables such as `/usr/bin/python3` and unrelated temporary paths are
not classified as ground truth merely because they are absolute paths. Shell
heredoc bodies are parsed separately so language operators such as Python's
`//` and quoted grep patterns do not become false path violations. A denied
typo that points to a nonexistent path is recorded by the hook but does not
invalidate the run because no external benchmark object was reached.

The isolation state is recorded in `run.json` as `groundtruth_isolation` and
the per-run audit is recorded in `result.json` as
`groundtruth_access_audit`. Local `ContextSniper search_code` and local shell
searches may still use the test ID; only external/network lookup with a
benchmark identifier is prohibited. These controls cannot prove that a model
did not memorize a public bug or CVE before the run.

## Validation and metrics

`evaluator/` contains the minimal validation snapshot needed by this adapter:
project-contract loading, recoverable disposable-Git handling, canonical diff
extraction, OCI/Docker execution, target-first validation followed by the full
regression suite for every buildable candidate, APR classification, and Codex
event token aggregation. Its exact upstream snapshot and file mapping are documented in
[`evaluator/UPSTREAM.md`](evaluator/UPSTREAM.md).

`result.json` reports:

- Codex token usage aggregated from `turn.completed` events;
- separate ContextSniper/embedding usage under `contextsniper_usage`;
- `plausible`, `cleanfix`, `noisefix`, `nonefix`, `negfix`, or `invalid`;
- initial, post-validation, fixed, and regression test IDs;
- the baseline and patched validation evidence.
- the fail-closed local ground-truth access audit.

Embedding usage is not mixed into the Codex token count.

## Prerequisites

From a fresh ContextSniper clone:

```bash
./bootstrap.sh
cp .env.example .env
# Edit .env and set OPENROUTER_API_KEY="sk-or-v1-..."
source setup_env.sh
```

`setup_env.sh` maps `OPENROUTER_API_KEY` to ContextSniper's internal
`CONTEXTSNIPER_EMBEDDING_API_KEY` variable and automatically loads the ignored
repository-root `.env`. The secret is neither committed nor written to
`run.json`/`result.json`. Share `.env.example`, not a populated `.env`.
`EMBEDDING_PROVIDER` remains `openai`
because ContextSniper uses an OpenAI-compatible client for the OpenRouter
endpoint; this does not require an OpenAI API key.

Also install and authenticate Codex CLI. The PHP image named in each prepared
contract must be available to its configured OCI runtime (normally Docker).
On macOS, install Go 1.22+ before `./bootstrap.sh` if needed so AGFS can be
built.

The runner code has no external repository dependency. The prepared dataset is
an experiment input and defaults to `../defects4c`; pass `--dataset-root` when
it lives elsewhere. Its expected PHP layout is:

```text
<dataset-root>/out_tmp_dirs/debugging_framework/php/inputs/
  <case-id>/
  <case-id>.debugging-framework.json
  <case-id>.failure.log
```

For another prepared dataset or project family, point directly at its input
folder. Both the flat three-file contract above and the SWE bundle layout
`<inputs>/<case-id>/{config.json,failure.log,<case-id>/}` are accepted:

```bash
./ContextSniper-Codex/run_defects4c_php.sh <case-id> \
  --inputs-dir /path/to/prepared/inputs
```

For this workspace's prepared SWE-fmt cases:

```bash
./ContextSniper-Codex/run_defects4c_php.sh fmtlib__fmt-1683 \
  --inputs-dir ../swe-bench-multilingual-tasks/debugging/out/fmtlib \
  --output-root ContextSniper-Codex/output_logs/swe-fmt
```

## Dry run

This resolves the prepared inputs and the full shared ContextSniper preset
without starting ContextSniper, Docker, or Codex:

```bash
./ContextSniper-Codex/run_defects4c_php.sh \
  CVE-2016-3132__28a6ed9f9a36 --dry-run
```

For a dataset elsewhere:

```bash
./ContextSniper-Codex/run_defects4c_php.sh \
  CVE-2016-3132__28a6ed9f9a36 \
  --dataset-root /path/to/defects4c
```

## Run one PHP case

```bash
./ContextSniper-Codex/run_defects4c_php.sh \
  CVE-2016-3132__28a6ed9f9a36
```

Defaults:

```text
agent              codex
model provider     openai
model              gpt-5.6-sol
reasoning effort   low
attempts           1
Codex timeout      1800 seconds
validation timeout 1800 seconds
```

## Select the repair agent and model

The original invocation is unchanged. With no overrides it uses Codex CLI with
the built-in OpenAI provider, `gpt-5.6-sol`, and reasoning effort `low`:

```bash
./ContextSniper-Codex/run_defects4c_php.sh \
  CVE-2016-3132__28a6ed9f9a36
```

To keep Codex CLI as the repair agent while using DeepSeek V4 Flash 0731
through OpenRouter:

```bash
export OPENROUTER_API_KEY="sk-or-v1-..."

./ContextSniper-Codex/run_defects4c_php.sh \
  CVE-2016-3132__28a6ed9f9a36 \
  --agent codex \
  --model deepseek/deepseek-v4-flash-0731 \
  --output-root ContextSniper-Codex/output_logs/deepseek-v4-flash-0731
```

Because `codex` is the default agent, `--agent codex` can be omitted. Without
`--model`, the runner selects `gpt-5.6-sol`. Selecting
`deepseek/deepseek-v4-flash-0731` automatically selects the OpenRouter provider
and uses reasoning effort `low`. Pass `--reasoning-effort high` or
`--reasoning-effort max` for a more expensive quality-oriented APR run.

`--model-provider openai|openrouter` is also available when the provider cannot
be inferred from a custom model name. Run metadata records `agent: codex`
separately from the selected provider and model. It also records the shared
`agent_harness_profile: contextsniper-codex-parity-v1`; compare model results
only when this value and the ContextSniper preset match.

The OpenRouter provider is supplied directly to Codex CLI on each invocation,
so this mode does not depend on a machine-local `~/.codex/config.toml`. The API
key is read only from `OPENROUTER_API_KEY`; run metadata records the agent,
provider, model, and effective reasoning setting, but never the key. The
bundled `models/deepseek-v4-flash-0731.json` catalog tells Codex the model's
context window, reasoning levels, text modality, and direct tool mode, avoiding
Codex's unknown-model fallback metadata. GPT and DeepSeek use the same
`contextsniper-codex-parity-v1` agent harness: the same task prompt,
ContextSniper policy, repair MCP tools, Codex feature flags, sandbox, lookup
guard, and validation. There are no DeepSeek-only task prompt suffixes or filesystem
rules. The DeepSeek catalog adds tool-protocol instructions: under approval policy
`never`, omit `justification`, `sandbox_permissions`, and `prefix_rule` from shell
calls, and do not treat local paths as MCP resource URIs. This avoids
justification/escalation retry loops without granting extra permissions or
changing ContextSniper settings. This model-specific base instruction differs
from GPT's built-in instructions; the setup preserves ContextSniper parity, not
byte-identical model system prompts. No benchmark-specific hints are added.
Hosted Codex web search is disabled for this OpenRouter route because
the benchmark harness keeps the OpenRouter tool surface fixed; general research
through ordinary network shell commands remains allowed and is still subject
to the benchmark-identifier guard. If a model repeatedly
emits unsupported tool names, invalid MCP servers, or forbidden approval
arguments, the runner fails fast instead of waiting for the full Codex timeout.
Progress messages and a
30-second heartbeat are written to stderr during long runs; the final JSON on
stdout remains machine-readable.

The output directory is `ContextSniper-Codex/output_logs/<run-id>/`:

| File | Purpose |
| --- | --- |
| `run.json` | Resolved non-secret run configuration, ContextSniper preset, and input hashes |
| `prompt.txt` | Exact prompt passed to Codex |
| `events.jsonl` | Codex events used for token aggregation |
| `response.txt` | Last Codex message |
| `patch.diff` | Canonical workspace patch |
| `validation/` | Baseline and patched validation evidence |
| `result.json` | Final APR status, tokens, and test-set comparison |
| `logs/` | Codex, ContextSniper, and AGFS logs |

`logs/benchmark-lookup-guard.jsonl` records requests blocked by the pre-tool
guard. Under audit policy `blocked-attempts-allowed-v1`, confirmed pre-tool
denials are recorded in `groundtruth_access_audit.blocked_attempts` and do not
prevent patch validation. This applies equally to GPT and DeepSeek, including
legacy denial logs. A separate execution event is still audited independently:
a denied request never excuses a later executed request. Malformed guard logs,
guard configuration errors, and unverified access remain invalid. Prompts,
ContextSniper settings, sandbox permissions, and blocking rules are unchanged.

`result.json` is authoritative for the original execution. If a later validator
run creates `revalidated-result.json`, use that file as the updated
classification while retaining `result.json` as the immutable original. Exit
code is `0` only for `plausible`, `1` for a completed non-plausible
classification, and `2` for preflight/execution errors.

## Optional OpenHands harness

Codex remains the default. `--agent openhands` uses OpenHands SDK 1.49.5 in
its own Python environment; cloning the OpenHands repository is unnecessary.
Install or reproduce that environment with Python 3.12+ (from `ContextSniper/`):

```bash
bash ContextSniper-Codex/setup_openhands.sh
# Or pass a Python 3.12+ executable as the first argument.
```

Run a prepared PHP case with the same default GPT model and reasoning effort:

```bash
./ContextSniper-Codex/run_defects4c_php.sh CVE-2016-3132__28a6ed9f9a36 \
  --agent openhands \
  --contextsniper-python "$PWD/.venv/bin/python"
```

The default authentication is `--openhands-auth subscription`, using ChatGPT
subscription login without an OpenAI API key. Sign in once with the same account
you use for Codex before running the command above:

```bash
ContextSniper-Codex/.venv-openhands/bin/python -c \
  'from openhands.sdk import LLM; LLM.subscription_login(vendor="openai", model="gpt-5.6-sol")'
./ContextSniper-Codex/run_defects4c_php.sh CVE-2016-3132__28a6ed9f9a36 \
  --agent openhands --openhands-auth subscription \
  --contextsniper-python "$PWD/.venv/bin/python"
```

Complete subscription login before launching a batch. OpenHands manages its
own authentication; selecting this harness does not automatically reuse Codex
CLI authentication. Live account/model availability must be verified with your
account. Integration tests use a simulated LLM and consume no model credits.
API billing is opt-in with `--openhands-auth api-key` and `OPENAI_API_KEY` for GPT
(or `OPENROUTER_API_KEY` for the OpenRouter provider).

The shared task prompt (`prompt.txt`), 44 ContextSniper settings, MCP search/edit
server and environment allowlist, benchmark guards, copied workspace, patch
extraction, and validator are reused. The shared prompt retains its historical
Codex host-mapping text. OpenHands supplies its own native system prompt,
tool schemas, conversation management, and iteration limit (SDK default 500);
those are harness differences, not identical Codex internals.

The OpenHands shell runs in the prepared case's existing Docker image with only
the copied workspace mounted. It requires image-mode inputs and a local image.
The container has a read-only root filesystem and no host credentials, home
directory or Docker socket mounted. Shell requests pass the shared guard before
execution. Only `search_code`, `edit_file`, the guarded workspace shell, and SDK
finish/think tools are enabled. Ambient SDK plugin/skill and vision-profile
discovery is disabled through an adapter tied to the pinned SDK version.

`--agent-timeout` aliases the existing `--codex-timeout` (default 1800 seconds).
`--openhands-python` overrides the isolated worker interpreter. Common output
files and existing `codex_*` execution fields remain for evaluator compatibility;
`agent_*` fields and `agent_harness_profile` identify the actual harness. Raw SDK
events, state, and successful-run usage/cost metrics are under `logs/openhands-*`.
An interrupted run may have incomplete usage metrics.

Run the SDK/MCP integration tests without API calls:

```bash
ContextSniper-Codex/.venv-openhands/bin/python -m unittest discover \
  -s ContextSniper-Codex/tests -p test_openhands_adapter.py
# Include real Docker shell isolation and timeout checks:
CONTEXTSNIPER_TEST_DOCKER_IMAGE=php-src/defect4c:latest \
  ContextSniper-Codex/.venv-openhands/bin/python -m unittest discover \
  -s ContextSniper-Codex/tests -p test_openhands_adapter.py
```

## Revalidate saved patches

After a validator update, saved patches can be validated again without calling
Codex, ContextSniper, or the embedding service:

```bash
.venv/bin/python ContextSniper-Codex/revalidate_results.py \
  --stale-in ContextSniper-Codex/output_logs
```

This selects old runs where the target failed but regression was not run, plus
runs invalidated by an older ground-truth/lookup audit. It preserves the
original `result.json` and writes the updated classification to
`revalidated-result.json`, with fresh evidence under
`validation/revalidated-v2/`. Use `--force` to replace an earlier revalidation
of the same saved patch.

## Test the adapter

The tests need only Python 3.11+ and repository files:

```bash
python -m unittest discover -s ContextSniper-Codex/tests -v
```
