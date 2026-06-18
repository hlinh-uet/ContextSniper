# ContextSniper Claude Code Plugin

Local Claude Code plugin for ContextSniper code search and MCP-based edits.

[中文文档](README_CN.md)

## What Gets Loaded

```text
claude-plugin/
|-- .claude-plugin/plugin.json
|-- .mcp.json
|-- hooks/hooks.json
|-- prompts/code_policy_injection.txt
|-- skills/
|-- bin/
|-- scripts/
`-- contextsniper_mcp/
```

Claude loads this plugin with `--plugin-dir`. The plugin starts the bundled MCP server. The MCP server starts the local ContextSniper backend and AGFS on demand.

## Configure

From a fresh clone, prepare the repository once:

```bash
git clone https://github.com/Calluking/ContextSniper.git
cd ContextSniper
./bootstrap.sh
export CONTEXTSNIPER_EMBEDDING_API_KEY="<your-key>"
source setup_env.sh
```

At minimum, set `CONTEXTSNIPER_EMBEDDING_API_KEY` in your shell or shell profile for real
code search. If you do not use the repository `.venv`, set `PY_BIN` to a Python
that can import `flask`, `mcp`, `openai`, and `pyagfs`. The MCP launcher also
checks common local Conda paths such as `~/miniconda3/bin/python`.
`./bootstrap.sh` creates `.venv`, installs `requirements.txt`, and builds the
bundled AGFS server at `agfs/build/agfs-server`.

The repository loader [../setup_env.sh](../setup_env.sh) imports shell profile
settings and applies repo defaults.

## Start Claude

Recommended command after exporting your ContextSniper settings:

```bash
cd /path/to/project
$CONTEXTSNIPER_DIR/claude-plugin/bin/contextsniper-claude
```

That helper sources the repository environment and then runs:

```bash
claude --plugin-dir "$CONTEXTSNIPER_DIR/claude-plugin"
```

If your shell already exports the same environment values, you can run Claude
directly from the project Claude should edit:

```bash
cd /path/to/project
claude --plugin-dir "$CONTEXTSNIPER_DIR/claude-plugin"
```

Do not pass `--mcp-config`; this plugin owns its `.mcp.json`.

The plugin MCP entrypoint and hook/manual commands honor `PY_BIN`. Without
`PY_BIN`, they try the repository `.venv`, common local Conda locations, and
then `python3`/`python` on `PATH`.

## Validate

Inside Claude:

```text
/plugin
```

Expected status:

```text
contextsniper Plugin · inline · ✔ enabled
└ contextsniper MCP · ✔ connected
```

Then try a code task:

```text
Fix the bug in the add function
```

The `UserPromptSubmit` hook injects the rendered [prompts/code_policy_injection.txt](prompts/code_policy_injection.txt). Claude should call:

```text
mcp__plugin_contextsniper_contextsniper__search_code
mcp__plugin_contextsniper_contextsniper__edit_file
```

If the status says environment variables such as `CONTEXTSNIPER_DIR` or
`CONTEXTSNIPER_RUNTIME_DIR` are missing, restart Claude with `claude-plugin/bin/contextsniper-claude`
from this checkout. Do not add a separate `--mcp-config`; the plugin's own
`.mcp.json` is the supported path.

The filtering-strategy section is controlled by `CONTEXTSNIPER_INJECT_FILTERING_PROMPT`.
Set it to `1` to include that section in interactive Claude sessions:

```bash
CONTEXTSNIPER_INJECT_FILTERING_PROMPT=1 claude-plugin/bin/contextsniper-render-code-policy
```

## Manual Commands

From the repository root:

```bash
claude-plugin/bin/contextsniper-status
claude-plugin/bin/contextsniper-start
claude-plugin/bin/contextsniper-stop
```

Memory helpers:

```bash
claude-plugin/bin/contextsniper-compose "what did we decide about MCP search?"
claude-plugin/bin/contextsniper-add-history --dry-run
claude-plugin/bin/contextsniper-add-history --yes
```

## Lifecycle

- `CONTEXTSNIPER_PLUGIN_AUTO_START=1`: auto-start backend services when needed.
- `CONTEXTSNIPER_PLUGIN_AUTO_STOP=1`: stop ContextSniper and AGFS when Claude exits.
- `CONTEXTSNIPER_PLUGIN_HOOK_START_WAIT=8`: maximum normal wait, in seconds, for a cold hook to start ContextSniper.
- `CONTEXTSNIPER_URL`: local ContextSniper HTTP endpoint, default `http://127.0.0.1:8090`.
- `AGFS_BASE_URL`: local AGFS endpoint, default `http://127.0.0.1:1833`.
