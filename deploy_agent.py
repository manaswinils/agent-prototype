"""Deploy agent: reads deploy.md and deploys to staging or production on Azure.

Build once → deploy to staging → (pipeline promotes to) prod.

Usage (standalone):
    python deploy_agent.py --repo-path /path/to/local/agent-sandbox --target staging
    python deploy_agent.py --repo-path /path/to/local/agent-sandbox --target prod

Or called from pipeline.py:
    from deploy_agent import build_image, deploy_to, rollback_to, generate_all_commands

Required env vars:
    ANTHROPIC_API_KEY
    (Azure CLI must already be authenticated: az login)
"""
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

DEPLOY_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are a deployment automation assistant.
You will receive a deploy.md configuration document and a deployment tag.
Generate the exact shell commands to build and deploy the application to both staging and production.

IMPORTANT: append "--output none" to both deploy commands to suppress verbose JSON output.

Respond with ONLY a JSON object — no prose, no code fences:
{
  "build_command": "<full az acr build command with <TAG> replaced by the provided tag>",
  "staging_deploy_command": "<full az containerapp update for staging with <TAG> replaced, ending with --output none>",
  "prod_deploy_command": "<full az containerapp update for production with <TAG> replaced, ending with --output none>",
  "staging_health_url": "<staging health check URL from deploy.md>",
  "prod_health_url": "<production health check URL from deploy.md>"
}"""


# ── JSON parsing ──────────────────────────────────────────────────────────────

def _parse_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Could not parse JSON from deploy agent response:\n{text[:500]}")


# ── core helpers ──────────────────────────────────────────────────────────────

def read_deploy_md(repo_path: Path) -> str:
    """Read deploy.md from the repo root. Raises FileNotFoundError if missing."""
    path = repo_path / "deploy.md"
    if not path.exists():
        raise FileNotFoundError(
            f"deploy.md not found at {path}. "
            "Create deploy.md in the repo root before running the deploy agent."
        )
    content = path.read_text(encoding="utf-8")
    print(f"[deploy] read deploy.md ({len(content)} chars)")
    return content


def generate_all_commands(deploy_md_content: str, tag: str) -> dict:
    """
    Call Claude to parse deploy.md and produce all az commands for staging + prod.
    Tag is Python-generated — Claude substitutes it verbatim.

    Returns dict with keys:
        build_command, staging_deploy_command, prod_deploy_command,
        staging_health_url, prod_health_url
    """
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    user_message = (
        f"Deployment tag to use (substitute for <TAG> in all commands): {tag}\n\n"
        f"deploy.md content:\n\n{deploy_md_content}"
    )

    print(f"[deploy] calling Claude {DEPLOY_MODEL} for commands (tag={tag}) ...")
    response = client.messages.create(
        model=DEPLOY_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = response.content[0].text
    commands = _parse_json(raw)

    required = {"build_command", "staging_deploy_command", "prod_deploy_command",
                "staging_health_url", "prod_health_url"}
    missing = required - set(commands.keys())
    if missing:
        raise ValueError(f"Claude response missing keys: {missing}")

    print(f"[deploy] tag:                    {tag}")
    print(f"[deploy] build_command:          {commands['build_command']}")
    print(f"[deploy] staging_deploy_command: {commands['staging_deploy_command']}")
    print(f"[deploy] prod_deploy_command:    {commands['prod_deploy_command']}")
    print(f"[deploy] staging_health_url:     {commands['staging_health_url']}")
    print(f"[deploy] prod_health_url:        {commands['prod_health_url']}")

    return commands


def run_command(cmd: str, timeout: int = 300, cwd: str | None = None) -> tuple[int, str]:
    """Run a shell command with live output streaming. Returns (exit_code, full_output)."""
    print(f"[deploy] running: {cmd}")
    output_lines: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=cwd,
        )
        for line in iter(proc.stdout.readline, ""):
            print(f"[deploy]   {line}", end="")
            output_lines.append(line)
        proc.stdout.close()
        proc.wait(timeout=timeout)
        return proc.returncode, "".join(output_lines)
    except subprocess.TimeoutExpired:
        proc.kill()
        output_lines.append(f"\n[deploy] command timed out after {timeout}s\n")
        return 1, "".join(output_lines)
    except Exception as e:
        return 1, str(e)


def verify_health(url: str, retries: int = 5, delay: float = 10.0, timeout: int = 30) -> bool:
    """HTTP GET the URL, expect 200. Retries with delay. Returns True if healthy."""
    print(f"[deploy] verifying health: {url}")
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.urlopen(url, timeout=timeout)
            if req.status == 200:
                print(f"[deploy] health check passed (attempt {attempt})")
                return True
            print(f"[deploy] health check attempt {attempt}: HTTP {req.status}")
        except (urllib.error.HTTPError, urllib.error.URLError, Exception) as e:
            print(f"[deploy] health check attempt {attempt} failed: {e}")
        if attempt < retries:
            print(f"[deploy] retrying in {delay:.0f}s ...")
            time.sleep(delay)
    print(f"[deploy] health check failed after {retries} attempts.")
    return False


# ── per-environment helpers ───────────────────────────────────────────────────

def get_current_image_tag(deploy_cmd: str, cwd: str | None = None) -> str | None:
    """
    Query the currently-running image tag for the app referenced in deploy_cmd.
    Returns tag string (e.g. "20260419-223451") or None on failure.
    """
    m_name = re.search(r"--name\s+(\S+)", deploy_cmd)
    m_rg = re.search(r"--resource-group\s+(\S+)", deploy_cmd)
    if not m_name or not m_rg:
        print("[deploy] could not parse app name/resource-group from deploy_command")
        return None
    app_name = m_name.group(1)
    resource_group = m_rg.group(1)
    query_cmd = (
        f"az containerapp show --name {app_name} --resource-group {resource_group} "
        f'--query "properties.template.containers[0].image" -o tsv'
    )
    try:
        result = subprocess.run(
            query_cmd, shell=True, capture_output=True, text=True, timeout=30, cwd=cwd
        )
        if result.returncode != 0:
            print(f"[deploy] get_current_image_tag failed: {result.stderr.strip()}")
            return None
        image = result.stdout.strip()
        if ":" in image:
            tag = image.split(":")[-1]
            print(f"[deploy] current tag for {app_name}: {tag}")
            return tag
    except Exception as e:
        print(f"[deploy] get_current_image_tag error: {e}")
    return None


def build_image(commands: dict, cwd: str | None = None) -> bool:
    """Build and push the Docker image to ACR. Returns True on success."""
    print("\n[deploy] --- BUILD ---")
    exit_code, _ = run_command(commands["build_command"], timeout=600, cwd=cwd)
    if exit_code != 0:
        print(f"[deploy] build failed (exit {exit_code})")
        return False
    print("[deploy] build succeeded")
    return True


def deploy_to(commands: dict, target: str, cwd: str | None = None) -> tuple[bool, str | None]:
    """
    Deploy to 'staging' or 'prod'.
    Captures the current tag before deploying (for rollback).
    Returns (success, previous_tag).
    """
    deploy_cmd = commands[f"{target}_deploy_command"]
    health_url = commands[f"{target}_health_url"]

    previous_tag = get_current_image_tag(deploy_cmd, cwd=cwd)

    print(f"\n[deploy] --- DEPLOY TO {target.upper()} ---")
    exit_code, _ = run_command(deploy_cmd, timeout=120, cwd=cwd)
    if exit_code != 0:
        print(f"[deploy] deploy to {target} failed (exit {exit_code})")
        return False, previous_tag

    print(f"\n[deploy] --- HEALTH CHECK ({target.upper()}) ---")
    # Staging scales to zero — longer timeout and more retries for cold start
    retries = 15 if target == "staging" else 5
    delay = 15.0 if target == "staging" else 10.0
    http_timeout = 120 if target == "staging" else 30
    healthy = verify_health(health_url, retries=retries, delay=delay, timeout=http_timeout)
    if healthy:
        print(f"[deploy] ✅ {target.upper()} healthy")
    else:
        print(f"[deploy] ⚠️  {target.upper()} health check failed")
    return healthy, previous_tag


def rollback_to(commands: dict, target: str, previous_tag: str, cwd: str | None = None) -> bool:
    """
    Roll back staging or prod to a previous image tag.
    Substitutes previous_tag into the stored deploy command and re-runs it.
    """
    deploy_cmd = commands[f"{target}_deploy_command"]
    rollback_cmd = re.sub(r"(--image\s+\S+:)\S+", rf"\g<1>{previous_tag}", deploy_cmd)
    print(f"\n[deploy] --- ROLLBACK {target.upper()} to {previous_tag} ---")
    exit_code, _ = run_command(rollback_cmd, timeout=120, cwd=cwd)
    if exit_code == 0:
        print(f"[deploy] rollback succeeded — {target} is back on tag: {previous_tag}")
    else:
        print(f"[deploy] rollback failed (exit {exit_code}) — manual intervention required")
    return exit_code == 0


# ── convenience full-deploy function (used by CLI) ────────────────────────────

def deploy(repo_path: Path, target: str = "prod") -> tuple[bool, str | None, str | None, dict | None, str | None]:
    """
    Full single-target deploy: read deploy.md → build → deploy to target → health check.
    Used by the standalone CLI and by pipeline for simple single-env runs.

    Returns (success, tag, health_url, commands, previous_tag).
    """
    deploy_md = read_deploy_md(repo_path)
    tag = datetime.now().strftime("%Y%m%d-%H%M%S")

    try:
        commands = generate_all_commands(deploy_md, tag)
    except (ValueError, Exception) as e:
        print(f"[deploy] failed to generate commands: {e}")
        return False, None, None, None, None

    repo_cwd = str(repo_path)

    if not build_image(commands, cwd=repo_cwd):
        return False, tag, commands.get(f"{target}_health_url"), commands, None

    success, previous_tag = deploy_to(commands, target, cwd=repo_cwd)
    return success, tag, commands.get(f"{target}_health_url"), commands, previous_tag


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Deploy agent — reads deploy.md and deploys")
    parser.add_argument("--repo-path", required=True,
                        help="Path to local clone of the target repo")
    parser.add_argument("--target", choices=["staging", "prod"], default="prod",
                        help="Deployment target (default: prod)")
    args = parser.parse_args()

    repo_path = Path(args.repo_path).resolve()
    success, tag, health_url, _commands, _prev = deploy(repo_path, target=args.target)
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
