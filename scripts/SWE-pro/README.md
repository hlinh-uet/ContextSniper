# SWE-bench Pro Runners

[中文文档](README_CN.md)

This folder contains six SWE-bench Pro runners:

| Case | Script | ContextSniper search | Filtering |
| --- | --- | --- | --- |
| Claude legacy | `scripts/SWE-pro/claude/legacy/run_swe_task_pro_plain_claude.sh` | No | No |
| Claude ContextSniper | `scripts/SWE-pro/claude/ContextSniper/run_swe_task_pro_contextsniper_plugin.sh` | Yes | No |
| Claude ContextSniper-FILTER | `scripts/SWE-pro/claude/ContextSniper-FILTER/run_swe_task_pro_contextsniper_plugin.sh` | Yes | Yes |
| OpenClaw legacy | `scripts/SWE-pro/openclaw/legacy/run_swe_task_pro_plain_openclaw.sh` | No | No |
| OpenClaw ContextSniper | `scripts/SWE-pro/openclaw/ContextSniper/run_swe_task_pro_openclaw_contextsniper_plugin.sh` | Yes | No |
| OpenClaw ContextSniper-FILTER | `scripts/SWE-pro/openclaw/ContextSniper-FILTER/run_swe_task_pro_openclaw_contextsniper_filter_plugin.sh` | Yes | Yes |

## Instructions

Start from the repository root:

```bash
cd /path/to/ContextSniper
```

Prepare ContextSniper once:

```bash
./bootstrap.sh --install-swe-deps
```

Load your local ContextSniper settings:

```bash
source setup_env.sh
```

Run a task by passing the SWE-bench Pro instance id. If no id is passed, each
runner uses its own default task.

## Claude

Plain Claude:

```bash
./scripts/SWE-pro/claude/legacy/run_swe_task_pro_plain_claude.sh instance_qutebrowser__qutebrowser-f91ace96223cac8161c16dd061907e138fe85111-v059c6fdc75567943479b23ebca7c07b5e9a7f34c
```

Claude with ContextSniper search/edit:

```bash
./scripts/SWE-pro/claude/ContextSniper/run_swe_task_pro_contextsniper_plugin.sh instance_qutebrowser__qutebrowser-f91ace96223cac8161c16dd061907e138fe85111-v059c6fdc75567943479b23ebca7c07b5e9a7f34c
```

Claude with ContextSniper search/edit and read/bash filtering:

```bash
./scripts/SWE-pro/claude/ContextSniper-FILTER/run_swe_task_pro_contextsniper_plugin.sh instance_qutebrowser__qutebrowser-f91ace96223cac8161c16dd061907e138fe85111-v059c6fdc75567943479b23ebca7c07b5e9a7f34c
```

## OpenClaw

Plain OpenClaw:

```bash
source scripts/SWE-pro/openclaw/legacy/setup_swe_env.sh
./scripts/SWE-pro/openclaw/legacy/run_swe_task_pro_plain_openclaw.sh instance_qutebrowser__qutebrowser-f91ace96223cac8161c16dd061907e138fe85111-v059c6fdc75567943479b23ebca7c07b5e9a7f34c
```

OpenClaw with ContextSniper search/edit:

```bash
source scripts/SWE-pro/openclaw/ContextSniper/setup_swe_env.sh
./scripts/SWE-pro/openclaw/ContextSniper/run_swe_task_pro_openclaw_contextsniper_plugin.sh instance_qutebrowser__qutebrowser-f91ace96223cac8161c16dd061907e138fe85111-v059c6fdc75567943479b23ebca7c07b5e9a7f34c
```

OpenClaw with ContextSniper search/edit and read/exec filtering:

```bash
source scripts/SWE-pro/openclaw/ContextSniper-FILTER/setup_swe_env.sh
./scripts/SWE-pro/openclaw/ContextSniper-FILTER/run_swe_task_pro_openclaw_contextsniper_filter_plugin.sh instance_qutebrowser__qutebrowser-f91ace96223cac8161c16dd061907e138fe85111-v059c6fdc75567943479b23ebca7c07b5e9a7f34c
```

