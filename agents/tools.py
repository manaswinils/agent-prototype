"""Tool definitions and implementations for the coding agent."""
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents.sandbox import DockerSandbox, ProcessSandbox

# JSON schemas Claude sees — these tell the model what tools exist and how to call them.
TOOL_SCHEMAS = [
    {
        "name": "list_files",
        "description": "List files and directories at a given path inside the working repo. Use this to explore the repo structure before reading or writing files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path inside the repo. Use '.' for the repo root.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the full contents of a file inside the working repo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the file from the repo root.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create a new file or overwrite an existing one with the given content. Use this to make code changes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the file from the repo root.",
                },
                "content": {
                    "type": "string",
                    "description": "Full content to write to the file.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command inside the repo directory. Use for running tests, installing deps, checking syntax. Do not use for git operations — those are handled automatically.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run. Example: 'python -m pytest' or 'ls -la'.",
                }
            },
            "required": ["command"],
        },
    },
]


class ToolExecutor:
    """Executes tool calls scoped to a working directory. Refuses to escape it."""

    def __init__(self, repo_dir: Path):
        self.repo_dir = repo_dir.resolve()

    def _resolve_safe(self, relative_path: str) -> Path:
        """Resolve a path and refuse if it escapes the repo directory."""
        target = (self.repo_dir / relative_path).resolve()
        if not str(target).startswith(str(self.repo_dir)):
            raise ValueError(f"Path {relative_path} escapes the repo directory")
        return target

    def list_files(self, path: str) -> str:
        target = self._resolve_safe(path)
        if not target.exists():
            return f"Error: path does not exist: {path}"
        if not target.is_dir():
            return f"Error: not a directory: {path}"
        entries = []
        for entry in sorted(target.iterdir()):
            # Skip noise
            if entry.name in {".git", "__pycache__", "node_modules", ".venv", "venv"}:
                continue
            suffix = "/" if entry.is_dir() else ""
            entries.append(f"{entry.name}{suffix}")
        return "\n".join(entries) if entries else "(empty directory)"

    def read_file(self, path: str) -> str:
        target = self._resolve_safe(path)
        if not target.exists():
            return f"Error: file does not exist: {path}"
        if not target.is_file():
            return f"Error: not a file: {path}"
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"Error: file is not UTF-8 text: {path}"
        # Truncate very large files to keep token use sane
        if len(content) > 50_000:
            content = content[:50_000] + "\n\n[... truncated, file is larger than 50k chars ...]"
        return content

    def write_file(self, path: str, content: str) -> str:
        target = self._resolve_safe(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} chars to {path}"

    def run_command(self, command: str) -> str:
        # Block git so the agent doesn't fight our orchestration
        forbidden = ("git ", "git\t")
        if command.strip().startswith(forbidden) or command.strip() == "git":
            return "Error: git commands are handled automatically. Do not run git yourself."
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return "Error: command timed out after 60 seconds"
        output = f"exit_code: {result.returncode}\n"
        if result.stdout:
            output += f"stdout:\n{result.stdout[:5000]}\n"
        if result.stderr:
            output += f"stderr:\n{result.stderr[:5000]}\n"
        return output

    def dispatch(self, tool_name: str, tool_input: dict) -> str:
        """Route a tool_use block to the right method."""
        try:
            if tool_name == "list_files":
                return self.list_files(tool_input["path"])
            if tool_name == "read_file":
                return self.read_file(tool_input["path"])
            if tool_name == "write_file":
                path = tool_input.get("path", "")
                content = tool_input.get("content")
                if content is None:
                    return (
                        f"Error: write_file called without 'content' for '{path}'. "
                        "This happens when the file content is too large to fit in one response. "
                        "Split the file into smaller logical sections and write each part separately, "
                        "OR write a shorter version of the file."
                    )
                return self.write_file(path, content)
            if tool_name == "run_command":
                return self.run_command(tool_input["command"])
            return f"Error: unknown tool '{tool_name}'"
        except Exception as e:
            return f"Error executing {tool_name}: {e}"


class SandboxToolExecutor(ToolExecutor):
    """ToolExecutor that routes run_command through an isolated sandbox.

    File operations (list_files, read_file, write_file) still operate directly
    on the host filesystem — the repo clone is the shared state between the
    agent and the sandbox. Only shell execution is sandboxed.

    Args:
        repo_dir: The local repo clone (shared between host and sandbox).
        sandbox:  A started DockerSandbox or ProcessSandbox instance.
    """

    def __init__(self, repo_dir: Path, sandbox: "DockerSandbox | ProcessSandbox"):
        super().__init__(repo_dir)
        self.sandbox = sandbox

    def run_command(self, command: str) -> str:
        """Execute the command inside the sandbox."""
        forbidden = ("git ", "git\t")
        if command.strip().startswith(forbidden) or command.strip() == "git":
            return "Error: git commands are handled automatically. Do not run git yourself."
        try:
            exit_code, output = self.sandbox.exec(command, timeout=60)
            result = f"exit_code: {exit_code}\n"
            if output:
                result += f"stdout:\n{output[:5000]}\n"
            return result
        except Exception as e:
            return f"Error executing command in sandbox: {e}"
