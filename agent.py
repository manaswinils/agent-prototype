"""Minimal coding agent: goal in, PR out. Supports iterating on existing PRs via --pr."""
import argparse
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv
from git import Repo
from github import Github

from tools import TOOL_SCHEMAS, ToolExecutor

load_dotenv()

MODEL = "claude-opus-4-5"
MAX_ITERATIONS = 25
SYSTEM_PROMPT = """You are an autonomous coding agent working inside a GitHub repository.

Your job:
1. Understand the goal the user gave you.
2. Explore the repo with list_files and read_file to understand its structure and conventions.
3. Make the minimal set of changes needed to accomplish the goal.
4. Match existing code style, file locations, and naming conventions.
5. When you're done, summarize what you changed in one short paragraph and end your turn.

Rules:
- Do NOT run git commands — commits and PRs are handled automatically after you finish.
- Do NOT add dependencies unless the goal requires it.
- Prefer the smallest change that works. Do not refactor unrelated code.
- If the goal is ambiguous, make a reasonable choice and note the assumption in your summary.
- If you are iterating on an existing PR, focus only on addressing the feedback. Do not revisit unrelated parts of the PR.
"""


def clone_repo(repo_full_name: str, token: str, username: str, branch: str | None = None) -> Path:
    """Clone the target repo into a temp directory. Optionally check out an existing branch."""
    tmp = Path(tempfile.mkdtemp(prefix="agent-"))
    url = f"https://{username}:{token}@github.com/{repo_full_name}.git"
    print(f"[setup] cloning {repo_full_name} ...")
    repo = Repo.clone_from(url, tmp)
    if branch:
        print(f"[setup] checking out existing branch: {branch}")
        repo.git.checkout(branch)
    return tmp


def fetch_pr_context(token: str, repo_full_name: str, pr_number: int) -> dict:
    """Fetch the branch name, all review comments, and reviewer info for a PR."""
    gh = Github(token)
    gh_repo = gh.get_repo(repo_full_name)
    pr = gh_repo.get_pull(pr_number)

    comments = []
    # Inline code review comments
    for c in pr.get_review_comments():
        comments.append({
            "author": c.user.login,
            "file": c.path,
            "line": c.line or c.original_line,
            "body": c.body,
            "kind": "inline",
        })
    # Top-level PR conversation comments
    for c in pr.get_issue_comments():
        comments.append({
            "author": c.user.login,
            "body": c.body,
            "kind": "conversation",
        })

    # Formal review submissions (body text of approve/request-changes reviews)
    requested_users, requested_teams = pr.get_review_requests()
    reviewer_logins = {u.login for u in requested_users}
    team_slugs = [t.slug for t in requested_teams]
    for review in pr.get_reviews():
        if review.user and review.user.login != pr.user.login:
            reviewer_logins.add(review.user.login)
        if review.body and review.body.strip():
            comments.append({
                "author": review.user.login if review.user else "unknown",
                "body": review.body,
                "state": review.state,
                "kind": "review",
            })

    return {
        "number": pr.number,
        "title": pr.title,
        "branch": pr.head.ref,
        "body": pr.body or "",
        "comments": comments,
        "reviewer_logins": list(reviewer_logins),
        "team_slugs": team_slugs,
        "pr_object": pr,
    }


def format_pr_context_for_prompt(pr_ctx: dict, user_goal: str) -> str:
    """Build a prompt that gives the agent full context on the PR feedback."""
    lines = [
        f"You are iterating on an existing pull request: #{pr_ctx['number']} — {pr_ctx['title']}",
        f"Branch: {pr_ctx['branch']}",
        "",
        "Original PR description:",
        pr_ctx["body"] or "(no description)",
        "",
        "--- Review comments ---",
    ]

    if not pr_ctx["comments"]:
        lines.append("(no comments found)")
    else:
        for i, c in enumerate(pr_ctx["comments"], 1):
            if c["kind"] == "inline":
                lines.append(f"[{i}] Inline comment by {c['author']} on {c['file']}:{c['line']}")
            elif c["kind"] == "review":
                lines.append(f"[{i}] Review ({c['state']}) by {c['author']}:")
            else:
                lines.append(f"[{i}] Conversation comment by {c['author']}:")
            lines.append(f"    {c['body']}")
            lines.append("")

    lines.append("--- User instruction for this iteration ---")
    lines.append(user_goal)
    lines.append("")
    lines.append("Address the feedback above. Make only the changes needed. The branch is already checked out.")

    return "\n".join(lines)


