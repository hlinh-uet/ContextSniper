---
name: contextsniper-add-history
description: 使用终端命令 contextsniper-add-history 将 Claude Code transcript 历史导入 ContextSniper。
---

# ContextSniper 添加历史

使用终端命令：

```bash
claude-plugin/bin/contextsniper-add-history --dry-run
```

先向用户展示 dry-run 汇总结果。只有在用户明确确认后，才执行真正导入：

```bash
claude-plugin/bin/contextsniper-add-history --yes
```

当用户要求添加/导入项目历史，或请求 `/contextsniper-add-history` 时使用本技能。
