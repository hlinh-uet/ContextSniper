<p align="center">
  <img src="docs/assets/readme/ContextSniper/LOGO_BACKGROUND.png" alt="ContextSniper" width="760">
</p>

# ContextSniper

<p align="center">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MulanPSL--2.0-blue.svg"></a>
  <a href="pyproject.toml"><img alt="Python" src="https://img.shields.io/badge/python-3.11%2B-blue.svg"></a>
  <a href="pyproject.toml"><img alt="Version" src="https://img.shields.io/badge/version-0.1.0-blue.svg"></a>
  <a href="claude-plugin/"><img alt="Claude Code" src="https://img.shields.io/badge/Claude%20Code-plugin-purple.svg"></a>
  <a href="openclaw-plugin/"><img alt="OpenClaw" src="https://img.shields.io/badge/OpenClaw-plugin-blue.svg"></a>
  <a href="filter/"><img alt="ContextSniper Filter" src="https://img.shields.io/badge/filter-read%20%2B%20bash-orange.svg"></a>
  <a href="agfs/"><img alt="AGFS" src="https://img.shields.io/badge/AGFS-local%20memory-teal.svg"></a>
</p>

Make a local codebase searchable from Claude Code or OpenClaw with semantic code
search, filtered long outputs, and exact-replacement file edits.

[中文文档](README_CN.md)

## Usage Cost Reduction

The SWE Lite verification reports include token and turn breakdowns for Claude
Code and OpenClaw runs. We chose `text-embedding-3-large` as the embedding
model.

| Claude Code | OpenClaw |
| --- | --- |
| ![Average usage cost, Claude Code](docs/assets/readme/average_usage_cost_barchart.png) | ![Average usage cost, OpenClaw](docs/assets/readme/average_usage_cost_barchart_openclaw.png) |

### Claude Average Per Task

| Metric | Legacy&nbsp;&nbsp;&nbsp;&nbsp; | ContextSniper&nbsp;&nbsp;&nbsp;&nbsp; | ContextSniper Filter&nbsp;&nbsp;&nbsp;&nbsp; | ContextSniper Reduction | ContextSniper Filter Reduction |
| --- | ---: | ---: | ---: | ---: | ---: |
| Input tokens | 1,534,963 | 941,761 | 810,396 | -39% | <strong><font color="#26C889">-47%</font></strong> |
| Output tokens | 16,969 | 11,656 | 11,946 | <strong><font color="#26C889">-31%</font></strong> | -30% |
| Total tokens | 1,551,933 | 953,417 | 822,342 | -39% | <strong><font color="#26C889">-47%</font></strong> |

### OpenClaw Average Per Task

| Metric | Legacy&nbsp;&nbsp;&nbsp;&nbsp; | ContextSniper&nbsp;&nbsp;&nbsp;&nbsp; | ContextSniper Filter&nbsp;&nbsp;&nbsp;&nbsp; | ContextSniper Reduction | ContextSniper Filter Reduction |
| --- | ---: | ---: | ---: | ---: | ---: |
| Input tokens | 1,309,937 | 769,590 | 622,321 | -41% | <strong><font color="#26C889">-52%</font></strong> |
| Output tokens | 17,439 | 10,399 | 12,982 | <strong><font color="#26C889">-40%</font></strong> | -26% |
| Total tokens | 1,327,377 | 779,989 | 635,303 | -41% | <strong><font color="#26C889">-52%</font></strong> |

## Overview

ContextSniper ships two local plugins and one Codex evaluation adapter:

| Host | Plugin | What it adds |
| --- | --- | --- |
| Claude Code | [claude-plugin/](claude-plugin/) | MCP tools for code search/edit plus prompt policy injection |
| OpenClaw | [openclaw-plugin/](openclaw-plugin/) | Native tools, prompt policy injection, and read/exec filtering |
| Codex CLI | [ContextSniper-Codex/](ContextSniper-Codex/) | Plain ContextSniper search/edit with Codex `gpt-5.6-sol` low and bundled Defects4C validation |

Both plugins can start the local ContextSniper backend and AGFS service
for you, then stop the services they started when the host exits.

![Runtime architecture](<docs/assets/readme/ContextSniper/EN_Runtime Architecture.png>)

## Quickstart

Prepare a fresh checkout once:

```bash
git clone https://github.com/Calluking/ContextSniper.git
cd ContextSniper
./bootstrap.sh
```

`bootstrap.sh` is interactive for setup steps: if Ubuntu packages or Python
`.venv` are missing, it asks before installing anything. It also checks whether
OpenClaw and Claude Code are installed, then asks which integration to set up.
It does not ask you to type API keys into the installer.

For a clone that will be used by a teammate, create the ignored local
environment file and add that teammate's own credentials:

```bash
cp .env.example .env
# Edit .env and set OPENROUTER_API_KEY.
```

`setup_env.sh` loads the repository-root `.env` automatically. Never commit or
share a populated `.env`; share `.env.example` instead.

Export your model and embedding settings (DeepSeek for OpenClaw and OpenRouter
for embeddings in this example):
```bash
export DEEPSEEK_API_KEY="<your-deepseek-key>"
export DEEPSEEK_BASE_URL="https://api.deepseek.com"
export OPENCLAW_MODEL="deepseek/deepseek-v4-flash"
export OPENROUTER_API_KEY="<your-openrouter-key>"
export CONTEXTSNIPER_EMBEDDING_BASE_URL="https://openrouter.ai/api/v1"
export CONTEXTSNIPER_EMBEDDING_MODEL="openai/text-embedding-3-small"
export ANTHROPIC_MODEL="haiku"
```

