# CLAUDE.md — agent-prototype

This file tells Claude Code how to work with this repository.

## What this repo is

A collection of autonomous AI agents that operate on GitHub repositories:
- **`agent.py`** — coding agent: takes a goal, clones a target repo, implements changes via Claude tool-use loop, opens or iterates on a PR.
- **`review_agent.py`** — review agent: fetches a PR diff, reviews it with Claude, posts inline comments, approves or requests changes, and enables auto-merge on approval.
- **`tools.py`** — tool executor used by `agent.py` (list_files, read_file, write_file, run_command).

## Environment setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in values
```

Required `.env` variables:
| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `GITHUB_TOKEN` | Classic PAT with `repo` + `workflow` scopes |
| `GITHUB_USERNAME` | GitHub username matching the token |
| `GITHUB_REPO` | Target repo in `owner/repo` format |

## Running the agents

```bash
# Open a new PR with a coding goal
python agent.py "add a hello world endpoint"

# Iterate on an existing PR (addresses all comments)
python agent.py "address review feedback" --pr 42

# Squash-merge a PR
python agent.py --merge --pr 42

# Review a PR (posts inline comments + approves/requests changes)
python review_agent.py --pr 42
```

## Models

| Agent | Model | Why |
|---|---|---|
| `agent.py` | `claude-opus-4-5` | Tool-use loop needs strong instruction following |
| `review_agent.py` | `claude-opus-4-6` | Thoroughness for security/correctness review |

Use the latest available model for new agents (`claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`).

## Code conventions

- Each agent is a **standalone script** with a `main()` + `if __name__ == "__main__"` entry point.
- All GitHub interactions go through **PyGithub** (`from github import Github`).
- All Anthropic calls go through the **`anthropic` SDK** (`from anthropic import Anthropic`).
- Load env vars with `load_dotenv()` at module top; read with `os.environ[...]` (not `.get()`) so missing vars fail loudly.
- Agent functions return typed values and print `[tag] message` logs to stdout.
- Do **not** add git commands inside agent loops — git operations are handled by the orchestration functions (`commit_and_push_*`, `merge_pr`, etc.).

## Adding a new agent

1. Create `<name>_agent.py` as a standalone script.
2. Add a `SYSTEM_PROMPT` constant at the top.
3. Define one core function `run_<name>(token, repo, ...)` that does the work.
4. Add a `main()` with `argparse` reading `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, `GITHUB_REPO` from env.
5. Update this file and `ARCHITECTURE.md`.

## What NOT to do

- Do not commit `.env` — it is in `.gitignore`.
- Do not add `print` statements inside tight loops — use the `[tag]` convention sparingly.
- Do not catch bare `Exception` without logging — always print the error.
- Do not hardcode repo names or API keys anywhere.
