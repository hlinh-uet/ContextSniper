# ContextSniper OpenClaw 插件

这是 ContextSniper 的原生 OpenClaw 插件，提供代码搜索和精确字符串替换编辑。

插件暴露的工具：

- `contextsniper_search_code`
- `contextsniper_edit_file`
- `contextsniper_index_codebase`
- `contextsniper_health`

OpenClaw 加载插件时，插件可以自动启动本地 ContextSniper 和 AGFS 服务；OpenClaw 退出时，会停止由插件启动的服务。

对于代码相关 prompt，插件会注入策略，要求 agent 在大范围读取文件前调用 `contextsniper_search_code`，并在可行时用 `contextsniper_edit_file` 修改文件。

启用过滤时，插件还会 hook OpenClaw 原生的 `read` 和 `exec` 调用。整文件读取会被改写到 ContextSniper 生成的过滤后文件；Python/pytest 测试、diff 等白名单长输出命令会被 wrapper 包起来，先经过 ContextSniper 过滤再返回给 agent。被过滤的输出会包含取回原始输出的提示。

## 安装

在全新 clone 中，先准备一次仓库环境：

```bash
git clone https://github.com/Calluking/ContextSniper.git
cd ContextSniper
./bootstrap.sh
export OPENROUTER_API_KEY="<your-openrouter-key>"
source setup_env.sh
```

至少在 shell profile 中设置 `OPENROUTER_API_KEY`（或底层变量
`CONTEXTSNIPER_EMBEDDING_API_KEY`）。OpenClaw
插件加载时会通过 `setup_env.sh` 导入 shell profile 设置并应用仓库默认值。
`./bootstrap.sh` 会创建 `.venv`、安装 `requirements.txt`，并把内置 AGFS
server 构建到 `agfs/build/agfs-server`。

你也可以把 embedding 密钥放在本机 shell 环境里，例如 `~/.bashrc`：

```bash
export OPENROUTER_API_KEY="<your-openrouter-key>"
export CONTEXTSNIPER_EMBEDDING_BASE_URL="https://openrouter.ai/api/v1"
export CONTEXTSNIPER_EMBEDDING_MODEL="openai/text-embedding-3-small"
```

然后以本地链接方式安装插件：

```bash
openclaw plugins install --link ./openclaw-plugin --dangerously-force-unsafe-install
openclaw plugins enable contextsniper
openclaw gateway restart
```

也可以直接运行 `./bootstrap.sh --install-openclaw-plugin`，它会在准备仓库后
执行这些 OpenClaw 插件安装命令。

如果这个插件之前已经从另一个 checkout 安装过，先卸载旧注册项，再重新链接
当前 clone：

```bash
openclaw plugins uninstall contextsniper --force
openclaw plugins install --link ./openclaw-plugin --dangerously-force-unsafe-install
```

OpenClaw 要求显式加上 unsafe-install 参数，是因为这个插件会通过 Node
child process API 启动本地 ContextSniper/AGFS 服务。对于自动启动能力来说这是预期行为；
插件只会启动本仓库内的本地服务。

验证运行时是否加载：

```bash
openclaw plugins inspect contextsniper --runtime --json
```

运行时输出应包含 `status: "loaded"` 以及这些工具：

```text
contextsniper_health
contextsniper_index_codebase
contextsniper_search_code
contextsniper_edit_file
```

在全新机器上，这个 inspect 命令不应该再提示缺少 `CONTEXTSNIPER_DIR` 或
`CONTEXTSNIPER_RUNTIME_DIR`。这些值现在由插件自己初始化。

## 配置

正常交互使用不需要额外配置。插件加载时会从本地链接的插件目录自动发现
ContextSniper 仓库，导入 `setup_env.sh`，在需要时启动 ContextSniper/AGFS，把 OpenClaw 当前启动
目录作为目标 workspace，并默认开启 read/exec 过滤。