## SWE-Pro Docker Environment

By default, SWE-Pro runners derive the task command helper from the official
prebuilt image named by the instance `dockerhub_tag` column:

```text
jefzda/sweap-images:<dockerhub_tag>
```

The helper is written into the generated workspace as
`RUN_IN_SWE_PRO_DOCKER_ENV.sh`. It applies the current workspace patch inside
the official `/app` checkout before running the requested command.

## Quick Debug

For a fast smoke run that avoids pulling the large SWE-Pro Docker image and
skips final validation:

```bash
SWE_USE_DERIVED_LOCAL_ENV=0 SWE_SKIP_VALIDATION=1 \
  ./scripts/SWE-pro/claude/ContextSniper/run_swe_task_pro_contextsniper_plugin.sh instance_qutebrowser__qutebrowser-f91ace96223cac8161c16dd061907e138fe85111-v059c6fdc75567943479b23ebca7c07b5e9a7f34c
```

Use the SWE-Pro Docker validator:

```bash
SWE_VALIDATION_FORCE_LOCAL=0 \
  ./scripts/SWE-pro/claude/ContextSniper/run_swe_task_pro_contextsniper_plugin.sh instance_qutebrowser__qutebrowser-f91ace96223cac8161c16dd061907e138fe85111-v059c6fdc75567943479b23ebca7c07b5e9a7f34c
```

The same environment variables work with the other runners.

## Output

Each runner writes to its own `output_logs` directory:

```text
scripts/SWE-pro/claude/legacy/output_logs/
scripts/SWE-pro/claude/ContextSniper/output_logs/
scripts/SWE-pro/claude/ContextSniper-FILTER/output_logs/
scripts/SWE-pro/openclaw/legacy/output_logs/
scripts/SWE-pro/openclaw/ContextSniper/output_logs/
scripts/SWE-pro/openclaw/ContextSniper-FILTER/output_logs/
```

Every output directory has a `latest` symlink:

```bash
ls -la scripts/SWE-pro/claude/ContextSniper/output_logs/latest
```

Useful files:

| File | Purpose |
| --- | --- |
| `workspace/` | Target repository after the agent edited it |
| `workspace/TASK.md` | Prompt given to the agent |
| `logs/claude-code-debug.log` | Claude debug log |
| `logs/claude-stdout.log` | Claude run output |
| `logs/openclaw-stdout.log` | OpenClaw run output |
| `latest_session_render.txt` | Readable session summary |
| `openclaw_tool_summary.json` | Parsed OpenClaw tool-call summary |
| `logs/contextsniper-server.log` | ContextSniper backend log |
| `validation.md` | Human-readable validation result |
| `validation.json` | Machine-readable validation result |

## Checks

Claude ContextSniper search calls:

```bash
rg -n "Calling MCP tool: search_code|Tool 'search_code'" \
  scripts/SWE-pro/claude/ContextSniper/output_logs/latest/logs/claude-code-debug.log
```

Claude ContextSniper search timings:

```bash
rg -n "code_semantic_search hybrid path|candidate_ingest_sec" \
  scripts/SWE-pro/claude/ContextSniper/output_logs/latest/logs/contextsniper-server.log
```

OpenClaw ContextSniper tool calls:

```bash
cat scripts/SWE-pro/openclaw/ContextSniper/output_logs/latest/openclaw_tool_summary.json
rg -n "contextsniper_search_code|contextsniper_edit_file" \
  scripts/SWE-pro/openclaw/ContextSniper/output_logs/latest/logs/*.jsonl
```

Filtering should only appear in `ContextSniper-FILTER` runs:

```bash
rg -n "filter_bash|filter_read|FILTER|contextsniper_filter" \
  scripts/SWE-pro/claude/ContextSniper-FILTER/output_logs/latest/logs/*
```

Validation:

```bash
cat scripts/SWE-pro/claude/ContextSniper/output_logs/latest/validation.md
cat scripts/SWE-pro/claude/ContextSniper/output_logs/latest/validation.json
```
