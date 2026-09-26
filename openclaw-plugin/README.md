# ContextSniper for OpenClaw

Native OpenClaw plugin for ContextSniper code search and exact string replacement edits.

The plugin exposes these OpenClaw tools:

- `contextsniper_search_code`
- `contextsniper_edit_file`
- `contextsniper_index_codebase`
- `contextsniper_health`

It can also start the local ContextSniper and AGFS services when OpenClaw loads the plugin, then stop the services it started when OpenClaw exits.

For code-looking prompts, the plugin injects a policy that asks the agent to call `contextsniper_search_code` before broad file reads and to use `contextsniper_edit_file` for the patch when practical.

When the filter is enabled, the plugin also hooks native OpenClaw `read` and
`exec` calls. Full-file reads can be rewritten to an ContextSniper-filtered file, and
whitelisted long-output commands such as Python/pytest test runs and diffs are
wrapped so their output goes through ContextSniper before it reaches the agent. Filtered
outputs include the original-output retrieval hint.

## Install

From a fresh clone, prepare the repository once:

```bash
git clone https://github.com/Calluking/ContextSniper.git
cd ContextSniper
./bootstrap.sh
export OPENROUTER_API_KEY="<your-openrouter-key>"
source setup_env.sh
```

Set at least `OPENROUTER_API_KEY` (or the lower-level
`CONTEXTSNIPER_EMBEDDING_API_KEY`) in your shell profile. The OpenClaw plugin loads
`setup_env.sh`, which imports shell profile settings and applies repo defaults.
`./bootstrap.sh` creates `.venv`, installs
`requirements.txt`, and builds the bundled AGFS server at
`agfs/build/agfs-server`.

OpenClaw also needs agent model credentials. For a DeepSeek setup, export these
before running `./bootstrap.sh --install-openclaw-plugin`:

```bash
export OPENAI_API_KEY="<your-deepseek-key>"
export OPENAI_BASE_URL="https://api.deepseek.com"
export OPENCLAW_MODEL="deepseek/deepseek-v4-flash"
```

You may also keep secret embedding settings in your shell environment, for
example in `~/.bashrc`:

```bash
export OPENROUTER_API_KEY="<your-openrouter-key>"
export CONTEXTSNIPER_EMBEDDING_BASE_URL="https://openrouter.ai/api/v1"
export CONTEXTSNIPER_EMBEDDING_MODEL="openai/text-embedding-3-small"
```

Then install the plugin as a linked local plugin:

```bash
./bootstrap.sh --install-openclaw-plugin
```

The bootstrap command installs/checks local prerequisites, links the plugin,
verifies the runtime tools, creates an OpenClaw auth profile when
`OPENCLAW_MODEL` plus `OPENCLAW_API_KEY` or `OPENAI_API_KEY` are present, and
prints `openclaw configure` if model credentials are still missing.

If this plugin was already installed from another checkout, uninstall the old
registration first and relink the current clone:

```bash
openclaw plugins uninstall contextsniper --force
./bootstrap.sh --install-openclaw-plugin
```

OpenClaw asks for the explicit unsafe-install flag because this plugin starts
local ContextSniper/AGFS processes through Node's child process API. That is expected for
auto-start. The plugin starts only local services from this repository.

Verify it:

```bash
openclaw plugins inspect contextsniper --runtime --json
```

The runtime output should show `status: "loaded"` and these tools:

```text
contextsniper_health
contextsniper_index_codebase
contextsniper_search_code
contextsniper_edit_file
```

On a fresh machine, this inspect command should not report missing
`CONTEXTSNIPER_DIR`/`CONTEXTSNIPER_RUNTIME_DIR`. Those values are initialized by the plugin now.

## Configure

For normal interactive use, there is nothing to configure after installation.
When the plugin loads, it discovers this repository from the linked plugin
directory, imports `setup_env.sh`, starts ContextSniper/AGFS if needed, uses the current
OpenClaw working directory as the target workspace, and enables read/exec
filtering by default.

The settings below are advanced overrides under
`plugins.entries.contextsniper.config`. Persisted OpenClaw config is
ignored by default during interactive use so old benchmark settings cannot
poison a new chat. Set `CONTEXTSNIPER_OPENCLAW_RESPECT_CONFIG=1` to honor these values.

