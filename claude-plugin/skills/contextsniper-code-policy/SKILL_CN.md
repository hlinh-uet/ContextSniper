---
name: contextsniper-code-policy
description: 高效使用 ContextSniper MCP 代码搜索和编辑工具进行代码库调查与修补。
---

# ContextSniper 代码策略

当项目中可用 `contextsniper` MCP 工具，并且需要处理源码时，使用本策略。

## MCP 要求

在编辑任何 source/code 文件前，至少调用一次 ContextSniper MCP 工具 `search_code`。

使用 `path=$WORK_DIR`，查询词按你平时使用 `grep` 的方式组织。

如果未设置 `$WORK_DIR`，使用 Claude 启动时的工作区根目录，除非用户明确指定其他目录。插件会把空路径依次解析为 `CONTEXTSNIPER_WORKSPACE_ROOT`、`CLAUDE_PROJECT_DIR`，最后是当前工作目录。

比较多个靠前的 L2 chunk 命中，然后只修改最匹配的文件。

不要在此任务中使用 Claude 内置 `Edit` 工具。

把 ContextSniper MCP 工具 `edit_file` 视为编辑工具。

## 编码任务协议

任务步骤：

1. 用聚焦的、符合项目习惯的测试或命令复现问题。
2. 在大范围读取文件前，先使用 ContextSniper MCP 工具 `search_code`。
3. 结合失败行为和语义搜索结果定位最匹配的实现位置。
4. 使用 ContextSniper MCP 工具 `edit_file` 修复问题，不使用 Claude 内置 `Edit`。
5. 重新运行验证，确保相关测试通过。

最终回答必须包含：

- 根因；
- 修改的文件；
- 验证命令和输出。

## MCP 效率策略（减少轮次和 token）

- 将 ContextSniper 的 `search_code` 作为大范围 `grep` 和探索性 `read` 的替代。
- 减少额外搜索循环：优先使用 1-2 次高质量、聚焦到符号级别的 `search_code` 查询。
- 如果返回 snippet 已包含目标文件和有用行上下文，直接行动：
  - 立即用 ContextSniper MCP 工具 `edit_file` 编辑；或
  - 执行下一个具体动作（打补丁/测试），不要继续大范围读取。
- 命中良好后避免读取大量无关文件，保持轮次和 token 低。
- 只有在验证安全性或精确补丁依赖时，才额外读取文件。
- 一旦通过 `search_code` 找到代码，跳过内置 `Read`/`Edit` 路径，直接分析并用 MCP 工具编辑。
