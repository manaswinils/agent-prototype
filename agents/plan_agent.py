"""Plan agent: explores a repo with Claude and writes plan.md.

The plan() function accepts an already-cloned repo_path so the caller
(pipeline.py) can share one clone across the PLAN and IMPLEMENT stages.

Usage (standalone):
    python plan_agent.py "add a /health endpoint" --repo manaswinils/agent-sandbox

Required env vars:
    ANTHROPIC_API_KEY
    GITHUB_TOKEN
    GITHUB_USERNAME
    GITHUB_REPO  (default if --repo not provided)
"""
import argparse
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# Standalone-execution support: add repo root to path when run as `python agents/plan_agent.py`
if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.tools import ToolExecutor

load_dotenv()

PLAN_MODEL = "claude-opus-4-6"
MAX_PLAN_ITERATIONS = 20

# Read-only tool schemas — no write_file or run_command in the exploration loop.
# This guarantees Claude fully explores before committing to a plan.
PLAN_TOOL_SCHEMAS = [
    {
        "name": "list_files",
        "description": "List files and directories at a given path inside the repo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path inside the repo. Use '.' for root."}
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the full contents of a file inside the repo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to the file from repo root."}
            },
            "required": ["path"],
        },
    },
]

PLAN_SYSTEM_PROMPT = """You are a senior software architect performing codebase analysis.
Your job: thoroughly explore the repository, then produce a structured implementation plan.

START by reading these living context documents — they capture accumulated project knowledge:
  - CLAUDE.md            project conventions, coding patterns, app structure, what NOT to do
  - docs/ARCHITECTURE.md current system components, routes, data flow, deployment topology
  - docs/TEST.md         test strategy, what is already tested, mocking patterns, coverage status
  - docs/DECISIONS.md    past architectural decisions and their rationale (learn from these)
  - docs/deploy.md       deployment configuration and infrastructure constraints

Then read targeted path-based rules for the files this goal will touch:
  - docs/rules/app.md        if the goal touches app.py (routes, client, error handling)
  - docs/rules/tests.md      if the goal touches tests/ (mocking, fixtures, coverage)
  - docs/rules/templates.md  if the goal touches templates/ (selectors, form, escaping)
  - docs/rules/harness.md    if the goal touches harness/ (scripts, fitness functions)

After reading context files, use list_files and read_file to explore source files
affected by the goal. When you have sufficient understanding, produce the plan.

Your plan must align with the existing conventions in CLAUDE.md and must not contradict
any active architectural decisions from docs/DECISIONS.md. If your plan introduces a significant
new design decision, note it in the Risks and Assumptions section.

Output ONLY the plan.md content — nothing before or after it. It must start exactly with:
# Implementation Plan: <goal verbatim>

Structure:

# Implementation Plan: <goal>

## Overview
2-3 sentences describing what will be done and why.

## Files to Create
| File | Purpose |
|------|---------|
| path/to/file.py | one-line description |

## Files to Modify
| File | Change Required |
|------|----------------|
| existing.py | describe the change |

## Implementation Approach
1. Numbered step-by-step implementation strategy.
2. Reference specific existing patterns from docs/ARCHITECTURE.md and CLAUDE.md.
3. Be specific about function names, template variables, and conventions to match.

## Test Strategy
- What to unit test (mock app.client — no real API calls in unit tests)
- What to functionally test via Flask test client
- E2E validation (which HTML selectors and user flows will be exercised)

## Risks and Assumptions
- Any new architectural decisions and their rationale
- Compatibility considerations with existing decisions from docs/DECISIONS.md
"""


def _summarize_input(tool_input: dict) -> str:
    parts = []
    for k, v in tool_input.items():
        s = str(v)
        if len(s) > 60:
            s = s[:60] + "..."
        parts.append(f"{k}={s!r}")
    return ", ".join(parts)


