# Architecture — Multi-Agent CI/CD System

## Overview

This system uses a collection of AI agents (powered by Claude) to automate the
full software development lifecycle on a GitHub repository: writing code, reviewing
it, generating tests, and deploying to Azure.

## Agent inventory

| Agent | File | Model | Trigger | Responsibility |
|---|---|---|---|---|
| Coding agent | `agent.py` | claude-opus-4-5 | CLI / manual | Implements a goal in a repo, opens or iterates on a PR |
| Review agent | `review_agent.py` | claude-opus-4-6 | GitHub Actions (PR) | Reviews diff, posts inline comments, approves or requests changes |
| Test agent | `.agents/test_agent.py` | claude-sonnet-4-6 | GitHub Actions (PR) | Generates unit + functional tests, runs pytest, reports coverage |

## System diagram

```
Developer / Automation
        │
        │  python agent.py "goal"
        ▼
┌───────────────────┐
│   Coding Agent    │  clones repo → Claude tool-use loop → commit + push
│   (agent.py)      │  (tools: list_files, read_file, write_file, run_command)
└────────┬──────────┘
         │  opens PR on agent-sandbox
         ▼
┌─────────────────────────────────────────────────────┐
│                  GitHub: agent-sandbox               │
│                                                     │
│  PR opened / updated                                │
│       │                                             │
│       ├──► ai-review.yml ──► review_agent.py        │
│       │         │                                   │
│       │         ├── inline comments on diff         │
│       │         ├── APPROVE → enable auto-merge     │
│       │         └── REQUEST_CHANGES → agent.py --pr │
│       │                                             │
│       └──► test.yml ──► test_agent.py               │
│                 │                                   │
│                 ├── generates tests/test_unit.py    │
│                 ├── generates tests/test_functional │
│                 ├── pytest --cov --cov-fail-under=70│
│                 └── posts coverage comment on PR    │
│                                                     │
│  All checks pass → auto-merge to main               │
│       │                                             │
│       ▼                                             │
│  azure-deploy.yml                                   │
│       ├── test job  (re-runs tests as gate)         │
│       └── deploy job                               │
│             ├── docker build                        │
│             ├── push to Azure Container Registry    │
│             └── az containerapp update              │
└─────────────────────────────────────────────────────┘
                          │
                          ▼
              Azure Container Apps
              (motivational-quote-app)
```

## Data flow

### Coding agent (`agent.py`)

```
CLI args (goal, --pr, --merge)
    → clone repo into /tmp/
    → [if --pr] fetch PR context: branch, inline comments, conversation comments, review bodies
    → build prompt (goal + PR context)
    → Claude tool-use loop (max 25 iterations):
          Claude → tool_use block → ToolExecutor.dispatch() → tool_result → Claude
    → commit + push changes
    → [new PR] open_pull_request()
    → [existing PR] comment_on_pr() + request_re_review()
    → [--merge] merge_pr()
    → cleanup temp dir
```

### Review agent (`review_agent.py`)

```
PR number
    → pr.get_files() → patch text per file
    → parse_valid_new_lines(patch) → set of valid diff line numbers
    → Claude (claude-opus-4-6, JSON response):
          {summary, inline_comments: [{file, line, comment}], verdict}
    → post_inline_comments() (nearest-valid-line fallback)
    → pr.create_review(event=APPROVE|REQUEST_CHANGES)
    → [APPROVE] pr.enable_automerge(merge_method=SQUASH)
```

### Test agent (`.agents/test_agent.py`)

```
PR number (from PR_NUMBER env var)
    → pr.get_files() → filter Python source files
    → read local checkout of each file
    → Claude (claude-sonnet-4-6):
          generates two ```python blocks (test_unit, test_functional)
    → parse_test_blocks() → classify by first-line comment
    → write tests/test_unit.py + tests/test_functional.py
    → pytest --cov=app --cov-report=json --cov-fail-under=70
    → parse coverage.json → format Markdown report
    → pr.create_issue_comment(coverage report)
    → exit with pytest exit code (gates the GH Actions job)
```

## Claude response formats

### Review agent — JSON
```json
{
  "summary": "2-3 sentence overall assessment",
  "inline_comments": [
    {"file": "app.py", "line": 15, "comment": "actionable feedback"}
  ],
  "verdict": "APPROVE"
}
```
Parsing: three-tier fallback (raw JSON → strip fences → first `{…}` blob).

### Test agent — code blocks
```
```python
# tests/test_unit.py
...
```
```python
# tests/test_functional.py
...
```
```
Parsing: `re.findall(r'```python\n(.*?)```', text, re.DOTALL)`, classified by first-line comment.

## GitHub Actions workflow chain

```
Trigger: pull_request (opened / synchronize / reopened)
├── ai-review.yml / review  (parallel)
└── test.yml / test          (parallel)

Trigger: push to main
└── azure-deploy.yml
      ├── test  (sequential gate)
      └── build-and-deploy  (needs: [test])
```

Required GitHub secrets on `agent-sandbox`:

| Secret | Used by |
|---|---|
| `ANTHROPIC_API_KEY` | review_agent.py, test_agent.py |
| `AZURE_CREDENTIALS` | azure-deploy.yml (az login) |
| `ACR_NAME` | azure-deploy.yml |
| `CONTAINER_APP_NAME` | azure-deploy.yml |
| `AZURE_RESOURCE_GROUP` | azure-deploy.yml |

## Extension points

- **New agent**: add `<name>_agent.py` in `agent-prototype/` and `.agents/<name>_agent.py` in the target repo. Add a new workflow YAML that calls it.
- **New target repo**: set `GITHUB_REPO` in `.env` and ensure the PAT has access.
- **Stricter coverage**: change `--cov-fail-under=70` in `test.yml` and `azure-deploy.yml`.
- **Different merge strategy**: change `merge_method` in `review_agent.py` `enable_automerge()` call (`MERGE`, `SQUASH`, or `REBASE`).
