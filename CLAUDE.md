# CLAUDE.md — agent-prototype

This file tells Claude Code how to work with this repository.

## What this repo is

A collection of autonomous AI agents that operate on GitHub repositories:
- **`agent.py`** — coding agent: clones repo, implements goal via Claude tool-use loop, opens/iterates PR. Contains GitHub GraphQL helpers (`get_open_review_thread_ids`, `resolve_all_review_threads`) for thread enforcement.
- **`review_agent.py`** — review agent: fetches PR diff + living context docs (CLAUDE.md, ARCHITECTURE.md, TEST.md from repo head), reviews with Claude, posts inline comments, approves/requests changes.
- **`plan_agent.py`** — plan agent: reads all living context docs first (CLAUDE.md, ARCHITECTURE.md, TEST.md, DECISIONS.md, deploy.md), then explores repo, writes `plan.md`.
- **`deploy_agent.py`** — deploy agent: reads `deploy.md`, calls Claude for az CLI commands, builds image once, deploys to staging then prod, health checks, rollback support.
- **`docs_agent.py`** — living docs updater: after every successful merge, updates ARCHITECTURE.md, TEST.md, DECISIONS.md, CLAUDE.md in the target repo based on what changed.
- **`pipeline.py`** — orchestrator: runs all 13 stages end-to-end (see pipeline stages below).
- **`tools.py`** — tool executor: `ToolExecutor` (direct) and `SandboxToolExecutor` (sandbox-isolated run_command).
- **`sandbox.py`** — execution sandbox: `DockerSandbox` (Docker container, preferred) and `ProcessSandbox` (restricted subprocess fallback). All agent run_command calls go through here.
- **`gc_agent.py`** — periodic garbage collection agent: dead code, complexity, docs drift, creates GitHub issue.

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

## Pipeline stages

```
Stage 1   PLAN              plan_agent.py reads living docs → writes plan.md
          [HITL gate]       human reviews plan, can provide feedback to implementation
Stage 2   IMPLEMENT         agent.py reads living docs → implements → opens PR (sandbox)
Stage 2.5 HARNESS           lint.sh + fitness.py → posted as PR comment (non-blocking)
Stage 3   TEST              generate pytest tests (Claude) → run locally
          [HITL gate]       human reviews test results
Stage 4   REVIEW            review_agent.py reads CLAUDE.md+ARCHITECTURE.md+TEST.md → reviews PR
          [HITL gate]       human reviews verdict, can override AI decision
Stage 5   RESOLVE COMMENTS  agent.py addresses feedback → resolve_all_review_threads (sandbox)
Stage 6   TEST AFTER RESOLVE re-run tests on updated branch
Stage 7   BUILD+STAGING     deploy_agent.py: az acr build → deploy to staging Container App
          [HITL gate]       human manually verifies staging URL before E2E
Stage 8   E2E STAGING       Puppeteer E2E vs live staging (real Anthropic API)
          [HITL gate]       human approves before irreversible merge to main
Stage 9   COMMIT            squash-merge PR to main
Stage 10  UPDATE DOCS       docs_agent.py updates ARCHITECTURE/TEST/DECISIONS/CLAUDE.md → commit
Stage 11  DEPLOY PROD       deploy_agent.py: deploy same image tag to prod Container App
Stage 12  E2E PROD          Puppeteer E2E vs prod; on failure: rollback + revert main + GitHub issue
```

Stages 4-6 loop up to `--max-resolve` times (default 3). All agent run_command calls execute
inside an isolated sandbox (DockerSandbox when Docker is available, ProcessSandbox otherwise).

## Sandbox

Agent shell execution is isolated via `agents/sandbox.py`:
- **DockerSandbox** (preferred): container with `--network none`, `--memory 512m`, `--cpus 1.0`
- **ProcessSandbox** (fallback): subprocess with CPU resource limits, no Docker required
- File ops (read_file, write_file, list_files) always run on the host — the repo clone is
  the shared state between the agent and the sandbox.

## Running the agents

```bash
# Run the full pipeline end-to-end (interactive, HITL gates at key stages)
python pipeline.py "add a /health endpoint"
python pipeline.py "add dark mode" --max-resolve 5

# Run without HITL gates (CI/CD mode)
python pipeline.py "add a /health endpoint" --auto

# Run individual agents
python agent.py "add a hello world endpoint"        # open new PR
python agent.py "address review feedback" --pr 42   # iterate on PR
python agent.py --merge --pr 42                     # squash-merge PR
python review_agent.py --pr 42                      # review a PR
python plan_agent.py "add dark mode"                # write plan.md only
python deploy_agent.py --repo-path /path/to/clone  # deploy only
python docs_agent.py --repo-path /path/to/clone --goal "..." --summary "..." --pr-url "..."
```

## Models

| Agent | Model | Why |
|---|---|---|
| `agent.py` | `claude-opus-4-5` | Tool-use loop needs strong instruction following |
| `review_agent.py` | `claude-opus-4-6` | Thoroughness for security/correctness review |
| `plan_agent.py` | `claude-opus-4-6` | Architecture reasoning needs full capability |
| `deploy_agent.py` | `claude-sonnet-4-6` | Single structured prompt — speed over depth |
| `docs_agent.py` | `claude-sonnet-4-6` | Doc update — structured JSON, speed matters |
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