def run_plan_loop(client: Anthropic, executor: ToolExecutor, goal: str) -> str:
    """Run the Claude exploration + planning loop. Returns the raw plan content."""
    messages = [{"role": "user", "content": f"Goal: {goal}\n\nExplore the repo and produce plan.md."}]
    final_text = ""

    for iteration in range(1, MAX_PLAN_ITERATIONS + 1):
        print(f"[plan] iter {iteration} — calling Claude ...")
        response = client.messages.create(
            model=PLAN_MODEL,
            max_tokens=4096,
            system=PLAN_SYSTEM_PROMPT,
            tools=PLAN_TOOL_SCHEMAS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        for block in response.content:
            if block.type == "text" and block.text.strip():
                final_text = block.text.strip()

        if response.stop_reason == "end_turn":
            print("[plan] exploration complete.")
            return final_text

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"[plan] tool: {block.name}({_summarize_input(block.input)})")
                result = executor.dispatch(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        if not tool_results:
            print("[plan] no tools used and not end_turn — stopping.")
            return final_text

        messages.append({"role": "user", "content": tool_results})

    print(f"[plan] hit max iterations ({MAX_PLAN_ITERATIONS}).")
    return final_text


def plan(
    goal: str,
    token: str,
    repo_full_name: str,
    username: str,
    repo_path: Path,
) -> str:
    """
    Explore the cloned repo and produce plan.md inside it.

    Args:
        goal: Natural language goal.
        token: GitHub PAT (unused here, accepted for interface consistency).
        repo_full_name: e.g. 'owner/repo'.
        username: GitHub username.
        repo_path: Path to the already-cloned local repo.

    Returns:
        The plan content string (also written to repo_path/plan.md).
    """
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    executor = ToolExecutor(repo_path)

    print(f"[plan] exploring {repo_full_name} for goal: {goal[:80]}")
    raw = run_plan_loop(client, executor, goal)

    # Extract from the plan heading onward
    marker = "# Implementation Plan:"
    idx = raw.find(marker)
    if idx == -1:
        raise ValueError(
            f"plan.md missing '{marker}' heading. "
            f"Raw response (first 300 chars): {raw[:300]}"
        )
    plan_content = raw[idx:].strip()

    # Archive: write to plans/<timestamp>-<slug>.md — never overwrite older plans
    plans_dir = repo_path / "plans"
    plans_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", goal.lower())[:40].strip("-")
    archive_path = plans_dir / f"plan-{timestamp}-{slug}.md"
    archive_path.write_text(plan_content, encoding="utf-8")
    print(f"[plan] archived → plans/{archive_path.name} ({len(plan_content)} chars)")

    # Also write plan.md at root as "latest plan" pointer for agent backward-compat
    plan_path = repo_path / "plan.md"
    pointer = (
        f"<!-- Latest plan: plans/{archive_path.name} -->\n"
        f"<!-- This file is auto-updated. Full history in plans/ directory. -->\n\n"
        + plan_content
    )
    plan_path.write_text(pointer, encoding="utf-8")
    print(f"[plan] plan.md updated (→ plans/{archive_path.name})")

    # Add plans/ to .gitignore if not already present (prevent plan spam in PRs)
    # We keep plans/ committed so history is preserved — no gitignore addition

    return plan_content


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan agent — writes plan.md for a goal")
    parser.add_argument("goal", help="Natural language goal")
    parser.add_argument("--repo", default=None, help="owner/repo (default: GITHUB_REPO env var)")
    args = parser.parse_args()

    from agents.agent import clone_repo

    token = os.environ["GITHUB_TOKEN"]
    username = os.environ["GITHUB_USERNAME"]
    repo = args.repo or os.environ["GITHUB_REPO"]

    repo_path = clone_repo(repo, token, username)
    try:
        plan_content = plan(args.goal, token, repo, username, repo_path)
        print("\n" + "=" * 60)
        print(plan_content[:1000])
        if len(plan_content) > 1000:
            print(f"... ({len(plan_content)} chars total, see plan.md)")
    finally:
        shutil.rmtree(repo_path, ignore_errors=True)


if __name__ == "__main__":
    main()
