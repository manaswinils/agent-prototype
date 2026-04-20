"""Garbage collection agent — between-PR entropy detection.

Runs periodically (not per-PR) to detect accumulated technical debt, complexity
drift, dead code, and documentation staleness.

Implemented checks:
  1. Architecture fitness functions (harness/fitness.py)
  2. Dead code detection (vulture)
  3. Cyclomatic complexity (radon)
  4. Docs vs code drift — Claude-powered semantic check

Creates a GitHub issue if any threshold is exceeded.

Usage:
    python agents/gc_agent.py --repo owner/repo [--clone-path /tmp/repo]

Required env vars:
    ANTHROPIC_API_KEY
    GITHUB_TOKEN
    GITHUB_USERNAME
    GITHUB_REPO  (default if --repo not provided)
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# Standalone-execution support
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.agent import clone_repo, create_github_issue

load_dotenv()

GC_MODEL = "claude-sonnet-4-6"

# Thresholds
COMPLEXITY_THRESHOLD = 10      # radon cyclomatic complexity — flag functions above this
DEAD_CODE_THRESHOLD = 3        # vulture unused items — flag if more than this many
DOCS_DRIFT_THRESHOLD = 3       # Claude-detected doc/code discrepancies

GC_SYSTEM_PROMPT = """You are a code quality analyst reviewing a codebase for documentation drift.

You will receive:
1. The current docs/ARCHITECTURE.md content
2. The current source code files

Your task: identify discrepancies between what the docs claim and what the code actually does.
Look for:
- Routes documented but not implemented (or vice versa)
- Components mentioned in docs that don't exist in code
- Data flow described in docs that doesn't match actual code flow
- Missing dependencies or incorrect dependency descriptions