```json
{
  "workspaceRoot": "/path/to/project",
  "contextsniperUrl": "http://127.0.0.1:8090",
  "autoStart": true,
  "autoStop": true,
  "injectCodePolicy": true,
  "filterEnabled": true,
  "filterNativeRead": true,
  "filterNativeExec": true
}
```

By default the plugin uses OpenClaw's process working directory as the
workspace. For special harnesses, set `CONTEXTSNIPER_OPENCLAW_RESPECT_ENV_PATHS=1` before
using `CONTEXTSNIPER_WORKSPACE_ROOT` or `OPENCLAW_WORKSPACE_ROOT`. A persisted OpenClaw
`workspaceRoot` config value is only honored when both
`CONTEXTSNIPER_OPENCLAW_RESPECT_CONFIG=1` and `CONTEXTSNIPER_OPENCLAW_RESPECT_CONFIG_WORKSPACE=1`,
which avoids stale SWE-run workspaces leaking into interactive sessions.

Environment toggles use the same names as the Claude plugin. They are optional;
set them only when you want to override the defaults:

```bash
export CONTEXTSNIPER_FILTER_ENABLED=1
export CONTEXTSNIPER_FILTER_NATIVE_READ=1
export CONTEXTSNIPER_FILTER_NATIVE_BASH=1
```

Set any of those to `0`, `false`, `no`, or `off` to disable that layer.

The filtering-strategy paragraph in the injected prompt is also controlled the
same way as Claude:

```bash
export CONTEXTSNIPER_INJECT_FILTERING_PROMPT=1
```

By default it is omitted from the prompt while the actual filter hooks remain
available.

## Start OpenClaw

Minimal OpenClaw flow:

```bash
git clone https://github.com/Calluking/ContextSniper.git
cd ContextSniper
./bootstrap.sh --install-openclaw-plugin
$EDITOR setup_env.sh
source setup_env.sh
cd /path/to/project
openclaw chat --local
```

Start `openclaw chat --local` only after you `cd` into the project you want the
agent to work on. No manual `CONTEXTSNIPER_WORKSPACE_ROOT`, `CONTEXTSNIPER_DIR`, or
`CONTEXTSNIPER_RUNTIME_DIR` is needed for normal interactive use.

ContextSniper uses the directory where you launched `openclaw chat --local` as its code
search/edit workspace. OpenClaw's native shell may still report its internal
workspace, such as `/root/.openclaw/workspace`, when the agent runs `pwd`. If a
shell command must run from the project directory, name the target path in your
prompt.

Then ask with an explicit target path:

```text
The target project is /path/to/project. Run cd /path/to/project && ./scripts/run_smoke.sh, identify the failing implementation, fix only that bug, and rerun the command until it passes.
```

The TUI may keep tool cards collapsed. Absence of visible `contextsniper_search_code` text in the terminal does not mean search was skipped.

## Verify A Run Used Search

Find the newest OpenClaw session JSONL and search for ContextSniper tool calls:

```bash
latest=$(ls -t ~/.openclaw/agents/*/sessions/*.jsonl | grep -v trajectory | head -1)
rg -n "ContextSniper|FILTER IS TRIGGERED|contextsniper_search_code|contextsniper_edit_file" "$latest"
```

A successful run should include a flow like:

```text
TOOL CALL: contextsniper_search_code
TOOL RESULT: contextsniper_search_code
TOOL CALL: contextsniper_edit_file
TOOL RESULT: contextsniper_edit_file
TOOL CALL: exec
```

Example `contextsniper_search_code` arguments:

```json
{
  "path": "/path/to/project",
  "query": "add function"
}
```

## Runtime

Default runtime directory:

```text
~/.cache/contextsniper-openclaw-plugin
```

Manual service commands are still available through the shared ContextSniper terminal script:

```bash
source setup_env.sh
"$PY_BIN" claude-plugin/scripts/contextsniper_terminal.py start --runtime-dir ~/.cache/contextsniper-openclaw-plugin
"$PY_BIN" claude-plugin/scripts/contextsniper_terminal.py status --runtime-dir ~/.cache/contextsniper-openclaw-plugin
"$PY_BIN" claude-plugin/scripts/contextsniper_terminal.py stop --runtime-dir ~/.cache/contextsniper-openclaw-plugin
```
