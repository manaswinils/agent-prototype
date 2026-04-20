# Architecture — Multi-Agent CI/CD System

## Overview

This system uses a collection of AI agents (powered by Claude) to automate the
full software development lifecycle on a GitHub repository: writing code, reviewing
it, generating tests, and deploying to Azure.

## Agent inventory

| Agent | File | Model | Trigger | Responsibility |
|---|---|---|---|---|
| Pipeline orchestrator | `pipeline.py` | — | CLI | Runs all 13 stages end-to-end |
| Plan agent | `plan_agent.py` | claude-opus-4-6 | Stage 1 (pipeline) | Reads living docs + repo, writes plan.md |
| Coding agent | `agent.py` | claude-opus-4-5 | Stage 2 / 5 (pipeline) | Reads living docs, implements goal, opens/iterates PR |
| Review agent | `review_agent.py` | claude-opus-4-6 | Stage 4 (pipeline) | Reads CLAUDE.md + ARCHITECTURE.md, reviews diff, resolves threads |
| Test generator | `pipeline.py:run_test_stage` | claude-sonnet-4-6 | Stage 3 / 6 (pipeline) | Generates pytest tests, runs locally |
| Deploy agent | `deploy_agent.py` | claude-sonnet-4-6 | Stage 7–8 / 11–12 (pipeline) | Reads deploy.md, runs az CLI commands, health checks |
| Docs agent | `docs_agent.py` | claude-sonnet-4-6 | Stage 10 (pipeline) | Updates ARCHITECTURE.md, TEST.md, DECISIONS.md, CLAUDE.md |
| E2E runner | `pipeline.py:run_e2e_tests` | — | Stage 8 / 12 (pipeline) | Runs Puppeteer tests vs live staging/prod |

## Pipeline flow (pipeline.py)

```
python pipeline.py "goal" [--repo owner/repo] [--max-resolve N]
        │
        ▼
Stage 1  PLAN
  plan_agent.py reads CLAUDE.md, ARCHITECTURE.md, TEST.md, DECISIONS.md, deploy.md
  → explore repo with list_files + read_file
  → write plan.md to clone
        │
        ▼
Stage 2  IMPLEMENT
  agent.py reads all living docs + plan.md → Claude tool-use loop → commit + push new branch
  → open_pull_request() → PR #N
        │
        ▼
Stage 3  TEST
  generate pytest tests (claude-sonnet-4-6) if absent
  → pip install → pytest --cov (non-fatal)
        │
        ▼
Stage 4  REVIEW  ◄──────────────────────────────────────┐
  review_agent.py reads CLAUDE.md + ARCHITECTURE.md + TEST.md from PR head
  → Claude reviews diff against project conventions
  → post inline comments → submit APPROVE / REQUEST_CHANGES
        │                                               │
        │  APPROVE?                                     │
        ├── yes → check get_open_review_thread_ids()   │
        │          resolve any remaining open threads   │
        │          → proceed to Stage 7                 │
        │                                               │
        └── no, REQUEST_CHANGES ───────────────────────►
Stage 5  RESOLVE COMMENTS                               │
  agent.py reads PR context → Claude addresses feedback  │
  → commit + push → comment on PR                       │
  → resolve_all_review_threads() (GitHub GraphQL)       │
        │                                               │
        ▼                                               │
Stage 6  TEST AFTER RESOLVE                             │
  re-run tests on updated branch (non-fatal) ───────────┘
        │         (back to Stage 4, up to --max-resolve times)
        ▼
Stage 7  BUILD + DEPLOY TO STAGING
  read_deploy_md() → generate_all_commands() (Claude, one call)
  → az acr build (build once)
  → az containerapp update → motivational-quote-app-staging
  → verify_health() (15 retries × 15s, 120s HTTP timeout for cold start)
        │
        ▼
Stage 8  E2E TEST — STAGING
  npm install → node test_e2e.js <staging_url>  (real Anthropic API calls)
  → fail: rollback staging → abort (code NOT merged)
  → pass: continue
        │
        ▼
Stage 9  COMMIT
  merge_pr() (squash merge to main)
        │
        ▼
Stage 10  UPDATE LIVING DOCS
  docs_agent.py reads source + current docs
  → Claude updates ARCHITECTURE.md, TEST.md, DECISIONS.md, CLAUDE.md
  → commit + push to main (non-fatal if fails)
        │
        ▼
Stage 11  DEPLOY TO PROD
  az containerapp update → motivational-quote-app (same image tag, no rebuild)
  → verify_health() (5 retries × 10s, 30s HTTP timeout)
        │
        ▼
Stage 12  E2E TEST — PROD
  node test_e2e.js <prod_url>  (real Anthropic API calls)
  → fail:
       rollback_to(prod, previous_prod_tag)
       revert_merge_on_main() → new revert PR → auto-merge
       create_github_issue() with full failure details
  → pass: ✅ pipeline complete
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
    → fetch_context_for_review(pr):
          pr.base.repo.get_contents("CLAUDE.md", ref=head_sha)
          pr.base.repo.get_contents("ARCHITECTURE.md", ref=head_sha)
          pr.base.repo.get_contents("TEST.md", ref=head_sha)
    → pr.get_files() → patch text per file
    → parse_valid_new_lines(patch) → set of valid diff line numbers
    → Claude (claude-opus-4-6, JSON response):
          system: review against conventions in CLAUDE.md + ARCHITECTURE.md
          {summary, inline_comments: [{file, line, comment}], verdict}
    → post_inline_comments() (nearest-valid-line fallback)
    → pr.create_review(event=APPROVE|REQUEST_CHANGES|COMMENT)
    → [APPROVE via pipeline] get_open_review_thread_ids() → resolve remaining
```