下面这些是高级覆盖项，配置位置为
`plugins.entries.contextsniper.config`。交互式使用时默认忽略持久化的
OpenClaw 配置，避免旧 benchmark 设置污染新 chat。设置
`CONTEXTSNIPER_OPENCLAW_RESPECT_CONFIG=1` 后才会使用这些值：

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

默认情况下，插件使用 OpenClaw 进程启动目录作为 workspace。特殊 harness 如果要使用
`CONTEXTSNIPER_WORKSPACE_ROOT` 或 `OPENCLAW_WORKSPACE_ROOT`，需要先设置
`CONTEXTSNIPER_OPENCLAW_RESPECT_ENV_PATHS=1`。持久化的 OpenClaw `workspaceRoot` 配置只有同时设置
`CONTEXTSNIPER_OPENCLAW_RESPECT_CONFIG=1` 和 `CONTEXTSNIPER_OPENCLAW_RESPECT_CONFIG_WORKSPACE=1`
时才会生效，这样可以避免 SWE 跑出来的旧 workspace 泄漏到交互式 session。

环境变量开关与 Claude 插件保持一致。它们只是覆盖默认行为时才需要设置：

```bash
export CONTEXTSNIPER_FILTER_ENABLED=1
export CONTEXTSNIPER_FILTER_NATIVE_READ=1
export CONTEXTSNIPER_FILTER_NATIVE_BASH=1
```

把任意开关设为 `0`、`false`、`no` 或 `off` 可关闭对应层。

注入 prompt 里的过滤策略说明也和 Claude 一样由同一个变量控制：

```bash
export CONTEXTSNIPER_INJECT_FILTERING_PROMPT=1
```

默认不会把这段策略说明注入 prompt，但实际 filter hook 仍然可用。

## 启动 OpenClaw

最小 OpenClaw 流程：

```bash
git clone https://github.com/Calluking/ContextSniper.git
cd ContextSniper
./bootstrap.sh --install-openclaw-plugin
$EDITOR setup_env.sh
source setup_env.sh
cd /path/to/project
openclaw chat --local
```

只有在 `cd` 到希望 agent 修改的项目目录后，再运行 `openclaw chat --local`。
正常交互使用不需要设置 `CONTEXTSNIPER_WORKSPACE_ROOT`、`CONTEXTSNIPER_DIR` 或
`CONTEXTSNIPER_RUNTIME_DIR`。

ContextSniper 会把你启动 `openclaw chat --local` 时所在的目录作为代码搜索/编辑
workspace。OpenClaw 原生 shell 运行 `pwd` 时，仍可能显示内部 workspace，例如
`/root/.openclaw/workspace`。如果 shell 命令必须在项目目录运行，请在 prompt
里明确写出目标项目路径。

然后带上明确目标路径提问：

```text
The target project is /path/to/project. Run cd /path/to/project && ./scripts/run_smoke.sh, identify the failing implementation, fix only that bug, and rerun the command until it passes.
```

TUI 可能会折叠工具调用卡片。终端里没有直接看到 `contextsniper_search_code`，不代表工具没有被调用。

## 验证某次运行使用了搜索

找到最新的 OpenClaw session JSONL，并搜索 ContextSniper 工具调用：

```bash
latest=$(ls -t ~/.openclaw/agents/*/sessions/*.jsonl | grep -v trajectory | head -1)
rg -n "ContextSniper|FILTER IS TRIGGERED|contextsniper_search_code|contextsniper_edit_file" "$latest"
```

一次成功运行通常会包含这样的流程：

```text
TOOL CALL: contextsniper_search_code
TOOL RESULT: contextsniper_search_code
TOOL CALL: contextsniper_edit_file
TOOL RESULT: contextsniper_edit_file
TOOL CALL: exec
```

示例 `contextsniper_search_code` 参数：

```json
{
  "path": "/path/to/project",
  "query": "add function"
}
```