Then load the settings:

```bash
source setup_env.sh
```

`./bootstrap.sh` creates `.venv`, installs the runtime dependencies in
`requirements.txt`, and builds `agfs/build/agfs-server`. Full SWE-bench
validation is optional; install that heavier stack only when needed with
`./bootstrap.sh --install-swe-deps`.

![Fresh clone quickstart](<docs/assets/readme/ContextSniper/EN_Fresh Clone Start.png>)

## Start Claude

From the project you want Claude to edit:

```bash
cd /path/to/project
claude --plugin-dir "$CONTEXTSNIPER_DIR/claude-plugin"
```

Do not pass `--mcp-config`; the Claude plugin owns its `.mcp.json`.

## Start OpenClaw

From the project you want OpenClaw to edit:

```bash
cd /path/to/project
openclaw chat --local
```

## Requirements

- Python 3.11+
- Go 1.22+
- Claude Code CLI, OpenClaw CLI, or both
- An OpenAI-compatible embedding endpoint and API key
- Conda, only if you use the SWE runner's default local validation environment

## Verify

Claude:

```text
/plugin
```

Expected status:

```text
contextsniper Plugin · inline · ✔ enabled
└ contextsniper MCP · ✔ connected
```

OpenClaw:

```bash
openclaw plugins inspect contextsniper --runtime --json
```

The runtime output should include:

```text
contextsniper_health
contextsniper_index_codebase
contextsniper_search_code
contextsniper_edit_file
```

To confirm a run used ContextSniper search/filtering:

```bash
latest=$(ls -t ~/.openclaw/agents/*/sessions/*.jsonl | grep -v trajectory | head -1)
rg -n "ContextSniper|FILTER IS TRIGGERED|contextsniper_search_code|contextsniper_edit_file" "$latest"
```

For Claude, inspect the current Claude debug log or the `/plugin` panel. A
healthy code run should show MCP tool names containing `search_code` and
`edit_file`.

![Code task flow](<docs/assets/readme/ContextSniper/EN_Code Task Flow.png>)

## Configuration

Local settings should come from your shell environment or shell profile, which
`setup_env.sh` imports before applying repo defaults. Do not commit real API
keys.

Common settings:

| Variable | Purpose |
| --- | --- |
| `OPENROUTER_API_KEY` | OpenRouter key; mapped to ContextSniper's internal embedding key |
| `CONTEXTSNIPER_EMBEDDING_API_KEY` | API key for real semantic code search |
| `CONTEXTSNIPER_EMBEDDING_BASE_URL` | OpenAI-compatible embedding endpoint |
| `CONTEXTSNIPER_EMBEDDING_MODEL` | Embedding model name |
| `PY_BIN` | Optional Python override for plugin/backend runtime |
| `CONTEXTSNIPER_FILTER_ENABLED` | Toggle read/exec filtering, default `1` |
| `CONTEXTSNIPER_INJECT_FILTERING_PROMPT` | Toggle the filtering-strategy prompt section |

`requirements.txt` includes `httpx[socks]`, and `setup_env.sh` sets
`NO_PROXY`/`no_proxy` for `127.0.0.1`, `localhost`, and `::1` so local ContextSniper/AGFS
traffic bypasses HTTP(S)/SOCKS proxies.

## Useful Files

- [bootstrap.sh](bootstrap.sh): one-command local setup.
- [setup_env.sh](setup_env.sh): shared environment loader.
- [claude-plugin/prompts/code_policy_injection.txt](claude-plugin/prompts/code_policy_injection.txt): Claude policy prompt.
- [openclaw-plugin/prompts/code_policy_injection.txt](openclaw-plugin/prompts/code_policy_injection.txt): OpenClaw policy prompt.
- [docs/assets/readme/](docs/assets/readme/): generated README diagrams. Refresh with `python3 docs/assets/readme/generate_readme_diagrams.py`.
- [scripts/SWE/](scripts/SWE/): SWE Lite runners and reports.
- [LICENSE](LICENSE): Mulan Permissive Software License v2 (`MulanPSL-2.0`).

## SWE Lite Runner

The SWE runners build a task prompt and start Claude or OpenClaw
non-interactively. See [scripts/SWE/README.md](scripts/SWE/README.md) for the
six runner modes, run commands, output locations, and log checks.

## Codex Defects4C Runner

The [ContextSniper-Codex adapter](ContextSniper-Codex/) runs one plain
ContextSniper attempt with `gpt-5.6-sol` at low reasoning, records Codex token
events, and uses the prepared validation contract to classify the patch as
`plausible`, `cleanfix`, `noisefix`, `nonefix`, `negfix`, or `invalid`.

## License

This project is licensed under the Mulan Permissive Software License v2
(`MulanPSL-2.0`). See [LICENSE](LICENSE).

## References

- [AGFS](https://github.com/c4pt0r/agfs): ContextSniper bundles and builds the local AGFS
  server under [agfs/](agfs/) and uses `pyagfs` for local memory/file-service
  operations.
