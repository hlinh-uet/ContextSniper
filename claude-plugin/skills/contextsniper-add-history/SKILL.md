---
name: contextsniper-add-history
description: Use the terminal command contextsniper-add-history to import Claude Code transcript history into ContextSniper.
---

# ContextSniper Add History

Use the terminal command:

```bash
claude-plugin/bin/contextsniper-add-history --dry-run
```

Show the user the dry-run totals first. Only run the import after explicit confirmation:

```bash
claude-plugin/bin/contextsniper-add-history --yes
```

Use this when the user asks to add/import project history or asks `/contextsniper-add-history`.
