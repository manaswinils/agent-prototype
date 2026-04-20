"""Living document updater.

Called by pipeline.py after a successful merge to update all living context
documents in the target repo based on the changes just made.

Documents maintained:
  ARCHITECTURE.md  — system components, routes, data flow, integrations, deployment
  TEST.md          — test strategy, coverage status, patterns, known gaps
  DECISIONS.md     — architectural decisions log (prepend new ADRs, keep existing)
  CLAUDE.md        — project conventions for AI agents (app structure section only)

Usage (standalone):
    python docs_agent.py --repo-path /path/to/clone --goal "add dark mode" \\
        --summary "Added CSS variables and a toggle route" --pr-url https://...

Or called from pipeline.py:
    from docs_agent import update_living_docs, write_and_commit_docs
"""
import argparse
import json
import os
import re
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv
from git import Repo

load_dotenv()

DOCS_MODEL = "claude-sonnet-4-6"

# Living docs to maintain — paths relative to repo root.
# ARCHITECTURE/TEST/DECISIONS live in docs/; CLAUDE.md stays at root (Claude Code needs it there).
LIVING_DOCS = ["docs/ARCHITECTURE.md", "docs/TEST.md", "docs/DECISIONS.md", "CLAUDE.md"]

# Source files to read for doc context — patterns relative to repo root
SOURCE_PATTERNS = ["*.py", "requirements.txt", "Dockerfile", "Procfile"]
SOURCE_SKIP = {"__pycache__", ".git", "venv", ".venv", ".agents", "node_modules"}
MAX_SOURCE_FILE_CHARS = 3000   # truncate large source files to keep prompt manageable
MAX_TEST_FILE_CHARS   = 1500

DOCS_SYSTEM_PROMPT = """You are a technical writer maintaining living documentation for a software project.

You receive:
1. Current content of each documentation file (or "(new file)" if it does not exist yet)
2. Key source files from the project
3. A summary of what was just changed — goal, plan, and implementation details

Your job: produce an updated version of EVERY documentation file that accurately reflects
the CURRENT state of the project after this change.

Rules:
- Keep existing content that is still accurate; update only what changed
- docs/ARCHITECTURE.md: update routes table, components, data flow, and deployment topology if changed
- docs/TEST.md: update the "What is tested" list, coverage notes, and E2E test descriptions if changed
- docs/DECISIONS.md: prepend a new ADR entry (ADR-NNN) for any significant design decision made in
  this change; keep all existing entries unchanged; number sequentially from existing highest
- CLAUDE.md: update the App structure table and CI/CD section if new files or routes were added;
  do NOT change the conventions or "What NOT to do" sections
- Be specific and factual; no marketing language; no invented information
- If nothing changed for a particular doc, return the exact existing content unchanged

Respond with ONLY a valid JSON object mapping each filename to its complete updated content.
Use the exact keys: "docs/ARCHITECTURE.md", "docs/TEST.md", "docs/DECISIONS.md", "CLAUDE.md"
{"docs/ARCHITECTURE.md": "# Architecture...", "docs/TEST.md": "...", "docs/DECISIONS.md": "...", "CLAUDE.md": "..."}
No prose outside the JSON. No markdown fences wrapping the JSON."""


# ── helpers ────────────────────────────────────────────────────────────────────

def _parse_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Could not parse JSON from docs agent response:\n{text[:500]}")


def _collect_source_files(repo_path: Path) -> str:
    """Read key source files from the repo for context."""
    parts: list[str] = []
    for pattern in SOURCE_PATTERNS:
        for fpath in sorted(repo_path.rglob(pattern)):
            rel = str(fpath.relative_to(repo_path))
            if any(skip in rel for skip in SOURCE_SKIP):
                continue
            try:
                content = fpath.read_text(encoding="utf-8")
                limit = MAX_TEST_FILE_CHARS if "test" in rel.lower() else MAX_SOURCE_FILE_CHARS
                truncated = content[:limit]
                if len(content) > limit:
                    truncated += f"\n... (truncated, {len(content)} chars total)"
                parts.append(f"=== {rel} ===\n{truncated}")
            except OSError:
                pass
    return "\n\n".join(parts)


def _read_current_docs(repo_path: Path) -> dict[str, str]:
    """Read current content of all living docs (or placeholder if absent)."""
    docs: dict[str, str] = {}
    for fname in LIVING_DOCS:
        fpath = repo_path / fname
        if fpath.exists():
            docs[fname] = fpath.read_text(encoding="utf-8")
        else:
            docs[fname] = "(new file — create from scratch based on the source files)"
    return docs


