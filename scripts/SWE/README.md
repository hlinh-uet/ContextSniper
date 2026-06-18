# SWE-bench Lite Runners

[中文文档](README_CN.md)

This folder contains six SWE-bench Lite runners:

| Case | Script | ContextSniper search | Filtering |
| --- | --- | --- | --- |
| Claude legacy | `scripts/SWE/claude/legacy/run_swe_task_lite_plain_claude.sh` | No | No |
| Claude ContextSniper | `scripts/SWE/claude/ContextSniper/run_swe_task_lite_contextsniper_plugin.sh` | Yes | No |
| Claude ContextSniper-FILTER | `scripts/SWE/claude/ContextSniper-FILTER/run_swe_task_lite_contextsniper_plugin.sh` | Yes | Yes |
| OpenClaw legacy | `scripts/SWE/openclaw/legacy/run_swe_task_lite_plain_openclaw.sh` | No | No |
| OpenClaw ContextSniper | `scripts/SWE/openclaw/ContextSniper/run_swe_task_lite_openclaw_contextsniper_plugin.sh` | Yes | No |
| OpenClaw ContextSniper-FILTER | `scripts/SWE/openclaw/ContextSniper-FILTER/run_swe_task_lite_openclaw_contextsniper_filter_plugin.sh` | Yes | Yes |

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

Run a task by passing the SWE-bench Lite instance id. If no id is passed, each
runner uses its own default task.

## Claude

Plain Claude:

```bash
./scripts/SWE/claude/legacy/run_swe_task_lite_plain_claude.sh django__django-10914
```

Claude with ContextSniper search/edit:

```bash
./scripts/SWE/claude/ContextSniper/run_swe_task_lite_contextsniper_plugin.sh django__django-10914
```

Claude with ContextSniper search/edit and read/bash filtering:

```bash
./scripts/SWE/claude/ContextSniper-FILTER/run_swe_task_lite_contextsniper_plugin.sh django__django-10914
```

## OpenClaw

Plain OpenClaw:

```bash
source scripts/SWE/openclaw/legacy/setup_swe_env.sh
./scripts/SWE/openclaw/legacy/run_swe_task_lite_plain_openclaw.sh django__django-10914
```

OpenClaw with ContextSniper search/edit:

```bash
source scripts/SWE/openclaw/ContextSniper/setup_swe_env.sh
./scripts/SWE/openclaw/ContextSniper/run_swe_task_lite_openclaw_contextsniper_plugin.sh django__django-10914
```

OpenClaw with ContextSniper search/edit and read/exec filtering:

```bash
source scripts/SWE/openclaw/ContextSniper-FILTER/setup_swe_env.sh
./scripts/SWE/openclaw/ContextSniper-FILTER/run_swe_task_lite_openclaw_contextsniper_filter_plugin.sh django__django-10914
```

## Quick Debug

Skip derived local environment setup and final validation:

```bash
SWE_USE_DERIVED_LOCAL_ENV=0 SWE_SKIP_VALIDATION=1 \
  ./scripts/SWE/claude/ContextSniper/run_swe_task_lite_contextsniper_plugin.sh django__django-10914
```

Use the official Docker validator instead of local validation:

```bash
SWE_VALIDATION_FORCE_LOCAL=0 \
  ./scripts/SWE/claude/ContextSniper/run_swe_task_lite_contextsniper_plugin.sh django__django-10914
```

The same environment variables work with the other runners.

## Output

Each runner writes to its own `output_logs` directory:

```text
scripts/SWE/claude/legacy/output_logs/
scripts/SWE/claude/ContextSniper/output_logs/
scripts/SWE/claude/ContextSniper-FILTER/output_logs/
scripts/SWE/openclaw/legacy/output_logs/
scripts/SWE/openclaw/ContextSniper/output_logs/
scripts/SWE/openclaw/ContextSniper-FILTER/output_logs/
```

Every output directory has a `latest` symlink:

```bash
ls -la scripts/SWE/claude/ContextSniper/output_logs/latest
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
  scripts/SWE/claude/ContextSniper/output_logs/latest/logs/claude-code-debug.log
```

Claude ContextSniper search timings:

```bash
rg -n "code_semantic_search hybrid path|candidate_ingest_sec" \
  scripts/SWE/claude/ContextSniper/output_logs/latest/logs/contextsniper-server.log
```

OpenClaw ContextSniper tool calls:

```bash
cat scripts/SWE/openclaw/ContextSniper/output_logs/latest/openclaw_tool_summary.json
rg -n "contextsniper_search_code|contextsniper_edit_file" \
  scripts/SWE/openclaw/ContextSniper/output_logs/latest/logs/*.jsonl
```

Filtering should only appear in `ContextSniper-FILTER` runs:

```bash
rg -n "filter_bash|filter_read|FILTER|contextsniper_filter" \
  scripts/SWE/claude/ContextSniper-FILTER/output_logs/latest/logs/*
```

Validation:

```bash
cat scripts/SWE/claude/ContextSniper/output_logs/latest/validation.md
cat scripts/SWE/claude/ContextSniper/output_logs/latest/validation.json
```
