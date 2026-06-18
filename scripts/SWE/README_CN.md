# SWE-bench Lite 运行器

[English README](README.md)

本目录包含六种 SWE-bench Lite 运行方式：

| 场景 | 脚本 | ContextSniper 搜索 | 过滤 |
| --- | --- | --- | --- |
| Claude legacy | `scripts/SWE/claude/legacy/run_swe_task_lite_plain_claude.sh` | 否 | 否 |
| Claude ContextSniper | `scripts/SWE/claude/ContextSniper/run_swe_task_lite_contextsniper_plugin.sh` | 是 | 否 |
| Claude ContextSniper-FILTER | `scripts/SWE/claude/ContextSniper-FILTER/run_swe_task_lite_contextsniper_plugin.sh` | 是 | 是 |
| OpenClaw legacy | `scripts/SWE/openclaw/legacy/run_swe_task_lite_plain_openclaw.sh` | 否 | 否 |
| OpenClaw ContextSniper | `scripts/SWE/openclaw/ContextSniper/run_swe_task_lite_openclaw_contextsniper_plugin.sh` | 是 | 否 |
| OpenClaw ContextSniper-FILTER | `scripts/SWE/openclaw/ContextSniper-FILTER/run_swe_task_lite_openclaw_contextsniper_filter_plugin.sh` | 是 | 是 |

## 运行步骤

从仓库根目录开始：

```bash
cd /path/to/ContextSniper
```

首次准备 ContextSniper：

```bash
./bootstrap.sh --install-swe-deps
```

加载本地 ContextSniper 设置：

```bash
source setup_env.sh
```

运行任务时传入 SWE-bench Lite instance id。未传 id 时，各 runner 会使用自己的默认任务。

## Claude

普通 Claude：

```bash
./scripts/SWE/claude/legacy/run_swe_task_lite_plain_claude.sh django__django-10914
```

Claude + ContextSniper 搜索/编辑：

```bash
./scripts/SWE/claude/ContextSniper/run_swe_task_lite_contextsniper_plugin.sh django__django-10914
```

Claude + ContextSniper 搜索/编辑 + read/bash 过滤：

```bash
./scripts/SWE/claude/ContextSniper-FILTER/run_swe_task_lite_contextsniper_plugin.sh django__django-10914
```

## OpenClaw

普通 OpenClaw：

```bash
source scripts/SWE/openclaw/legacy/setup_swe_env.sh
./scripts/SWE/openclaw/legacy/run_swe_task_lite_plain_openclaw.sh django__django-10914
```

OpenClaw + ContextSniper 搜索/编辑：

```bash
source scripts/SWE/openclaw/ContextSniper/setup_swe_env.sh
./scripts/SWE/openclaw/ContextSniper/run_swe_task_lite_openclaw_contextsniper_plugin.sh django__django-10914
```

OpenClaw + ContextSniper 搜索/编辑 + read/exec 过滤：

```bash
source scripts/SWE/openclaw/ContextSniper-FILTER/setup_swe_env.sh
./scripts/SWE/openclaw/ContextSniper-FILTER/run_swe_task_lite_openclaw_contextsniper_filter_plugin.sh django__django-10914
```

## 快速调试

跳过派生本地环境和最终验证：

```bash
SWE_USE_DERIVED_LOCAL_ENV=0 SWE_SKIP_VALIDATION=1 \
  ./scripts/SWE/claude/ContextSniper/run_swe_task_lite_contextsniper_plugin.sh django__django-10914
```

使用官方 Docker validator，而不是本地验证：

```bash
SWE_VALIDATION_FORCE_LOCAL=0 \
  ./scripts/SWE/claude/ContextSniper/run_swe_task_lite_contextsniper_plugin.sh django__django-10914
```

这些环境变量同样适用于其他 runner。

## 输出

每个 runner 写入自己的 `output_logs` 目录：

```text
scripts/SWE/claude/legacy/output_logs/
scripts/SWE/claude/ContextSniper/output_logs/
scripts/SWE/claude/ContextSniper-FILTER/output_logs/
scripts/SWE/openclaw/legacy/output_logs/
scripts/SWE/openclaw/ContextSniper/output_logs/
scripts/SWE/openclaw/ContextSniper-FILTER/output_logs/
```

每个输出目录都有 `latest` 链接：

```bash
ls -la scripts/SWE/claude/ContextSniper/output_logs/latest
```

常用文件：

| 文件 | 用途 |
| --- | --- |
| `workspace/` | agent 修改后的目标仓库 |
| `workspace/TASK.md` | 传给 agent 的提示词 |
| `logs/claude-code-debug.log` | Claude debug 日志 |
| `logs/claude-stdout.log` | Claude 运行输出 |
| `logs/openclaw-stdout.log` | OpenClaw 运行输出 |
| `latest_session_render.txt` | 可读 session 摘要 |
| `openclaw_tool_summary.json` | 解析后的 OpenClaw 工具调用摘要 |
| `logs/contextsniper-server.log` | ContextSniper 后端日志 |
| `validation.md` | 可读验证结果 |
| `validation.json` | 机器可读验证结果 |

## 检查

Claude ContextSniper 搜索调用：

```bash
rg -n "Calling MCP tool: search_code|Tool 'search_code'" \
  scripts/SWE/claude/ContextSniper/output_logs/latest/logs/claude-code-debug.log
```

Claude ContextSniper 搜索耗时：

```bash
rg -n "code_semantic_search hybrid path|candidate_ingest_sec" \
  scripts/SWE/claude/ContextSniper/output_logs/latest/logs/contextsniper-server.log
```

OpenClaw ContextSniper 工具调用：

```bash
cat scripts/SWE/openclaw/ContextSniper/output_logs/latest/openclaw_tool_summary.json
rg -n "contextsniper_search_code|contextsniper_edit_file" \
  scripts/SWE/openclaw/ContextSniper/output_logs/latest/logs/*.jsonl
```

过滤只应该出现在 `ContextSniper-FILTER` 运行中：

```bash
rg -n "filter_bash|filter_read|FILTER|contextsniper_filter" \
  scripts/SWE/claude/ContextSniper-FILTER/output_logs/latest/logs/*
```

验证结果：

```bash
cat scripts/SWE/claude/ContextSniper/output_logs/latest/validation.md
cat scripts/SWE/claude/ContextSniper/output_logs/latest/validation.json
```