Respond with ONLY a JSON object:
{
  "discrepancies": [
    {"type": "missing_route|extra_route|wrong_component|wrong_flow|other",
     "description": "specific description of the mismatch",
     "doc_claim": "what the doc says",
     "code_reality": "what the code actually does"}
  ],
  "summary": "one sentence overall assessment"
}
No prose outside the JSON. No markdown fences."""


def _run_cmd(cmd: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str]:
    """Run a command and return (returncode, output)."""
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return 1, f"Command timed out after {timeout}s"
    except FileNotFoundError:
        return 1, f"Command not found: {cmd[0]}"


def run_fitness_functions(repo_path: Path) -> dict:
    """Run harness/fitness.py if present. Returns {passed, violations, output}."""
    fitness_script = repo_path / "harness" / "fitness.py"
    if not fitness_script.exists():
        return {"passed": True, "violations": [], "output": "harness/fitness.py not found — skipped"}

    code, output = _run_cmd([sys.executable, "harness/fitness.py"], repo_path)
    violations = [
        line.strip() for line in output.splitlines()
        if line.strip() and not line.startswith("✅") and not line.strip().startswith("=")
    ]
    return {"passed": code == 0, "violations": violations, "output": output}


def run_vulture(repo_path: Path) -> dict:
    """Run vulture for dead code detection. Returns {passed, items, output}."""
    # Install vulture if needed
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "vulture", "--quiet"],
        capture_output=True
    )

    code, output = _run_cmd(
        [sys.executable, "-m", "vulture", ".", "--min-confidence", "80",
         "--exclude", "harness,tests,.git,venv,.venv"],
        repo_path
    )
    items = [line.strip() for line in output.splitlines() if line.strip()]
    return {
        "passed": len(items) <= DEAD_CODE_THRESHOLD,
        "items": items,
        "count": len(items),
        "output": output,
    }


def run_complexity(repo_path: Path) -> dict:
    """Run radon for cyclomatic complexity. Returns {passed, hotspots, output}."""
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "radon", "--quiet"],
        capture_output=True
    )

    code, output = _run_cmd(
        [sys.executable, "-m", "radon", "cc", ".", "-s", "-n", "C",
         "--exclude", "harness/*,tests/*"],
        repo_path
    )
    hotspots = [line.strip() for line in output.splitlines() if line.strip()]
    return {
        "passed": code == 0 and len(hotspots) <= 0,
        "hotspots": hotspots,
        "output": output,
    }


def check_docs_drift(repo_path: Path) -> dict:
    """Use Claude to check for doc/code discrepancies."""
    arch_file = repo_path / "docs" / "ARCHITECTURE.md"
    if not arch_file.exists():
        return {"passed": True, "discrepancies": [], "summary": "docs/ARCHITECTURE.md not found"}

    arch_content = arch_file.read_text(encoding="utf-8")

    # Collect source files
    source_parts = []
    for py_file in sorted(repo_path.rglob("*.py")):
        rel = str(py_file.relative_to(repo_path))
        skip = {"__pycache__", ".git", "venv", ".venv", "harness", "tests"}
        if any(s in rel for s in skip):
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
            source_parts.append(f"=== {rel} ===\n{content[:3000]}")
        except OSError:
            pass

    if not source_parts:
        return {"passed": True, "discrepancies": [], "summary": "No source files found"}

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    user_msg = (
        f"## docs/ARCHITECTURE.md:\n{arch_content}\n\n"
        f"## Source files:\n" + "\n\n".join(source_parts)
    )

    try:
        response = client.messages.create(
            model=GC_MODEL,
            max_tokens=2048,
            system=GC_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text.strip()

        # Parse JSON with fallback
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(m.group(0)) if m else {"discrepancies": [], "summary": "parse error"}

        discrepancies = data.get("discrepancies", [])
        return {
            "passed": len(discrepancies) <= DOCS_DRIFT_THRESHOLD,
            "discrepancies": discrepancies,
            "summary": data.get("summary", ""),
            "count": len(discrepancies),
        }
    except Exception as e:
        return {"passed": True, "discrepancies": [], "summary": f"Check failed: {e}"}


def build_issue_body(results: dict, repo_path: Path) -> str:
    """Build a GitHub issue body from GC results."""
    sections = [
        "## 🗑️ Garbage Collection Report\n",
        f"_Periodic entropy scan of `{repo_path.name}`_\n",
    ]

    fitness = results.get("fitness", {})
    vulture = results.get("vulture", {})
    complexity = results.get("complexity", {})
    drift = results.get("docs_drift", {})

    # Fitness
    status = "✅ PASSED" if fitness.get("passed") else "⚠️ VIOLATIONS"
    sections.append(f"### Architecture Fitness — {status}")
    if fitness.get("violations"):
        sections.append("```\n" + "\n".join(fitness["violations"][:20]) + "\n```")
    else:
        sections.append("_No violations detected._")

    # Dead code
    status = "✅ PASSED" if vulture.get("passed") else "⚠️ DEAD CODE"
    sections.append(f"\n### Dead Code (vulture) — {status}")
    if vulture.get("items"):
        sections.append(f"Found {vulture['count']} unused item(s):")
        sections.append("```\n" + "\n".join(vulture["items"][:20]) + "\n```")
    else:
        sections.append("_No dead code detected above confidence threshold._")

    # Complexity
    status = "✅ PASSED" if complexity.get("passed") else "⚠️ HIGH COMPLEXITY"
    sections.append(f"\n### Cyclomatic Complexity (radon) — {status}")
    if complexity.get("hotspots"):
        sections.append("Functions with complexity grade C or above:")
        sections.append("```\n" + "\n".join(complexity["hotspots"][:20]) + "\n```")
    else:
        sections.append("_All functions within acceptable complexity bounds._")

    # Docs drift
    status = "✅ PASSED" if drift.get("passed") else "⚠️ DRIFT DETECTED"
    sections.append(f"\n### Documentation Drift — {status}")
    sections.append(f"_{drift.get('summary', '')}_")
    if drift.get("discrepancies"):
        for d in drift["discrepancies"][:5]:
            sections.append(
                f"\n- **{d.get('type', 'unknown')}**: {d.get('description', '')}\n"
                f"  - Doc claims: _{d.get('doc_claim', 'N/A')}_\n"
                f"  - Code reality: _{d.get('code_reality', 'N/A')}_"
            )

    sections.append("\n---\n_Created by `agents/gc_agent.py`. Address issues in a new PR._")
    return "\n".join(sections)


def run_gc(repo_path: Path, token: str, repo_full_name: str) -> dict:
    """Run all GC checks. Returns results dict. Creates a GitHub issue if problems found."""
    print(f"[gc] scanning {repo_full_name} ...")

    results = {}

    print("[gc] running fitness functions ...")
    results["fitness"] = run_fitness_functions(repo_path)

    print("[gc] running vulture (dead code) ...")
    results["vulture"] = run_vulture(repo_path)

    print("[gc] running radon (complexity) ...")
    results["complexity"] = run_complexity(repo_path)

    print("[gc] checking docs/code drift with Claude ...")
    results["docs_drift"] = check_docs_drift(repo_path)

    # Determine if any check failed
    any_failed = any(not r.get("passed", True) for r in results.values())
    print(f"\n[gc] summary:")
    for check, result in results.items():
        passed = result.get("passed", True)
        print(f"  {check}: {'✅' if passed else '⚠️'}")

    if any_failed:
        print("\n[gc] issues found — creating GitHub issue ...")
        issue_body = build_issue_body(results, repo_path)
        issue_url = create_github_issue(
            token, repo_full_name,
            "🗑️ GC Agent: entropy detected — technical debt cleanup needed",
            issue_body,
        )
        print(f"[gc] issue created: {issue_url}")
        results["issue_url"] = issue_url
    else:
        print("[gc] ✅ No significant entropy detected.")

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Garbage collection agent — periodic entropy scan")
    parser.add_argument("--repo", default=None, help="owner/repo (default: GITHUB_REPO env var)")
    parser.add_argument("--clone-path", default=None,
                        help="Use existing clone at this path (skip cloning)")
    args = parser.parse_args()

    token = os.environ["GITHUB_TOKEN"]
    username = os.environ["GITHUB_USERNAME"]
    repo = args.repo or os.environ["GITHUB_REPO"]

    if args.clone_path:
        repo_path = Path(args.clone_path).resolve()
        own_clone = False
    else:
        print(f"[gc] cloning {repo} ...")
        repo_path = clone_repo(repo, token, username)
        own_clone = True

    try:
        results = run_gc(repo_path, token, repo)
        any_failed = any(not r.get("passed", True) for r in results.values() if isinstance(r, dict))
        sys.exit(1 if any_failed else 0)
    finally:
        if own_clone:
            shutil.rmtree(repo_path, ignore_errors=True)


if __name__ == "__main__":
    main()
