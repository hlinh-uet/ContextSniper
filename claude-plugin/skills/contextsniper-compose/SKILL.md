---
name: contextsniper-compose
description: Use the terminal command contextsniper-compose to retrieve composed ContextSniper context.
---

# ContextSniper Compose

Use the terminal command:

```bash
claude-plugin/bin/contextsniper-compose "<query>"
```

Use this when the user asks to recall previous decisions, search memory, or asks `/contextsniper-compose`.
Summarize the returned sections clearly. If nothing is found, suggest a narrower query.
