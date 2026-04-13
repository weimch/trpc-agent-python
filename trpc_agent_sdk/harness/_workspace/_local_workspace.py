# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Local workspace implementation for harness."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from trpc_agent_sdk.code_executors import WorkspaceInfo
from trpc_agent_sdk.code_executors import WorkspaceRunProgramSpec
from trpc_agent_sdk.code_executors.local import LocalProgramRunner

from ._base_filesystem import BaseFilesystem
from ._base_workspace import BaseWorkspace
from ._local_filesystem import LocalFilesystem


class LocalWorkspace(BaseWorkspace):
    """Local harness workspace built from filesystem + command runner."""

    _MAX_EXEC_OUTPUT = 10_000

    def __init__(self, root: str | Path):
        """Initialize local filesystem and command runner."""
        self._filesystem = LocalFilesystem(root=root)
        self._runner = LocalProgramRunner()
        self._workspace_info = WorkspaceInfo(id="harness_local_workspace", path=self.root.as_posix())

    @property
    def root(self) -> Path:
        """Return workspace root directory."""
        return self._filesystem.root

    def filesystem(self) -> BaseFilesystem:
        """Return filesystem adapter used by harness tools."""
        return self._filesystem

    async def exec(
        self,
        command: str,
        working_dir: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> str:
        """Execute a shell command inside the workspace.

        Args:
            command: Shell command string.
            working_dir: Optional command working directory.
            timeout: Optional timeout in seconds.

        Returns:
            str: Combined command output with exit code.
        """
        if not command.strip():
            raise ValueError("command is required")

        cwd = (self._filesystem.resolve_path(working_dir) if working_dir else self.root)
        shell = "/bin/bash"
        if not Path(shell).exists():
            shell = "/bin/sh"

        spec = WorkspaceRunProgramSpec(
            cmd=shell,
            args=["-lc", command],
            cwd=cwd.as_posix(),
            timeout=float(timeout) if timeout else 60.0,
        )
        try:
            result = await self._runner.run_program(self._workspace_info, spec)
        except Exception as ex:  # pylint: disable=broad-except
            raise RuntimeError(f"Failed to execute command: {ex}") from ex
        if result.timed_out:
            raise TimeoutError(f"Command timed out after {int(spec.timeout)} seconds")

        parts: list[str] = []
        if result.stdout:
            parts.append(result.stdout)
        if result.stderr and result.stderr.strip():
            parts.append(f"STDERR:\n{result.stderr}")
        parts.append(f"\nExit code: {result.exit_code}")

        output = "\n".join(parts).strip() or "(no output)"
        if len(output) > self._MAX_EXEC_OUTPUT:
            half = self._MAX_EXEC_OUTPUT // 2
            truncated_chars = len(output) - self._MAX_EXEC_OUTPUT
            output = (f"{output[:half]}"
                      f"\n\n... ({truncated_chars:,} chars truncated) ...\n\n"
                      f"{output[-half:]}")
        return output