### Docs agent (`docs_agent.py`)

```
repo_path (main branch post-merge), goal, impl_summary, pr_url, plan_content
    → _read_current_docs(): read ARCHITECTURE.md, TEST.md, DECISIONS.md, CLAUDE.md
    → _collect_source_files(): read *.py, requirements.txt, Dockerfile, Procfile
    → Claude (claude-sonnet-4-6, JSON response):
          {"ARCHITECTURE.md": "...", "TEST.md": "...", "DECISIONS.md": "...", "CLAUDE.md": "..."}
    → write updated files to repo_path
    → git add + commit + push to main
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

## Living context documents (in agent-sandbox)

Each agent reads relevant living docs before acting. The docs_agent updates them after every merge.

| Document | Read by | Updated by | Content |
|---|---|---|---|
| `CLAUDE.md` | plan_agent, coding agent, review_agent | docs_agent | Conventions, app structure, what NOT to do |
| `ARCHITECTURE.md` | plan_agent, coding agent, review_agent | docs_agent | Components, routes, data flow, deployment |
| `TEST.md` | plan_agent, coding agent, review_agent | docs_agent | Test strategy, coverage, mocking patterns |
| `DECISIONS.md` | plan_agent, coding agent | docs_agent | ADR log — past design decisions + rationale |
| `deploy.md` | plan_agent, coding agent, deploy_agent | manual | Azure resources, image names, health URLs |
| `plan.md` | coding agent | plan_agent (each run) | Current PR implementation plan |

Context propagation per stage:
- **Stage 1 (Plan)**: reads all 5 static docs → produces plan.md
- **Stage 2 (Implement)**: prompted to read CLAUDE.md + ARCHITECTURE.md + TEST.md + DECISIONS.md + plan.md
- **Stage 4 (Review)**: fetches CLAUDE.md + ARCHITECTURE.md + TEST.md from PR head SHA via GitHub API
- **Stage 10 (Update Docs)**: writes all 4 living docs based on what changed

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

### Docs agent — JSON
```json
{
  "ARCHITECTURE.md": "# Architecture — ...\n\n...",
  "TEST.md": "# Test Strategy — ...\n\n...",
  "DECISIONS.md": "# Architectural Decisions — ...\n\n## ADR-005: ...\n\n...",
  "CLAUDE.md": "# CLAUDE.md — ...\n\n..."
}
```
Parsing: same three-tier fallback (raw JSON → strip fences → first `{…}` blob).

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
