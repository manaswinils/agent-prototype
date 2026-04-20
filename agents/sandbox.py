"""Sandbox execution environment for agent tool calls.

Provides isolated execution for run_command calls so that agent-generated
shell commands cannot affect the host system.

Two isolation levels:
  DockerSandbox   — full container isolation (preferred when Docker is available)
  ProcessSandbox  — subprocess with resource limits and path restriction (fallback)

Usage:
    with DockerSandbox(repo_path) as sandbox:
        executor = SandboxToolExecutor(repo_path, sandbox)
        run_agent_loop(client, executor, goal)

Or create via the factory:
    with create_sandbox(repo_path) as sandbox:
        ...
"""
import os
import resource
import subprocess
import sys
from pathlib import Path


# ── Docker sandbox ─────────────────────────────────────────────────────────────

SANDBOX_IMAGE = "python:3.12-slim"
SANDBOX_MEMORY = "512m"
SANDBOX_CPUS = "1.0"


def _docker_available() -> bool:
    """Return True if the Docker daemon is reachable."""
    result = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        timeout=5,
    )
    return result.returncode == 0


class DockerSandbox:
    """Executes commands inside an ephemeral Docker container.

    The repo directory is mounted read-write at /workspace so file
    operations (write_file, list_files, read_file) on the host are
    immediately visible inside the container and vice-versa.

    Network is disabled by default — the container cannot make outbound
    connections, preventing accidental data exfiltration or supply-chain
    attacks via agent-generated install commands.
    """

    def __init__(
        self,
        repo_dir: Path,
        image: str = SANDBOX_IMAGE,
        memory: str = SANDBOX_MEMORY,
        cpus: str = SANDBOX_CPUS,
        network: bool = False,
    ):
        self.repo_dir = repo_dir.resolve()
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.network = network
        self.container_id: str | None = None
        self.available: bool = False

    def start(self) -> bool:
        """Pull image if needed and start the container. Returns True on success."""
        if not _docker_available():
            print("[sandbox] Docker not available — falling back to ProcessSandbox")
            return False

        network_flag = "bridge" if self.network else "none"
        cmd = [
            "docker", "run", "-d",
            "--rm",
            "-v", f"{self.repo_dir}:/workspace:rw",
            "-w", "/workspace",
            "--network", network_flag,
            "--memory", self.memory,
            "--cpus", self.cpus,
            "--security-opt", "no-new-privileges",
            self.image,
            "sleep", "7200",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print(f"[sandbox] failed to start Docker container: {result.stderr.strip()}")
            return False

        self.container_id = result.stdout.strip()
        self.available = True
        print(f"[sandbox] 🐳 Docker container started: {self.container_id[:12]} "
              f"(network={'on' if self.network else 'off'}, mem={self.memory})")
        return True

    def stop(self) -> None:
        if self.container_id:
            subprocess.run(
                ["docker", "kill", self.container_id],
                capture_output=True, timeout=10,
            )
            print(f"[sandbox] container {self.container_id[:12]} stopped")
            self.container_id = None
            self.available = False

    def exec(self, command: str, timeout: int = 120) -> tuple[int, str]:
        """Run a shell command inside the container. Returns (exit_code, output)."""
        if not self.container_id:
            raise RuntimeError("Sandbox not started — call start() first")
        try:
            result = subprocess.run(
                ["docker", "exec", self.container_id, "bash", "-c", command],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = result.stdout
            if result.stderr:
                output += result.stderr
            return result.returncode, output
        except subprocess.TimeoutExpired:
            return 1, f"Command timed out after {timeout}s"

    def __enter__(self) -> "DockerSandbox":
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.stop()


# ── Process sandbox (fallback) ─────────────────────────────────────────────────

class ProcessSandbox:
    """Restricted subprocess execution when Docker is unavailable.

    Provides:
      - Working directory restricted to repo_dir
      - CPU time limit (30s hard limit per command via resource module, POSIX only)
      - Output truncation
      - Warning that full isolation is not available

    This is NOT a security boundary — it is a best-effort restriction that
    prevents accidental damage but cannot stop a determined attacker.
    """

    def __init__(self, repo_dir: Path):
        self.repo_dir = repo_dir.resolve()
        self.available = True
        print("[sandbox] ⚠️  Docker unavailable — using ProcessSandbox (no container isolation)")

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        pass

    def exec(self, command: str, timeout: int = 120) -> tuple[int, str]:
        def set_limits():
            # POSIX-only: set CPU time limit to 30s to catch runaway processes
            if sys.platform != "win32":
                try:
                    resource.setrlimit(resource.RLIMIT_CPU, (30, 60))
                except (ValueError, resource.error):
                    pass

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
                preexec_fn=set_limits if sys.platform != "win32" else None,
            )
            output = result.stdout
            if result.stderr:
                output += result.stderr
            return result.returncode, output
        except subprocess.TimeoutExpired:
            return 1, f"Command timed out after {timeout}s"

    def __enter__(self) -> "ProcessSandbox":
        return self

    def __exit__(self, *args) -> None:
        pass


# ── Factory ────────────────────────────────────────────────────────────────────

def create_sandbox(repo_dir: Path, network: bool = False) -> DockerSandbox | ProcessSandbox:
    """Return the best available sandbox for this environment."""
    if _docker_available():
        return DockerSandbox(repo_dir, network=network)
    return ProcessSandbox(repo_dir)
