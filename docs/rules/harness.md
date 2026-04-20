# Rules: Harness Scripts (harness/)

Apply these rules when modifying or adding files in `harness/`.

## Purpose
The `harness/` directory contains the testing and quality harness — scripts that run
on every PR to catch issues before code ships. These are not application code.

## Shell scripts (*.sh)
- Start with `#!/usr/bin/env bash` and `set -euo pipefail`.
- Use `REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"` for portability.
- Exit code 0 = success; non-zero = failure.
- Print progress with `echo "=== [tag] message ==="` format.
- Skip gracefully (exit 0 with message) if dependencies are not installed.

## Python harness scripts (fitness.py, etc.)
- Must be runnable standalone: `python harness/fitness.py`.
- Must have `if __name__ == "__main__":` block.
- Exit code 0 = all checks passed; 1 = violations found.
- Each check returns `list[str]` — empty = passed, non-empty = violations with file:line refs.
- All violations must include `filepath:lineno` for IDE navigation.

## Non-blocking by default
- Harness failures post warnings as PR comments but do NOT abort the pipeline.
- The `--strict` flag (future) will make failures fatal.
- This allows gradual adoption: existing code gets warnings, new code gets enforcement.

## Dependencies
- Harness tools (ruff, mypy, mutmut) are in `requirements.txt` under the `# harness` section.
- Do not introduce harness tools that require system-level installation (e.g. apt packages).
- Prefer pure-Python tools that install via pip.

## Adding a new check
1. Add the function to `harness/fitness.py` returning `list[str]`.
2. Add it to the `run_all()` dict in `fitness.py`.
3. Document the rule it enforces in `docs/DECISIONS.md` as a new ADR.
4. Update this file if the check has special conventions.
