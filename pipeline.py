"""Multi-stage agentic pipeline for the motivational quote app.

Stages:
  1. PLAN              — explore repo, write plan.md
  2. IMPLEMENT         — coding agent implements the goal, opens PR
  3. TEST              — generate + run tests locally
  4. REVIEW            — review agent reviews the PR
  5. RESOLVE COMMENTS  — coding agent addresses review feedback  (if needed)
  6. TEST AFTER RESOLVE— re-run tests on updated branch           (if needed)
  7. COMMIT            — squash-merge the PR to main
  8. DEPLOY            — deploy_agent reads deploy.md, builds + deploys

Stages 4-6 repeat up to --max-resolve times if the review requests changes.

Usage:
    python pipeline.py "add a /health endpoint" [--repo owner/repo] [--max-resolve 3]

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
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

from agent import (
    clone_repo,
    comment_on_pr,
    commit_and_push_existing_branch,
    commit_and_push_new_branch,
    fetch_pr_context,
    format_pr_context_for_prompt,
    merge_pr,
    open_pull_request,
    run_agent_loop,
)
from deploy_agent import deploy
from plan_agent import plan
from review_agent import review_pr
from tools import ToolExecutor

load_dotenv()

MAX_RESOLVE_ITERATIONS = 3

# Model for locally-generated tests (mirrors .agents/test_agent.py)
TEST_MODEL = "claude-sonnet-4-6"

SKIP_PATTERNS = {"tests/", ".agents/", "__pycache__", ".git", "venv/", ".venv/", "plan.md"}

TEST_SYSTEM_PROMPT = """You are an expert Python test engineer.
You write thorough pytest test suites that always mock external API calls so tests never touch the network.
Your response contains EXACTLY two fenced Python code blocks and nothing else — no prose, no explanation."""

TEST_USER_PROMPT = """Generate a complete pytest test suite for the Flask app below.

=== SOURCE FILES ===
{source_files}

=== APP BEHAVIOUR ===
- Flask app in app.py with `client = Anthropic()` at module level.
- GET  /  → renders empty form (templates/index.html).
- POST /  → reads form field "work" (stripped); if non-empty calls client.messages.create()
            and passes the quote to the template; on exception renders error message.

=== OUTPUT FORMAT ===
Produce exactly two Python code blocks:

```python
# tests/test_unit.py
\"\"\"Unit tests — mock app.client so no real API calls are made.\"\"\"
import pytest
from unittest.mock import MagicMock, patch
from app import app

@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c

# Include tests for: GET /, POST / with valid work, POST / with empty work,
# POST / with whitespace-only work, POST / when Anthropic raises Exception.

... complete unit test code here ...
```

```python
# tests/test_functional.py
\"\"\"Functional tests using Flask test client.\"\"\"
import pytest
from unittest.mock import MagicMock, patch
from app import app as flask_app

@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c

# Include tests for: GET / HTML structure, full POST flow with mock quote,
# POST with API failure, POST with empty work.

... complete functional test code here ...
```

