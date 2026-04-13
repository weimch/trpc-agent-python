# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Base filesystem interface for harness workspace backends."""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from pathlib import Path
from typing import Literal
from typing import Optional

GrepOutputMode = Literal["files_with_matches", "content", "count"]
"""Supported output modes for grep-style searches."""


class BaseFilesystem(ABC):
    """Abstract filesystem contract reusable across workspace implementations."""

    @property
    @abstractmethod
    def root(self) -> Path:
        """Get filesystem root path.

        Returns:
            Path: Absolute root used to scope relative paths.
        """
        ...

    @abstractmethod
    def resolve_path(self, path: str, base_dir: Optional[Path] = None) -> Path:
        """Resolve a user path against root or an optional base directory.

        Args:
            path: Relative or absolute path string.
            base_dir: Optional base directory used for relative paths.

        Returns:
            Path: Normalized absolute path.
        """
        ...

    @abstractmethod
    async def read_file(self, path: str, offset: int = 1, limit: Optional[int] = None) -> str:
        """Read file content with optional line pagination support.

        Args:
            path: File path to read.
            offset: 1-indexed start line.
            limit: Optional maximum number of lines to read.

        Returns:
            str: Formatted file content.

        Raises:
            Exception: Implementations should raise on operation failure.
        """
        ...

    @abstractmethod
    async def write_file(self, path: str, content: str) -> str:
        """Write text content to a file path.

        Args:
            path: Destination file path.
            content: Content to write.

        Returns:
            str: User-facing operation result.

        Raises:
            Exception: Implementations should raise on operation failure.
        """
        ...

    @abstractmethod
    async def edit_file(self, path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
        """Replace text in a file with optional global replacement.

        Args:
            path: File path to edit.
            old_text: Source text to replace.
            new_text: Replacement text.
            replace_all: Whether to replace all occurrences.

        Returns:
            str: User-facing operation result.

        Raises:
            Exception: Implementations should raise on operation failure.
        """
        ...

    @abstractmethod
    async def list_dir(self, path: str, recursive: bool = False, max_entries: int = 200) -> str:
        """List directory entries, optionally recursively.

        Args:
            path: Directory path to list.
            recursive: Whether to recurse subdirectories.
            max_entries: Maximum entries to include.

        Returns:
            str: Formatted directory listing.

        Raises:
            Exception: Implementations should raise on operation failure.
        """
        ...

    @abstractmethod
    async def glob(self, pattern: str, path: str = ".") -> str:
        """Find files by glob pattern from a base path.

        Args:
            pattern: Glob pattern.
            path: Base path to search from.

        Returns:
            str: Formatted matching file list.

        Raises:
            Exception: Implementations should raise on operation failure.
        """
        ...

    @abstractmethod
    async def grep(
        self,
        pattern: str,
        path: Optional[str] = None,
        glob: Optional[str] = None,
        output_mode: GrepOutputMode = "files_with_matches",
    ) -> str:
        """Search file content by regex and return formatted matches.

        Args:
            pattern: Regex pattern to search for.
            path: Optional file or directory path.
            glob: Optional glob filter for candidate files.
            output_mode: Output shape for search results.

        Returns:
            str: Formatted search output.

        Raises:
            Exception: Implementations should raise on operation failure.
        """
        ...
