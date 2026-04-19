"""AI code review agent using Claude claude-opus-4-6.

Usage:
    python review_agent.py --pr 42

Required env vars:
    ANTHROPIC_API_KEY
    GITHUB_TOKEN
    GITHUB_REPO   e.g. manaswinils/agent-sandbox
"""
import argparse
import json
import os
import re
import sys

from anthropic import Anthropic
from dotenv import load_dotenv
from github import Github, GithubException

load_dotenv()

REVIEW_MODEL = "claude-opus-4-6"

SYSTEM_PROMPT = """You are a senior software engineer conducting a thorough pull request review.
You respond ONLY with a single JSON object — no markdown, no prose, no code fences.
The JSON must conform exactly to this schema:
{
  "summary": "<2-3 sentence overall assessment>",
  "inline_comments": [
    {"file": "<relative path>", "line": <integer new-file line>, "comment": "<actionable feedback>"}
  ],
  "verdict": "APPROVE" or "REQUEST_CHANGES"
}
Rules:
- Line numbers must be new-file line numbers visible in the diff (lines starting with +).
- Limit inline_comments to the 10 most important issues.
- Check for: security vulnerabilities, logic errors, missing error handling, style issues,
  hardcoded secrets, missing input validation, and framework best practices.
- Verdict is APPROVE only when the code is correct, secure, and follows best practices.
- Verdict is REQUEST_CHANGES for any significant security flaw, logic error, or missing error handling.
"""


# ── diff utilities ────────────────────────────────────────────────────────────

def parse_valid_new_lines(patch: str) -> set[int]:
    """Return the set of new-file line numbers present in a diff patch."""
    if not patch:
        return set()
    valid: set[int] = set()
    new_line = 0
    for raw in patch.split("\n"):
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            if m:
                new_line = int(m.group(1)) - 1
        elif raw.startswith("+"):
            new_line += 1
            valid.add(new_line)
        elif raw.startswith("-"):
            pass  # deleted line — no new-file number
        else:
            new_line += 1  # context line
    return valid


def build_diff_content(pr) -> tuple[str, dict[str, set[int]]]:
    """
    Returns (diff_text, valid_lines_by_file).
    diff_text is a human-readable concatenation of all file patches.
    valid_lines_by_file maps filename -> set of new-file line numbers in the diff.
    """
    parts: list[str] = []
    valid_lines: dict[str, set[int]] = {}

    for f in pr.get_files():
        patch = f.patch or ""
        valid_lines[f.filename] = parse_valid_new_lines(patch)
        parts.append(
            f"### {f.filename} ({f.status}, +{f.additions}/-{f.deletions})\n"
            f"```diff\n{patch}\n```"
        )

    return "\n\n".join(parts), valid_lines


# ── JSON parsing ──────────────────────────────────────────────────────────────

def parse_review_json(text: str) -> dict:
    """Parse Claude's response into a review dict. Three-tier fallback."""
    text = text.strip()

    # Tier 1: raw JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Tier 2: strip ```json ... ``` or ``` ... ``` fences
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Tier 3: find the first { ... } blob in the text
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse JSON from Claude response:\n{text[:500]}")


# ── GitHub interactions ───────────────────────────────────────────────────────

def post_inline_comments(
    pr,
    head_sha: str,
    inline_comments: list[dict],
    valid_lines: dict[str, set[int]],
) -> int:
    """Post inline review comments. Returns count of successfully posted comments."""
    posted = 0
    for item in inline_comments:
        file_path = item.get("file", "")
        line = item.get("line")
        comment = item.get("comment", "")

        if not file_path or not line or not comment:
            print(f"[review] skipping malformed comment: {item}")
            continue

        allowed = valid_lines.get(file_path, set())
        if line not in allowed:
            if allowed:
                nearest = min(allowed, key=lambda l: abs(l - line))
                print(f"[review] line {line} not in diff for {file_path}, using nearest {nearest}")
                line = nearest
            else:
                print(f"[review] {file_path} has no diff lines, skipping inline comment")
                continue

        try:
            pr.create_review_comment(
                body=comment,
                commit=head_sha,
                path=file_path,
                line=line,
                side="RIGHT",
            )
            posted += 1
            print(f"[review] inline comment → {file_path}:{line}")
        except GithubException as e:
            print(f"[review] could not post inline comment on {file_path}:{line}: {e}")

    return posted


