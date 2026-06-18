# ContextSniper Claude Code 插件

用于 ContextSniper 代码搜索和 MCP 文件编辑的本地 Claude Code 插件。

[English README](README.md)

## Claude 会加载什么

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

Claude 通过 `--plugin-dir` 加载这个插件。插件会启动内置 MCP server，MCP server 会按需启动本地 ContextSniper 后端和 AGFS。

## 配置

在全新 clone 中，先准备一次仓库环境：

```bash
git clone https://github.com/Calluking/ContextSniper.git
cd ContextSniper
./bootstrap.sh
export CONTEXTSNIPER_EMBEDDING_API_KEY="<your-key>"
source setup_env.sh
```

实际做代码搜索时，至少要在 shell 或 shell profile 中设置
`CONTEXTSNIPER_EMBEDDING_API_KEY`。如果不使用仓库里的
`.venv`，请把 `PY_BIN` 指向能 import `flask`、`mcp`、`openai` 和
`pyagfs` 的 Python。MCP 启动器也会检查常见本地 Conda 路径，例如
`~/miniconda3/bin/python`。
`./bootstrap.sh` 会创建 `.venv`、安装 `requirements.txt`，并把内置 AGFS
server 构建到 `agfs/build/agfs-server`。

仓库级 [../setup_env.sh](../setup_env.sh) 会导入 shell profile 设置并应用仓库默认值。

## 启动 Claude

导出 ContextSniper 设置后，推荐命令是：

```bash
cd /path/to/project
$CONTEXTSNIPER_DIR/claude-plugin/bin/contextsniper-claude
```

这个 helper 会先加载仓库环境，然后运行：

```bash
claude --plugin-dir "$CONTEXTSNIPER_DIR/claude-plugin"
```

如果你的 shell 已经导出了同样的环境变量，也可以在 Claude 要修改的项目目录中直接运行：

```bash
cd /path/to/project
claude --plugin-dir "$CONTEXTSNIPER_DIR/claude-plugin"
```

不要传 `--mcp-config`，插件自带 `.mcp.json`。

插件 MCP 入口、hooks 和手动命令都会尊重 `PY_BIN`。如果没有设置 `PY_BIN`，
它们会依次尝试仓库 `.venv`、常见本地 Conda 路径，以及 `PATH` 上的
`python3`/`python`。

## 验证

在 Claude 里运行：

```text
/plugin
```

预期状态：

```text
contextsniper Plugin · inline · ✔ enabled
└ contextsniper MCP · ✔ connected
```

然后尝试一个代码任务：

```text
Fix the bug in the add function
```

`UserPromptSubmit` hook 会注入渲染后的 [prompts/code_policy_injection.txt](prompts/code_policy_injection.txt)。Claude 应该调用：

```text
mcp__plugin_contextsniper_contextsniper__search_code
mcp__plugin_contextsniper_contextsniper__edit_file
```

如果状态提示缺少 `CONTEXTSNIPER_DIR` 或 `CONTEXTSNIPER_RUNTIME_DIR` 等环境变量，请从当前 checkout
重新用 `claude-plugin/bin/contextsniper-claude` 启动 Claude。不要额外传 `--mcp-config`；
插件自带的 `.mcp.json` 才是支持的路径。

过滤策略说明由 `CONTEXTSNIPER_INJECT_FILTERING_PROMPT` 控制。设为 `1` 时，交互式
Claude 会注入该段说明：

```bash
CONTEXTSNIPER_INJECT_FILTERING_PROMPT=1 claude-plugin/bin/contextsniper-render-code-policy
```

## 手动命令

在仓库根目录运行：

```bash
claude-plugin/bin/contextsniper-status
claude-plugin/bin/contextsniper-start
claude-plugin/bin/contextsniper-stop
```

记忆辅助命令：

```bash
claude-plugin/bin/contextsniper-compose "what did we decide about MCP search?"
claude-plugin/bin/contextsniper-add-history --dry-run
claude-plugin/bin/contextsniper-add-history --yes
```

## 生命周期

- `CONTEXTSNIPER_PLUGIN_AUTO_START=1`：需要时自动启动后端服务。
- `CONTEXTSNIPER_PLUGIN_AUTO_STOP=1`：Claude 退出时停止 ContextSniper 和 AGFS。
- `CONTEXTSNIPER_PLUGIN_HOOK_START_WAIT=8`：冷启动 hook 等待 ContextSniper 启动的常规上限，单位为秒。
- `CONTEXTSNIPER_URL`：本地 ContextSniper HTTP endpoint，默认 `http://127.0.0.1:8090`。
- `AGFS_BASE_URL`：本地 AGFS endpoint，默认 `http://127.0.0.1:1833`。
