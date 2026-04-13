# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Base workspace interface for harness runtime."""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from pathlib import Path
from typing import Optional

from ._base_filesystem import BaseFilesystem


class BaseWorkspace(ABC):
    """Abstract workspace contract used by harness tools/policies."""

    @property
    @abstractmethod
    def root(self) -> Path:
        """Get workspace root path.

        Returns:
            Path: Absolute workspace root used as the default execution base.
        """
        ...

    @abstractmethod
    def filesystem(self) -> BaseFilesystem:
        """Get filesystem adapter for workspace file operations.

        Returns:
            BaseFilesystem: Filesystem implementation bound to this workspace.
        """
        ...

    @abstractmethod
    async def exec(
        self,
        command: str,
        working_dir: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> str:
        """Execute a shell command inside the workspace.

        Args:
            command: Shell command to execute.
            working_dir: Optional working directory (relative or absolute).
            timeout: Optional timeout in seconds.

        Returns:
            str: User-facing command output message.

        Raises:
            Exception: Implementations should raise on operation failure.
        """
        ...