def run_agent_loop(client: Anthropic, executor: ToolExecutor, goal: str) -> str:
    """Run the Claude tool-use loop. Returns the final assistant summary text."""
    messages = [{"role": "user", "content": goal}]
    final_text = ""

    for iteration in range(1, MAX_ITERATIONS + 1):
        print(f"\n[iter {iteration}] calling Claude ...")
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOL_SCHEMAS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"[assistant] {block.text.strip()}")
                final_text = block.text.strip()

        if response.stop_reason == "end_turn":
            print("[iter] agent finished.")
            return final_text

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"[tool] {block.name}({_summarize_input(block.input)})")
                result = executor.dispatch(block.name, block.input)
                preview = result[:200].replace("\n", " ")
                print(f"[tool] -> {preview}{'...' if len(result) > 200 else ''}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        if not tool_results:
            print("[iter] no tools used and not end_turn, stopping.")
            return final_text

        messages.append({"role": "user", "content": tool_results})

    print(f"[iter] hit max iterations ({MAX_ITERATIONS}), stopping.")
    return final_text or "Agent hit iteration limit."


def _summarize_input(tool_input: dict) -> str:
    parts = []
    for k, v in tool_input.items():
        s = str(v)
        if len(s) > 60:
            s = s[:60] + "..."
        parts.append(f"{k}={s!r}")
    return ", ".join(parts)


def commit_and_push_new_branch(repo_path: Path, branch: str, commit_message: str) -> bool:
    """For new PRs: create a branch, commit, push. Returns True if there were changes."""
    repo = Repo(repo_path)
    repo.git.checkout("-b", branch)
    if not repo.is_dirty(untracked_files=True):
        print("[git] no changes to commit.")
        return False
    repo.git.add(A=True)
    repo.index.commit(commit_message)
    print(f"[git] pushing new branch {branch} ...")
    repo.git.push("--set-upstream", "origin", branch)
    return True


def commit_and_push_existing_branch(repo_path: Path, commit_message: str) -> bool:
    """For PR iterations: commit on current branch and push. Returns True if there were changes."""
    repo = Repo(repo_path)
    if not repo.is_dirty(untracked_files=True):
        print("[git] no changes to commit.")
        return False
    repo.git.add(A=True)
    repo.index.commit(commit_message)
    current_branch = repo.active_branch.name
    print(f"[git] pushing to existing branch {current_branch} ...")
    repo.git.push("origin", current_branch)
    return True


def open_pull_request(token: str, repo_full_name: str, branch: str, title: str, body: str) -> str:
    gh = Github(token)
    gh_repo = gh.get_repo(repo_full_name)
    default_branch = gh_repo.default_branch
    pr = gh_repo.create_pull(title=title, body=body, head=branch, base=default_branch)
    return pr.html_url


def comment_on_pr(token: str, repo_full_name: str, pr_number: int, body: str) -> None:
    gh = Github(token)
    gh_repo = gh.get_repo(repo_full_name)
    pr = gh_repo.get_pull(pr_number)
    pr.create_issue_comment(body)


def merge_pr(token: str, repo_full_name: str, pr_number: int) -> bool:
    """Squash-merge a PR. Tries auto-merge first, falls back to direct merge."""
    from github import GithubException
    gh = Github(token)
    pr = gh.get_repo(repo_full_name).get_pull(pr_number)
    print(f"[merge] attempting to merge PR #{pr_number}: {pr.title}")
    try:
        pr.enable_automerge(merge_method="SQUASH")
        print(f"[merge] auto-merge (squash) enabled for PR #{pr_number}")
        return True
    except GithubException as e:
        print(f"[merge] enable_automerge not available ({e.status}); trying direct merge")
    try:
        result = pr.merge(
            merge_method="squash",
            commit_title=f"Squash-merge PR #{pr_number}: {pr.title}",
        )
        if result.merged:
            print(f"[merge] PR #{pr_number} merged")
            return True
        print(f"[merge] merged=False: {result.message}")
        return False
    except GithubException as e:
        print(f"[merge] direct merge failed: {e}")
        return False


def request_re_review(token: str, repo_full_name: str, pr_number: int, reviewer_logins: list[str], team_slugs: list[str]) -> None:
    """Re-request reviews from all previous reviewers so they know the feedback was addressed."""
    if not reviewer_logins and not team_slugs:
        print("[review] no reviewers to re-request.")
        return
    gh = Github(token)
    gh_repo = gh.get_repo(repo_full_name)
    pr = gh_repo.get_pull(pr_number)
    kwargs: dict = {}
    if reviewer_logins:
        kwargs["reviewers"] = reviewer_logins
    if team_slugs:
        kwargs["team_reviewers"] = team_slugs
    pr.create_review_request(**kwargs)
    names = reviewer_logins + [f"team:{s}" for s in team_slugs]
    print(f"[review] re-requested review from: {', '.join(names)}")


def main():
    parser = argparse.ArgumentParser(description="Minimal coding agent")
    parser.add_argument("goal", nargs="?", help="What you want the agent to do")
    parser.add_argument("--pr", type=int, help="Iterate on an existing PR number instead of creating a new one")
    parser.add_argument("--merge", action="store_true", help="Squash-merge the PR specified by --pr (no coding)")
    parser.add_argument("--keep", action="store_true", help="Keep the temp clone for inspection")
    args = parser.parse_args()

    api_key = os.environ["ANTHROPIC_API_KEY"]
    gh_token = os.environ["GITHUB_TOKEN"]
    gh_user = os.environ["GITHUB_USERNAME"]
    gh_repo = os.environ["GITHUB_REPO"]

    if args.merge:
        if args.pr is None:
            parser.error("--merge requires --pr <number>")
        success = merge_pr(gh_token, gh_repo, args.pr)
        sys.exit(0 if success else 1)

    client = Anthropic(api_key=api_key)

    if args.pr is not None:
        # --- PR iteration mode ---
        print(f"[mode] iterating on existing PR #{args.pr}")
        pr_ctx = fetch_pr_context(gh_token, gh_repo, args.pr)
        print(f"[pr] branch: {pr_ctx['branch']}, comments: {len(pr_ctx['comments'])}")

        repo_path = clone_repo(gh_repo, gh_token, gh_user, branch=pr_ctx["branch"])
        try:
            executor = ToolExecutor(repo_path)
            prompt = format_pr_context_for_prompt(pr_ctx, args.goal)
            summary = run_agent_loop(client, executor, prompt)

            commit_message = f"Agent: address PR #{args.pr} feedback\n\n{summary}"
            pushed = commit_and_push_existing_branch(repo_path, commit_message)

            if pushed:
                comment_on_pr(
                    gh_token, gh_repo, args.pr,
                    f"🤖 Agent update pushed to this PR.\n\n**Summary:**\n{summary}",
                )
                request_re_review(
                    gh_token, gh_repo, args.pr,
                    pr_ctx["reviewer_logins"], pr_ctx["team_slugs"],
                )
                print(f"\n✅ Pushed update to PR #{args.pr}")
            else:
                print("\n⚠️  Agent finished but made no changes. Nothing pushed.")
        finally:
            if args.keep:
                print(f"[cleanup] keeping temp dir at {repo_path}")
            else:
                shutil.rmtree(repo_path, ignore_errors=True)

    else:
        # --- New PR mode (original behavior) ---
        repo_path = clone_repo(gh_repo, gh_token, gh_user)
        try:
            executor = ToolExecutor(repo_path)
            summary = run_agent_loop(client, executor, args.goal)

            branch = f"agent/{uuid.uuid4().hex[:8]}"
            commit_message = f"Agent: {args.goal[:60]}\n\n{summary}"
            pushed = commit_and_push_new_branch(repo_path, branch, commit_message)

            if pushed:
                pr_url = open_pull_request(
                    gh_token, gh_repo, branch,
                    title=f"Agent: {args.goal[:60]}",
                    body=f"**Goal:** {args.goal}\n\n**Summary:**\n{summary}\n\n---\n_Opened by coding agent prototype._",
                )
                print(f"\n✅ PR opened: {pr_url}")
            else:
                print("\n⚠️  Agent finished but made no changes. No PR opened.")
        finally:
            if args.keep:
                print(f"[cleanup] keeping temp dir at {repo_path}")
            else:
                shutil.rmtree(repo_path, ignore_errors=True)


if __name__ == "__main__":
    main()