def submit_review(pr, summary: str, verdict: str) -> None:
    """Submit the overall review (APPROVE or REQUEST_CHANGES)."""
    event = "APPROVE" if verdict == "APPROVE" else "REQUEST_CHANGES"
    pr.create_review(body=f"🤖 **AI Code Review**\n\n{summary}", event=event)
    print(f"[review] submitted {event} review")


def enable_automerge(pr, pr_number: int) -> None:
    """Enable squash auto-merge via PyGithub; fall back to direct merge."""
    try:
        pr.enable_automerge(merge_method="SQUASH")
        print(f"[review] auto-merge (squash) enabled for PR #{pr_number}")
    except GithubException as e:
        print(f"[review] enable_automerge not available ({e.status}); trying direct merge")
        try:
            result = pr.merge(
                merge_method="squash",
                commit_title=f"Auto-merge PR #{pr_number} (AI review approved)",
            )
            if result.merged:
                print(f"[review] PR #{pr_number} merged directly")
            else:
                print(f"[review] merge returned merged=False: {result.message}")
        except GithubException as e2:
            print(f"[review] direct merge also failed: {e2}")
            print("[review] enable branch protection auto-merge in repo settings, or merge manually.")


# ── core review function ──────────────────────────────────────────────────────

def review_pr(token: str, repo_full_name: str, pr_number: int) -> str:
    """Run the full review pipeline. Returns the verdict string."""
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    gh = Github(token)
    gh_repo = gh.get_repo(repo_full_name)
    pr = gh_repo.get_pull(pr_number)

    print(f"[review] PR #{pr_number}: {pr.title}")
    print(f"[review] head SHA: {pr.head.sha}")

    diff_content, valid_lines = build_diff_content(pr)

    if not diff_content.strip():
        print("[review] no diff content found; skipping review.")
        return "APPROVE"

    user_message = (
        f"PR #{pr_number}: {pr.title}\n\n"
        f"Description:\n{pr.body or '(none)'}\n\n"
        f"Diff:\n{diff_content}"
    )

    print(f"[review] calling Claude {REVIEW_MODEL} ...")
    response = client.messages.create(
        model=REVIEW_MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    raw_text = response.content[0].text
    print(f"[review] received response ({len(raw_text)} chars)")

    review = parse_review_json(raw_text)
    verdict = review.get("verdict", "REQUEST_CHANGES")
    print(f"[review] verdict: {verdict}")
    print(f"[review] summary: {review.get('summary', '')[:120]}")
    print(f"[review] inline comments: {len(review.get('inline_comments', []))}")

    posted = post_inline_comments(
        pr, pr.head.sha,
        review.get("inline_comments", []),
        valid_lines,
    )
    print(f"[review] {posted} inline comments posted")

    submit_review(pr, review.get("summary", ""), verdict)

    if verdict == "APPROVE":
        enable_automerge(pr, pr_number)

    return verdict


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="AI code review agent")
    parser.add_argument("--pr", type=int, required=True, help="PR number to review")
    parser.add_argument("--repo", default=None,
                        help="repo full name (default: GITHUB_REPO env var)")
    args = parser.parse_args()

    token = os.environ["GITHUB_TOKEN"]
    repo = args.repo or os.environ["GITHUB_REPO"]

    verdict = review_pr(token, repo, args.pr)
    print(f"\n{'✅ Approved' if verdict == 'APPROVE' else '🔄 Changes requested'}")
    sys.exit(0 if verdict == "APPROVE" else 1)


if __name__ == "__main__":
    main()
