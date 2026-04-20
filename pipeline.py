"""Multi-stage agentic pipeline for the motivational quote app.

Stages:
  1.  PLAN                — plan_agent reads living docs, explores repo, writes plan.md
  2.  IMPLEMENT           — coding agent reads living docs + plan.md, implements, opens PR
  3.  TEST                — generate + run pytest tests locally (non-fatal)
  2.5 HARNESS             — lint (ruff/mypy) + fitness functions; results posted as PR comment
  3.  TEST                — generate + run pytest tests locally (non-fatal; replaces old stage 3)
  4.  REVIEW              — review_agent reads CLAUDE.md+ARCHITECTURE.md+TEST.md, reviews PR
  5.  RESOLVE COMMENTS    — coding agent addresses feedback, resolve_all_review_threads (if needed)
  6.  TEST AFTER RESOLVE  — re-run tests on updated branch (if needed)
  7.  BUILD + STAGING     — az acr build → deploy to staging Container App
  8.  E2E STAGING         — Puppeteer E2E tests vs live staging (real Anthropic API)
  9.  COMMIT              — squash-merge the PR to main
  10. UPDATE LIVING DOCS  — docs_agent updates ARCHITECTURE.md, TEST.md, DECISIONS.md, CLAUDE.md
  11. DEPLOY PROD         — deploy same image tag to prod Container App (no rebuild)
  12. E2E PROD            — Puppeteer E2E vs prod; fail → rollback + revert main + GitHub issue

Stages 4-6 repeat up to --max-resolve times. Pipeline enforces that ALL review threads
must be resolved before advancing to Stage 7 (staging deploy).

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
from datetime import datetime
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

from agents.agent import (
    clone_repo,
    comment_on_pr,
    commit_and_push_existing_branch,
    commit_and_push_new_branch,
    create_github_issue,
    fetch_pr_context,
    format_pr_context_for_prompt,
    get_open_review_thread_ids,
    merge_pr,
    open_pull_request,
    resolve_all_review_threads,
    revert_merge_on_main,
    run_agent_loop,
)
from agents.deploy_agent import build_image, deploy_to, generate_all_commands, read_deploy_md, rollback_to
from agents.docs_agent import update_living_docs, write_and_commit_docs
from agents.plan_agent import plan
from agents.review_agent import review_pr
from agents.tools import ToolExecutor

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
    impl_summary: str | None = None
    test_passed: bool = False
    review_verdict: str = "PENDING"
    resolve_iterations: int = 0
    # Deploy state (shared commands — build once, deploy to staging then prod)
    deploy_tag: str | None = None
    deploy_commands: dict | None = None
    # Staging
    staging_deployed: bool = False
    previous_staging_tag: str | None = None
    staging_e2e_passed: bool = False
    # Production
    prod_deployed: bool = False
    previous_prod_tag: str | None = None
    prod_e2e_passed: bool = False
    # Commit
    merged: bool = False
    failure_issue_url: str | None = None


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


# ── harness checks stage ──────────────────────────────────────────────────────

def run_harness_checks(repo_path: Path, token: str, repo_full_name: str, pr_number: int) -> dict:
    """
    Run lint.sh and fitness.py from the repo's harness/ directory.
    Posts results as a PR comment (non-blocking — pipeline continues regardless).
    Returns a dict with keys: lint_passed, fitness_passed, lint_output, fitness_output.
    Skips gracefully if harness/ scripts are absent.
    """
    results = {"lint_passed": True, "fitness_passed": True, "lint_output": "", "fitness_output": ""}

    lint_script = repo_path / "harness" / "lint.sh"
    fitness_script = repo_path / "harness" / "fitness.py"

    comment_parts: list[str] = ["## 🔍 Harness Report\n"]

    # Run lint
    if lint_script.exists():
        print("[harness] running lint.sh ...")
        try:
            # Install ruff + mypy if available
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "ruff", "mypy", "--quiet"],
                cwd=repo_path, check=False, capture_output=True,
            )
            lint_result = subprocess.run(
                ["bash", "harness/lint.sh"],
                cwd=repo_path, capture_output=True, text=True, timeout=120,
            )
            results["lint_output"] = lint_result.stdout + lint_result.stderr
            results["lint_passed"] = lint_result.returncode == 0
            status = "✅ PASSED" if results["lint_passed"] else "⚠️ ISSUES FOUND"
            comment_parts.append(
                f"### Lint ({status})\n```\n{results['lint_output'][-1500:]}\n```\n"
            )
        except Exception as e:
            print(f"[harness] lint failed with exception: {e}")
            comment_parts.append(f"### Lint\n⚠️ lint.sh error: {e}\n")
    else:
        print("[harness] harness/lint.sh not found — skipping lint")
        comment_parts.append("### Lint\n_harness/lint.sh not present in this repo_\n")

    # Run fitness functions
    if fitness_script.exists():
        print("[harness] running harness/fitness.py ...")
        try:
            fitness_result = subprocess.run(
                [sys.executable, "harness/fitness.py"],
                cwd=repo_path, capture_output=True, text=True, timeout=60,
            )
            results["fitness_output"] = fitness_result.stdout + fitness_result.stderr
            results["fitness_passed"] = fitness_result.returncode == 0
            status = "✅ PASSED" if results["fitness_passed"] else "⚠️ VIOLATIONS"
            comment_parts.append(
                f"### Architecture Fitness ({status})\n```\n{results['fitness_output'][-1500:]}\n```\n"
            )
        except Exception as e:
            print(f"[harness] fitness failed with exception: {e}")
            comment_parts.append(f"### Architecture Fitness\n⚠️ fitness.py error: {e}\n")
    else:
        print("[harness] harness/fitness.py not found — skipping fitness checks")
        comment_parts.append("### Architecture Fitness\n_harness/fitness.py not present in this repo_\n")

    overall = "✅ All harness checks passed" if (results["lint_passed"] and results["fitness_passed"]) \
        else "⚠️ Some harness checks found issues (non-blocking — pipeline continues)"
    comment_parts.append(f"\n---\n_{overall}_")

    # Post as PR comment
    try:
        comment_on_pr(token, repo_full_name, pr_number, "\n".join(comment_parts))
        print("[harness] results posted as PR comment")
    except Exception as e:
        print(f"[harness] could not post PR comment: {e}")

    print(f"[harness] lint={'PASSED' if results['lint_passed'] else 'ISSUES'}, "
          f"fitness={'PASSED' if results['fitness_passed'] else 'VIOLATIONS'}")
    return results


# ── E2E test stage ────────────────────────────────────────────────────────────

def run_e2e_tests(repo_path: Path, health_url: str, env_label: str) -> tuple[bool, list[str], str]:
    """
    Run Puppeteer E2E tests against health_url.
    env_label is "staging" or "prod" (for logging only).
    Returns (passed, issues_list, full_output).
    Skips gracefully (returns True) if tests/e2e/test_e2e.js is not present.
    """
    e2e_dir = repo_path / "tests" / "e2e"
    test_file = e2e_dir / "test_e2e.js"
    if not e2e_dir.exists() or not test_file.exists():
        print(f"[e2e-{env_label}] tests/e2e/test_e2e.js not found — skipping.")
        return True, [], ""

    print(f"[e2e-{env_label}] installing node dependencies ...")
    subprocess.run(
        ["npm", "install", "--prefer-offline", "--no-audit", "--no-fund"],
        cwd=str(e2e_dir),
        check=False,
        capture_output=True,
    )

    print(f"[e2e-{env_label}] running test_e2e.js against {health_url} ...")
    result = subprocess.run(
        ["node", "test_e2e.js", health_url],
        cwd=str(e2e_dir),
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = result.stdout + ("\n" + result.stderr if result.stderr else "")
    print(output[-4000:])
    print(f"[e2e-{env_label}] exit code: {result.returncode}")

    passed = result.returncode == 0
    issues: list[str] = []
    if not passed:
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("  ✗") or (stripped.startswith("[") and "]" in stripped):
                issues.append(stripped)

    return passed, issues, output


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
        f"Read these context files FIRST — they contain accumulated project knowledge:\n"
        f"  - CLAUDE.md                conventions, coding patterns, app structure, what NOT to do\n"
        f"  - docs/ARCHITECTURE.md     current components, routes, data flow, deployment topology\n"
        f"  - docs/TEST.md             test strategy, mocking patterns, what is already covered\n"
        f"  - docs/DECISIONS.md        past architectural decisions — respect and follow these\n"
        f"  - docs/deploy.md           deployment configuration\n"
        f"  - plan.md                  the implementation plan for this goal\n\n"
        f"Also read targeted rules for the files you plan to modify:\n"
        f"  - docs/rules/app.md        if touching app.py (routes, error handling, client usage)\n"
        f"  - docs/rules/tests.md      if touching tests/ (mocking, fixtures, coverage)\n"
        f"  - docs/rules/templates.md  if touching templates/ (selectors, form structure, escaping)\n"
        f"  - docs/rules/harness.md    if touching harness/ (script conventions, fitness checks)\n\n"
        f"Plan summary:\n{state.plan_content[:2000]}"
    )
    executor = ToolExecutor(state.repo_path)
    summary = run_agent_loop(client, executor, goal_with_plan)
    state.impl_summary = summary

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

    # ── STAGE 2.5: HARNESS (lint + fitness) ─────────────────────────────────
    print(f"\n{'=' * 60}\n=== STAGE 2.5: HARNESS — lint + fitness functions ===\n{'=' * 60}\n")
    run_harness_checks(state.repo_path, state.token, state.repo_full_name, state.pr_number)

    # ── STAGE 3: TEST (first run) ────────────────────────────────────────────
    stage(3, "TEST")
    state.test_passed = run_test_stage(state.repo_path)
    print(f"[pipeline] Tests: {'PASSED' if state.test_passed else 'FAILED (non-fatal)'}")

    # ── STAGES 4-6: REVIEW / RESOLVE loop ───────────────────────────────────
    # The pipeline will NOT advance to deployment until:
    #   (a) the review verdict is APPROVE, AND
    #   (b) all review threads are resolved (programmatically via GitHub GraphQL).
    #
    # If threads remain open after an APPROVE (reviewer approved but didn't click
    # "Resolve conversation"), they are resolved automatically before moving on.
    # If the verdict is REQUEST_CHANGES, the coding agent addresses the feedback,
    # commits the changes, and all addressed threads are resolved before re-review.
    #
    # If max_resolve iterations are exhausted without full approval + resolution,
    # the pipeline aborts — manual intervention is required.
    for iteration in range(1, max_resolve + 2):
        stage(4, f"REVIEW (iteration {iteration})")
        verdict = review_pr(state.token, state.repo_full_name, state.pr_number)
        state.review_verdict = verdict
        state.resolve_iterations = iteration

        if verdict == "APPROVE":
            # Verify all threads are resolved before advancing
            open_threads = get_open_review_thread_ids(
                state.token, state.repo_full_name, state.pr_number
            )
            if open_threads:
                print(
                    f"[pipeline] Approved but {len(open_threads)} thread(s) still open — "
                    f"resolving programmatically ..."
                )
                resolve_all_review_threads(
                    state.token, state.repo_full_name, state.pr_number
                )
            print(f"[pipeline] ✅ PR #{state.pr_number} approved — all threads resolved.")
            break

        # REQUEST_CHANGES path — check iteration limit first
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

        # Mark all addressed threads as resolved so re-review starts clean
        resolved_count = resolve_all_review_threads(
            state.token, state.repo_full_name, state.pr_number
        )
        print(f"[pipeline] resolved {resolved_count} review thread(s)")

        stage(6, f"TEST AFTER RESOLVE (iteration {iteration})")
        test_path = clone_repo(
            state.repo_full_name, state.token, state.username, branch=state.branch
        )
        try:
            state.test_passed = run_test_stage(test_path)
            print(f"[pipeline] Tests: {'PASSED' if state.test_passed else 'FAILED (non-fatal)'}")
        finally:
            shutil.rmtree(test_path, ignore_errors=True)

    # ── STAGE 7: BUILD + DEPLOY TO STAGING (from PR branch) ─────────────────
    stage(7, "BUILD + DEPLOY TO STAGING")
    staging_path = clone_repo(
        state.repo_full_name, state.token, state.username, branch=state.branch
    )
    try:
        deploy_md = read_deploy_md(staging_path)
        state.deploy_tag = datetime.now().strftime("%Y%m%d-%H%M%S")
        state.deploy_commands = generate_all_commands(deploy_md, state.deploy_tag)

        # Build image once — reused for both staging and prod
        if not build_image(state.deploy_commands, cwd=str(staging_path)):
            print("[pipeline] Build failed — aborting.")
            _print_summary(state)
            sys.exit(1)

        # Deploy to staging
        state.staging_deployed, state.previous_staging_tag = deploy_to(
            state.deploy_commands, "staging", cwd=str(staging_path)
        )
    finally:
        shutil.rmtree(staging_path, ignore_errors=True)

    if not state.staging_deployed:
        print("[pipeline] Staging deploy failed — aborting.")
        _print_summary(state)
        sys.exit(1)

    # ── STAGE 8: E2E TEST (STAGING) ──────────────────────────────────────────
    stage(8, "E2E TEST — STAGING")
    staging_e2e_path = clone_repo(
        state.repo_full_name, state.token, state.username, branch=state.branch
    )
    try:
        staging_url = state.deploy_commands["staging_health_url"]
        state.staging_e2e_passed, staging_issues, staging_output = run_e2e_tests(
            staging_e2e_path, staging_url, "staging"
        )
    finally:
        shutil.rmtree(staging_e2e_path, ignore_errors=True)

    if not state.staging_e2e_passed:
        print(f"\n[pipeline] Staging E2E FAILED — {len(staging_issues)} issue(s):")
        for issue in staging_issues:
            print(f"  {issue}")
        print("\n[pipeline] Rolling back staging ...")
        if state.previous_staging_tag:
            rollback_to(state.deploy_commands, "staging", state.previous_staging_tag)
        else:
            print("[pipeline] No previous staging tag — rollback skipped.")
        print("[pipeline] Code NOT merged to main. Fix issues and re-run the pipeline.")
        _print_summary(state)
        sys.exit(1)

    print("[pipeline] Staging E2E passed ✅")

    # ── STAGE 9: COMMIT (only after staging E2E passes) ──────────────────────
    stage(9, "COMMIT")
    state.merged = merge_pr(state.token, state.repo_full_name, state.pr_number)
    if not state.merged:
        print("[pipeline] WARN: merge may be pending auto-merge or failed.")

    # ── STAGE 9.5: UPDATE LIVING DOCS ───────────────────────────────────────
    # Non-fatal — doc update failure never blocks deployment.
    stage(10, "UPDATE LIVING DOCS")
    docs_path = clone_repo(state.repo_full_name, state.token, state.username)
    try:
        doc_updates = update_living_docs(
            docs_path,
            state.goal,
            state.impl_summary or "",
            state.pr_url or "",
            state.plan_content or "",
        )
        write_and_commit_docs(docs_path, doc_updates, state.pr_number, state.pr_url)
    except Exception as e:
        print(f"[pipeline] WARN: doc update failed (non-fatal): {e}")
    finally:
        shutil.rmtree(docs_path, ignore_errors=True)

    # ── STAGE 11: DEPLOY TO PROD (from main branch) ──────────────────────────
    stage(11, "DEPLOY TO PROD")
    prod_path = clone_repo(state.repo_full_name, state.token, state.username)
    try:
        state.prod_deployed, state.previous_prod_tag = deploy_to(
            state.deploy_commands, "prod", cwd=str(prod_path)
        )
    finally:
        shutil.rmtree(prod_path, ignore_errors=True)

    if not state.prod_deployed:
        print("[pipeline] Prod deploy/health check failed.")
        print("[pipeline] Main branch was merged but prod is unhealthy.")
        _print_summary(state)
        sys.exit(1)

    # ── STAGE 12: E2E TEST (PROD) ─────────────────────────────────────────────
    stage(12, "E2E TEST — PROD")
    prod_e2e_path = clone_repo(state.repo_full_name, state.token, state.username)
    try:
        prod_url = state.deploy_commands["prod_health_url"]
        state.prod_e2e_passed, prod_issues, prod_output = run_e2e_tests(
            prod_e2e_path, prod_url, "prod"
        )
    finally:
        shutil.rmtree(prod_e2e_path, ignore_errors=True)

    if not state.prod_e2e_passed:
        print(f"\n[pipeline] Prod E2E FAILED — {len(prod_issues)} issue(s):")
        for issue in prod_issues:
            print(f"  {issue}")

        # Rollback prod
        print("\n[pipeline] Rolling back production ...")
        if state.previous_prod_tag:
            rollback_to(state.deploy_commands, "prod", state.previous_prod_tag)
        else:
            print("[pipeline] No previous prod tag — rollback skipped.")

        # Revert main branch
        print("\n[pipeline] Reverting main branch ...")
        reverted, revert_pr_url = revert_merge_on_main(
            state.token, state.repo_full_name, state.pr_number, state.username
        )

        # Create GitHub issue with all failure details
        issue_title = f"🚨 Prod E2E failure: {state.goal[:60]} (PR #{state.pr_number})"
        issue_body = (
            f"## Production E2E Test Failure\n\n"
            f"**Goal:** {state.goal}\n"
            f"**PR:** {state.pr_url}\n"
            f"**Deploy tag:** `{state.deploy_tag}`\n\n"
            f"## Failed Tests\n\n"
            + "\n".join(f"- {i}" for i in prod_issues)
            + f"\n\n## Full E2E Output\n\n```\n{prod_output[-3000:]}\n```\n\n"
            f"## Actions Taken\n\n"
            f"- {'✅' if state.previous_prod_tag else '⚠️'} Prod rolled back"
            + (f" to `{state.previous_prod_tag}`" if state.previous_prod_tag else " (no previous tag)")
            + f"\n- {'✅' if reverted else '❌'} Main branch revert"
            + (f" PR: {revert_pr_url}" if revert_pr_url else " failed")
            + f"\n\n## Next Steps\n\n"
            f"1. Investigate the failures above\n"
            f"2. Fix the issue in a new branch\n"
            f"3. Re-run the pipeline\n"
        )
        state.failure_issue_url = create_github_issue(
            state.token, state.repo_full_name, issue_title, issue_body
        )
        print(f"\n[pipeline] GitHub issue: {state.failure_issue_url}")
        _print_summary(state)
        sys.exit(1)

    print("[pipeline] Prod E2E passed ✅")
    _print_summary(state)


def _print_summary(state: PipelineState) -> None:
    tag = state.deploy_tag or "N/A"
    print(f"\n{'=' * 60}")
    print("=== PIPELINE COMPLETE ===")
    print(f"{'=' * 60}")
    print(f"Goal:          {state.goal}")
    print(f"PR:            {state.pr_url or 'N/A'}")
    print(f"Unit Tests:    {'✅ PASSED' if state.test_passed else '⚠️  FAILED'}")
    print(f"Review:        {'✅ APPROVED' if state.review_verdict == 'APPROVE' else '❌ ' + state.review_verdict} "
          f"(iteration {state.resolve_iterations})")
    print(f"Staging:       {'✅ deployed' if state.staging_deployed else '❌ FAILED'} "
          f"({'E2E ✅' if state.staging_e2e_passed else 'E2E ❌'})")
    print(f"Merged:        {'✅' if state.merged else '❌ NOT merged'}")
    print(f"Prod:          {'✅ deployed' if state.prod_deployed else '❌ FAILED'} "
          f"({'E2E ✅' if state.prod_e2e_passed else 'E2E ❌ (rolled back)'})")
    print(f"Tag:           {tag}")
    if state.deploy_commands:
        print(f"Staging URL:   {state.deploy_commands.get('staging_health_url', 'N/A')}")
        print(f"Prod URL:      {state.deploy_commands.get('prod_health_url', 'N/A')}")
    if state.failure_issue_url:
        print(f"Issue:         {state.failure_issue_url}")
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
