"""Deploy agent: reads deploy.md and deploys to the configured Azure target.

Usage (standalone):
    python deploy_agent.py --repo-path /path/to/local/agent-sandbox

Or called from pipeline.py:
    from deploy_agent import deploy
    success = deploy(repo_path)

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
Generate the exact shell commands to build and deploy the application.

Respond with ONLY a JSON object — no prose, no code fences:
{
  "build_command": "<full az acr build command with the provided tag substituted for <TAG>>",
  "deploy_command": "<full az containerapp update command with the provided tag substituted for <TAG>>",
  "health_url": "<the health check URL from deploy.md>"
}"""


# ── JSON parsing (same three-tier fallback as review_agent.py) ────────────────

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


def generate_deploy_commands(deploy_md_content: str, tag: str) -> dict:
    """
    Call Claude to parse deploy.md and produce concrete az commands.
    The tag is Python-generated for determinism — Claude is instructed to use it verbatim.

    Returns dict with keys: build_command, deploy_command, health_url.
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

    required = {"build_command", "deploy_command", "health_url"}
    missing = required - set(commands.keys())
    if missing:
        raise ValueError(f"Claude response missing keys: {missing}")

    print(f"[deploy] tag:            {tag}")
    print(f"[deploy] build_command:  {commands['build_command']}")
    print(f"[deploy] deploy_command: {commands['deploy_command']}")
    print(f"[deploy] health_url:     {commands['health_url']}")

    return commands


def run_command(cmd: str, timeout: int = 300, cwd: str | None = None) -> tuple[int, str]:
    """
    Run a shell command with live output streaming.
    Returns (exit_code, full_output).
    """
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

        # Stream output line by line
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


def verify_health(url: str, retries: int = 5, delay: float = 10.0) -> bool:
    """
    HTTP GET the URL, expect 200. Retries with delay.
    Uses urllib.request — no extra dependencies.
    Returns True if healthy, False after all retries exhausted.
    """
    print(f"[deploy] verifying health: {url}")
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.urlopen(url, timeout=15)
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


# ── rollback helpers ──────────────────────────────────────────────────────────

def get_current_image_tag(commands: dict, cwd: str | None = None) -> str | None:
    """
    Query the tag of the currently-running image in Azure Container Apps.
    Parses --name and --resource-group from the deploy_command.
    Returns the tag string (e.g. "20260419-223451") or None on failure.
    """
    deploy_cmd = commands.get("deploy_command", "")
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
    # Run silently — suppress per-line streaming for this short query
    try:
        import subprocess as _sp
        result = _sp.run(
            query_cmd, shell=True, capture_output=True, text=True, timeout=30, cwd=cwd
        )
        if result.returncode != 0:
            print(f"[deploy] get_current_image_tag query failed: {result.stderr.strip()}")
            return None
        image = result.stdout.strip()
        if ":" in image:
            tag = image.split(":")[-1]
            print(f"[deploy] current image tag: {tag}")
            return tag
    except Exception as e:
        print(f"[deploy] get_current_image_tag error: {e}")
    return None


def rollback_deploy(commands: dict, previous_tag: str, cwd: str | None = None) -> bool:
    """
    Roll back the Container App to a previous image tag.
    Substitutes previous_tag into the stored deploy_command and re-runs it.
    Returns True if the rollback command succeeded.
    """
    deploy_cmd = commands.get("deploy_command", "")
    # Replace tag portion in --image flag (everything after the last colon in the image ref)
    rollback_cmd = re.sub(r"(--image\s+\S+:)\S+", rf"\g<1>{previous_tag}", deploy_cmd)
    print(f"\n[deploy] --- ROLLBACK to {previous_tag} ---")
    exit_code, _ = run_command(rollback_cmd, timeout=120, cwd=cwd)
    if exit_code == 0:
        print(f"[deploy] rollback succeeded. Running tag: {previous_tag}")
    else:
        print(f"[deploy] rollback failed (exit {exit_code}). Manual intervention required.")
    return exit_code == 0


# ── main deploy function ──────────────────────────────────────────────────────

def deploy(repo_path: Path) -> tuple[bool, str | None, str | None, dict | None, str | None]:
    """
    Full deploy pipeline: read deploy.md → Claude → az acr build →
    az containerapp update → health check.

    Args:
        repo_path: Path to local clone of the target repo (deploy.md must exist).

    Returns:
        (success, tag, health_url, commands, previous_tag)
        commands is kept for rollback use by the caller.
        previous_tag is the tag that was live before this deploy (may be None).
    """
    deploy_md = read_deploy_md(repo_path)

    # Generate tag in Python for determinism — don't let Claude guess the time
    tag = datetime.now().strftime("%Y%m%d-%H%M%S")

    try:
        commands = generate_deploy_commands(deploy_md, tag)
    except (ValueError, Exception) as e:
        print(f"[deploy] failed to generate commands: {e}")
        return False, None, None, None, None

    repo_cwd = str(repo_path)

    # Query currently-deployed tag before we overwrite it
    previous_tag = get_current_image_tag(commands, cwd=repo_cwd)

    # Step 1: build and push image
    print("\n[deploy] --- BUILD ---")
    exit_code, _ = run_command(commands["build_command"], timeout=600, cwd=repo_cwd)
    if exit_code != 0:
        print(f"[deploy] build failed (exit {exit_code})")
        return False, tag, commands.get("health_url"), commands, previous_tag
    print("[deploy] build succeeded")

    # Step 2: update Container App
    print("\n[deploy] --- DEPLOY ---")
    exit_code, _ = run_command(commands["deploy_command"], timeout=120, cwd=repo_cwd)
    if exit_code != 0:
        print(f"[deploy] deploy failed (exit {exit_code})")
        return False, tag, commands.get("health_url"), commands, previous_tag
    print("[deploy] deploy command succeeded")

    # Step 3: health check
    print("\n[deploy] --- HEALTH CHECK ---")
    healthy = verify_health(commands["health_url"])

    if healthy:
        print(f"\n[deploy] ✅ Deployment complete. Tag: {tag}")
        print(f"[deploy]    URL: {commands['health_url']}")
    else:
        print(f"\n[deploy] ⚠️  Deployment may have issues. Check manually.")
        print(f"[deploy]    URL: {commands['health_url']}")

    return healthy, tag, commands["health_url"], commands, previous_tag


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Deploy agent — reads deploy.md and deploys")
    parser.add_argument("--repo-path", required=True,
                        help="Path to local clone of the target repo")
    args = parser.parse_args()

    repo_path = Path(args.repo_path).resolve()
    success, tag, health_url, _commands, _prev = deploy(repo_path)
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
