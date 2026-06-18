---
name: contextsniper-code-policy
description: Use ContextSniper MCP code search and edit tools efficiently for codebase investigation and patching.
---

# ContextSniper Code Policy

Use this policy when working on source code in a project with the `contextsniper` MCP tools available.

## MCP Requirement

Before you edit any source/code file, call the contextsniper MCP tool search_code at least once.

Use path=$WORK_DIR and query terms using how you would call 'grep' tool.

If `$WORK_DIR` is not set, use the workspace root where Claude was started unless the user explicitly names a different tree. The plugin resolves an empty path to `CONTEXTSNIPER_WORKSPACE_ROOT`, then `CLAUDE_PROJECT_DIR`, then the current working directory.

Compare multiple top L2 chunk hits, then patch only the best-matching files.

Do not use Claude's built-in `Edit` tool for this task.

Treat the ContextSniper MCP tool named `edit_file` as your edit tool.

## Coding Task Protocol

Task:

1. Reproduce the issue with focused project-appropriate tests or commands.
2. Use the ContextSniper MCP tool named `search_code` before broad file reads.
3. Use the failing behavior and semantic search results to locate the best matching implementation site.
4. Fix the bug using the ContextSniper MCP tool named `edit_file` rather than Claude's built-in `Edit` tool.
5. Re-run verification and ensure the relevant tests pass.

Final answer must include:

- root cause
- changed files
- verification command/output

## MCP Efficiency Policy (Turn/Token Reduction)

- Treat contextsniper `search_code` as a replacement for broad `grep` and exploratory `read`.
- Minimize extra search loops: prefer 1-2 high-quality `search_code` calls with focused symbol-level queries.
- If returned snippets already include the target file and useful line context, act directly:
  - edit the code immediately with the ContextSniper MCP tool named `edit_file`, or
  - run the next concrete action (patch/test) without additional broad reads.
- Avoid reading many unrelated files after a good hit; keep turns and token usage low.
- Only perform extra reads when required to verify safety or dependencies for the exact patch.
- Once the code is retrieved via `search_code`, skip the built-in 'Read'/'Edit' path and proceed directly to analyzing and editing with MCP tools.
