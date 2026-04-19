# CLAUDE.md — agent-prototype

This file tells Claude Code how to work with this repository.

## What this repo is

A collection of autonomous AI agents that operate on GitHub repositories:
- **`agent.py`** — coding agent: takes a goal, clones a target repo, implements changes via Claude tool-use loop, opens or iterates on a PR.
- **`review_agent.py`** — review agent: fetches a PR diff, reviews it with Claude, posts inline comments, approves or requests changes, and enables auto-merge on approval.
- **`plan_agent.py`** — plan agent: explores a repo with a read-only Claude tool-use loop, writes `plan.md` with implementation approach.
- **`deploy_agent.py`** — deploy agent: reads `deploy.md`, calls Claude for az CLI commands, builds image, deploys to Azure Container Apps, verifies health.
- **`pipeline.py`** — orchestrator: runs all 8 stages (Plan → Implement → Test → Review → Resolve → Test → Commit → Deploy) end-to-end.
- **`tools.py`** — tool executor used by coding and plan agents (list_files, read_file, write_file, run_command).

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
# Run the full pipeline end-to-end (recommended)
python pipeline.py "add a /health endpoint"

# Run individual agents
python agent.py "add a hello world endpoint"        # open new PR
python agent.py "address review feedback" --pr 42   # iterate on PR
python agent.py --merge --pr 42                     # squash-merge PR
python review_agent.py --pr 42                      # review a PR
python plan_agent.py "add dark mode"                # write plan.md only
python deploy_agent.py --repo-path /path/to/clone  # deploy only
```

## Models

| Agent | Model | Why |
|---|---|---|
| `agent.py` | `claude-opus-4-5` | Tool-use loop needs strong instruction following |
| `review_agent.py` | `claude-opus-4-6` | Thoroughness for security/correctness review |
| `plan_agent.py` | `claude-opus-4-6` | Architecture reasoning needs full capability |
| `deploy_agent.py` | `claude-sonnet-4-6` | Single structured prompt — speed over depth |
| `pipeline.py` (test gen) | `claude-sonnet-4-6` | Test generation — balanced speed/quality |

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
