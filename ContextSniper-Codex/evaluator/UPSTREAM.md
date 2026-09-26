# Vendored validation core

These modules are a self-contained copy of the minimal validation stack used by
`Debugging-Framework` at commit
`3845dfb4d31b1a3b73444b9478b34040006567f1`:

- `src/loaders/project.py` → `project.py`
- `src/environments/spec.py` → `environment_spec.py`
- `src/environments/oci.py` → `oci.py`
- `src/utils/project_config.py` → `project_config.py`
- `src/utils/workspace.py` → `workspace.py`
- `src/validation/project.py` → `validator.py`
- APR classification from `src/core/pipeline.py` → `outcome.py`
- Codex event usage from `src/core/retrieval.py` → `outcome.py`

Package import paths were changed in the copied validation modules. The macOS
OCI transport additionally excludes validator-created `.git` metadata while
copying source into a container; those files are not validation input and can
race with Git object packing. Keeping this snapshot inside ContextSniper makes
the baseline runnable after cloning or uploading this repository without
importing a sibling checkout.

The validation files also include the framework's validation-outcome update
from the local upstream working tree on 2026-09-23: every buildable candidate
runs both target and regression validation, and failing-test IDs must come from
an authoritative source before they can produce nonefix/noisefix/negfix labels.