Write real, complete code. Every test must have an assert."""


# ── stage header ──────────────────────────────────────────────────────────────

def stage(n: int, name: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"=== STAGE {n}: {name} ===")
    print(f"{'=' * 60}\n")


# ── pipeline state ────────────────────────────────────────────────────────────

@dataclass
class PipelineState:
    goal: str
    token: str
    repo_full_name: str
    username: str
    repo_path: Path | None = None
    branch: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    plan_content: str | None = None
    test_passed: bool = False
    review_verdict: str = "PENDING"
    resolve_iterations: int = 0
    deployed: bool = False


# ── local test stage ──────────────────────────────────────────────────────────

def _collect_source(repo_path: Path) -> str:
    """Read all testable .py files from a local clone."""
    parts: list[str] = []
    for py_file in sorted(repo_path.rglob("*.py")):
        rel = str(py_file.relative_to(repo_path))
        if any(skip in rel for skip in SKIP_PATTERNS):
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
            parts.append(f"=== {rel} ===\n{content}")
            print(f"[test] collected {rel} ({len(content)} chars)")
        except OSError as e:
            print(f"[test] could not read {rel}: {e}")
    return "\n\n".join(parts)


def _generate_tests(source_content: str) -> tuple[str, str]:
    """Ask Claude to generate unit + functional tests. Returns (unit_code, functional_code)."""
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = TEST_USER_PROMPT.format(source_files=source_content)

    print(f"[test] calling Claude {TEST_MODEL} for test generation ...")
    response = client.messages.create(
        model=TEST_MODEL,
        max_tokens=8192,
        system=TEST_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text
    print(f"[test] received response ({len(raw)} chars)")

    blocks = re.findall(r"```python\n(.*?)```", raw, re.DOTALL)
    print(f"[test] found {len(blocks)} code block(s)")

    unit_code = ""
    functional_code = ""
    for block in blocks:
        stripped = block.strip()
        first_line = stripped.split("\n")[0].lower()
        if "test_unit" in first_line or ("unit" in first_line and "functional" not in first_line):
            unit_code = stripped
        elif "test_functional" in first_line or "functional" in first_line:
            functional_code = stripped

    if not unit_code and not functional_code and len(blocks) >= 2:
        unit_code, functional_code = blocks[0].strip(), blocks[1].strip()
    elif not unit_code and len(blocks) >= 1:
        unit_code = blocks[0].strip()

    if not unit_code:
        raise ValueError("Could not extract unit test code from Claude response")
    if not functional_code:
        raise ValueError("Could not extract functional test code from Claude response")

    return unit_code, functional_code


def run_test_stage(repo_path: Path) -> bool:
    """
    Run tests against a local clone. Generates tests if tests/ is empty.
    Returns True if pytest exit code == 0. Failures are non-fatal to the pipeline.
    """
    tests_dir = repo_path / "tests"
    existing_tests = list(tests_dir.glob("test_*.py")) if tests_dir.exists() else []

    if not existing_tests:
        print("[test] no test files found — generating with Claude ...")
        source = _collect_source(repo_path)
        if not source:
            print("[test] no testable source files found; skipping test generation.")
            return True
        try:
            unit_code, functional_code = _generate_tests(source)
            tests_dir.mkdir(exist_ok=True)
            (tests_dir / "__init__.py").touch()
            (tests_dir / "test_unit.py").write_text(unit_code, encoding="utf-8")
            (tests_dir / "test_functional.py").write_text(functional_code, encoding="utf-8")
            print(f"[test] wrote tests/test_unit.py + tests/test_functional.py")
        except Exception as e:
            print(f"[test] test generation failed: {e}")
            return False

    # Install dependencies
    print("[test] installing dependencies ...")
    req_file = repo_path / "requirements.txt"
    if req_file.exists():
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", "requirements.txt",
             "pytest", "pytest-cov", "--quiet"],
            cwd=repo_path,
            check=False,
        )

    # Run pytest
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--cov=app",
         "--cov-report=term-missing", "-v"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    output = result.stdout + ("\n" + result.stderr if result.stderr else "")
    print(output[-3000:])
    print(f"[test] exit code: {result.returncode}")
    return result.returncode == 0


# ── pipeline ──────────────────────────────────────────────────────────────────

def run_pipeline(state: PipelineState, max_resolve: int = MAX_RESOLVE_ITERATIONS) -> None:
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # ── STAGE 1: PLAN ────────────────────────────────────────────────────────
    stage(1, "PLAN")
    state.repo_path = clone_repo(state.repo_full_name, state.token, state.username)
    state.plan_content = plan(
        goal=state.goal,
        token=state.token,
        repo_full_name=state.repo_full_name,
        username=state.username,
        repo_path=state.repo_path,
    )

    # ── STAGE 2: IMPLEMENT ───────────────────────────────────────────────────
    stage(2, "IMPLEMENT")
    goal_with_plan = (
        f"{state.goal}\n\n"
        f"A plan has been written to plan.md in the repo root. "
        f"Read it first with read_file, then implement accordingly.\n\n"
        f"Plan summary:\n{state.plan_content[:2000]}"
    )
    executor = ToolExecutor(state.repo_path)
    summary = run_agent_loop(client, executor, goal_with_plan)

    state.branch = f"agent/{uuid.uuid4().hex[:8]}"
    commit_message = f"Agent: {state.goal[:60]}\n\n{summary}"
    pushed = commit_and_push_new_branch(state.repo_path, state.branch, commit_message)

    if not pushed:
        print("[pipeline] Agent made no changes. Aborting pipeline.")
        sys.exit(1)

    state.pr_url = open_pull_request(
        state.token, state.repo_full_name, state.branch,
        title=f"Agent: {state.goal[:60]}",
        body=(
            f"**Goal:** {state.goal}\n\n"
            f"**Plan:**\n\n{state.plan_content}\n\n"
            f"**Summary:**\n{summary}\n\n"
            f"---\n_Opened by pipeline agent._"
        ),
    )
    state.pr_number = int(state.pr_url.rstrip("/").split("/")[-1])
    print(f"[pipeline] PR #{state.pr_number}: {state.pr_url}")

    # ── STAGE 3: TEST (first run) ────────────────────────────────────────────
    stage(3, "TEST")
    state.test_passed = run_test_stage(state.repo_path)
    print(f"[pipeline] Tests: {'PASSED' if state.test_passed else 'FAILED (non-fatal)'}")

    # ── STAGES 4-6: REVIEW / RESOLVE loop ───────────────────────────────────
    for iteration in range(1, max_resolve + 2):
        stage(4, f"REVIEW (iteration {iteration})")
        verdict = review_pr(state.token, state.repo_full_name, state.pr_number)
        state.review_verdict = verdict
        state.resolve_iterations = iteration

        if verdict == "APPROVE":
            print(f"[pipeline] PR #{state.pr_number} approved.")
            break

        if iteration > max_resolve:
            print(f"[pipeline] Max resolve iterations ({max_resolve}) reached without approval.")
            print("[pipeline] Aborting — manual intervention required.")
            sys.exit(1)

        stage(5, f"RESOLVE COMMENTS (iteration {iteration})")
        pr_ctx = fetch_pr_context(state.token, state.repo_full_name, state.pr_number)
        resolve_path = clone_repo(
            state.repo_full_name, state.token, state.username, branch=state.branch
        )
        try:
            resolve_executor = ToolExecutor(resolve_path)
            prompt = format_pr_context_for_prompt(pr_ctx, state.goal)
            resolve_summary = run_agent_loop(client, resolve_executor, prompt)
            commit_msg = (
                f"Agent: address PR #{state.pr_number} feedback "
                f"(iteration {iteration})\n\n{resolve_summary}"
            )
            pushed = commit_and_push_existing_branch(resolve_path, commit_msg)
            if pushed:
                comment_on_pr(
                    state.token, state.repo_full_name, state.pr_number,
                    f"🤖 Agent resolved comments (iteration {iteration}).\n\n"
                    f"**Summary:** {resolve_summary}",
                )
        finally:
            shutil.rmtree(resolve_path, ignore_errors=True)

        stage(6, f"TEST AFTER RESOLVE (iteration {iteration})")
        test_path = clone_repo(
            state.repo_full_name, state.token, state.username, branch=state.branch
        )
        try:
            state.test_passed = run_test_stage(test_path)
            print(f"[pipeline] Tests: {'PASSED' if state.test_passed else 'FAILED (non-fatal)'}")
        finally:
            shutil.rmtree(test_path, ignore_errors=True)

    # ── STAGE 7: COMMIT ──────────────────────────────────────────────────────
    stage(7, "COMMIT")
    merged = merge_pr(state.token, state.repo_full_name, state.pr_number)
    if not merged:
        print("[pipeline] WARN: merge may be pending auto-merge or failed.")

    # ── STAGE 8: DEPLOY ──────────────────────────────────────────────────────
    stage(8, "DEPLOY")
    deploy_path = clone_repo(state.repo_full_name, state.token, state.username)
    try:
        state.deployed = deploy(deploy_path)
    finally:
        shutil.rmtree(deploy_path, ignore_errors=True)

    if not state.deployed:
        print("[pipeline] WARN: deployment health check failed.")

    _print_summary(state)


def _print_summary(state: PipelineState) -> None:
    print(f"\n{'=' * 60}")
    print("=== PIPELINE COMPLETE ===")
    print(f"{'=' * 60}")
    print(f"Goal:      {state.goal}")
    print(f"PR:        {state.pr_url or 'N/A'}")
    print(f"Tests:     {'✅ PASSED' if state.test_passed else '⚠️  FAILED'}")
    print(f"Review:    {'✅ APPROVED' if state.review_verdict == 'APPROVE' else '❌ ' + state.review_verdict} "
          f"(iteration {state.resolve_iterations})")
    print(f"Merged:    {'✅' if state.deployed or state.review_verdict == 'APPROVE' else '⚠️  check manually'}")
    print(f"Deployed:  {'✅' if state.deployed else '❌ FAILED'}")
    print(f"{'=' * 60}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-stage agentic pipeline")
    parser.add_argument("goal", help="Natural language goal for the agent")
    parser.add_argument("--repo", default=None,
                        help="Target repo in owner/repo format (default: GITHUB_REPO env var)")
    parser.add_argument("--max-resolve", type=int, default=MAX_RESOLVE_ITERATIONS,
                        dest="max_resolve",
                        help=f"Max review/resolve iterations before aborting (default: {MAX_RESOLVE_ITERATIONS})")
    args = parser.parse_args()

    # Validate all env vars up front
    api_key = os.environ["ANTHROPIC_API_KEY"]  # noqa: F841
    token = os.environ["GITHUB_TOKEN"]
    username = os.environ["GITHUB_USERNAME"]
    repo = args.repo or os.environ["GITHUB_REPO"]

    state = PipelineState(
        goal=args.goal,
        token=token,
        repo_full_name=repo,
        username=username,
    )

    try:
        run_pipeline(state, max_resolve=args.max_resolve)
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n[pipeline] FATAL: {e}")
        raise
    finally:
        if state.repo_path and state.repo_path.exists():
            shutil.rmtree(state.repo_path, ignore_errors=True)


if __name__ == "__main__":
    main()