# ── core functions ─────────────────────────────────────────────────────────────

def update_living_docs(
    repo_path: Path,
    goal: str,
    impl_summary: str,
    pr_url: str,
    plan_content: str = "",
) -> dict[str, str]:
    """
    Ask Claude to update all living docs based on the changes just landed.

    Args:
        repo_path:    Local clone of the repo (main branch after merge).
        goal:         The pipeline goal string.
        impl_summary: Coding agent summary of changes made.
        pr_url:       URL of the PR that was just merged.
        plan_content: Content of plan.md from the pipeline run (optional).

    Returns:
        Dict mapping filename → updated content for each doc that changed.
    """
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    current_docs = _read_current_docs(repo_path)
    source_files = _collect_source_files(repo_path)

    docs_section = "\n\n".join(
        f"### Current {fname}:\n{content}"
        for fname, content in current_docs.items()
    )

    user_message = (
        f"## What just changed\n\n"
        f"**Goal:** {goal}\n"
        f"**PR:** {pr_url}\n\n"
        f"**Implementation summary:**\n{impl_summary}\n\n"
        + (f"**Plan:**\n{plan_content[:2000]}\n\n" if plan_content else "")
        + f"## Current documentation (update these)\n\n{docs_section}\n\n"
        f"## Current source files (use for accuracy)\n\n{source_files}"
    )

    print(f"[docs] calling Claude {DOCS_MODEL} to update living docs ...")
    response = client.messages.create(
        model=DOCS_MODEL,
        max_tokens=8192,
        system=DOCS_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    raw = response.content[0].text
    print(f"[docs] received response ({len(raw)} chars)")

    try:
        updates = _parse_json(raw)
    except ValueError as e:
        print(f"[docs] failed to parse response: {e}")
        return {}

    # Keep only known doc files with string content
    valid = {k: v for k, v in updates.items() if k in LIVING_DOCS and isinstance(v, str) and v.strip()}
    print(f"[docs] updates for: {list(valid.keys())}")
    return valid


def write_and_commit_docs(
    repo_path: Path,
    updates: dict[str, str],
    pr_number: int | None = None,
    pr_url: str = "",
) -> bool:
    """
    Write updated docs to repo_path and commit + push on the current branch.
    Returns True if files were written and committed.
    """
    if not updates:
        print("[docs] no doc updates to write")
        return False

    written: list[str] = []
    for fname, content in updates.items():
        fpath = repo_path / fname
        fpath.parent.mkdir(parents=True, exist_ok=True)  # ensure docs/ exists
        fpath.write_text(content, encoding="utf-8")
        print(f"[docs] wrote {fname} ({len(content)} chars)")
        written.append(fname)

    if not written:
        return False

    git_repo = Repo(repo_path)
    for fname in written:
        git_repo.index.add([fname])

    if not git_repo.is_dirty(index=True):
        print("[docs] no doc changes detected — nothing to commit")
        return False

    pr_ref = f" after PR #{pr_number}" if pr_number else ""
    commit_msg = (
        f"[docs] update living documents{pr_ref}\n\n"
        f"Updated: {', '.join(written)}"
        + (f"\nPR: {pr_url}" if pr_url else "")
    )
    git_repo.index.commit(commit_msg)
    current_branch = git_repo.active_branch.name
    print(f"[docs] pushing doc updates to {current_branch} ...")
    git_repo.git.push("origin", current_branch)
    print(f"[docs] ✅ docs pushed: {written}")
    return True


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Living docs updater")
    parser.add_argument("--repo-path", required=True,
                        help="Path to local clone of the target repo (main branch)")
    parser.add_argument("--goal", required=True, help="Goal that was just implemented")
    parser.add_argument("--summary", default="", help="Implementation summary")
    parser.add_argument("--pr-url", default="", help="URL of the merged PR")
    parser.add_argument("--plan", default="", help="Plan content (optional)")
    args = parser.parse_args()

    repo_path = Path(args.repo_path).resolve()
    updates = update_living_docs(repo_path, args.goal, args.summary, args.pr_url, args.plan)
    written = write_and_commit_docs(repo_path, updates, pr_url=args.pr_url)
    raise SystemExit(0 if written else 1)


if __name__ == "__main__":
    main()
