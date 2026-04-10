# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Workspace-backed harness tools."""

from __future__ import annotations

from typing import Any
from typing import List
from typing import Optional

from trpc_agent_sdk.context import InvocationContext
from trpc_agent_sdk.filter import BaseFilter
from trpc_agent_sdk.tools import BaseTool
from trpc_agent_sdk.types import FunctionDeclaration
from trpc_agent_sdk.types import Schema
from trpc_agent_sdk.types import Type

from .._workspace import BaseWorkspace


class _WorkspaceTool(BaseTool):
    """Shared base for harness tools using a workspace instance."""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        workspace: BaseWorkspace,
        filters_name: Optional[List[str]] = None,
        filters: Optional[List[BaseFilter]] = None,
    ) -> None:
        super().__init__(
            name=name,
            description=description,
            filters_name=filters_name,
            filters=filters,
        )
        self._workspace = workspace


class ReadFileTool(_WorkspaceTool):
    """Read file content from workspace."""

    def __init__(
        self,
        workspace: BaseWorkspace,
        filters_name: Optional[List[str]] = None,
        filters: Optional[List[BaseFilter]] = None,
    ) -> None:
        super().__init__(
            name="read_file",
            description="Read the contents of a file. Returns numbered lines.",
            workspace=workspace,
            filters_name=filters_name,
            filters=filters,
        )

    def _get_declaration(self) -> FunctionDeclaration:
        return FunctionDeclaration(
            name="read_file",
            description="Read the contents of a file. Returns numbered lines.",
            parameters=Schema(
                type=Type.OBJECT,
                properties={
                    "path":
                    Schema(type=Type.STRING, description="The file path to read"),
                    "offset":
                    Schema(
                        type=Type.INTEGER,
                        description="Line number to start reading from (1-indexed)",
                        minimum=1,
                    ),
                    "limit":
                    Schema(
                        type=Type.INTEGER,
                        description="Maximum number of lines to read",
                        minimum=1,
                    ),
                },
                required=["path"],
            ),
        )

    async def _run_async_impl(self, *, tool_context: InvocationContext, args: dict[str, Any]) -> Any:
        path = args.get("path", "")
        offset = int(args.get("offset") or 1)
        limit = args.get("limit")
        return await self._workspace.filesystem().read_file(path=path, offset=offset, limit=limit)


class WriteFileTool(_WorkspaceTool):
    """Write file content into workspace."""

    def __init__(
        self,
        workspace: BaseWorkspace,
        filters_name: Optional[List[str]] = None,
        filters: Optional[List[BaseFilter]] = None,
    ) -> None:
        super().__init__(
            name="write_file",
            description="Write content to a file. Creates parent directories if needed.",
            workspace=workspace,
            filters_name=filters_name,
            filters=filters,
        )

    def _get_declaration(self) -> FunctionDeclaration:
        return FunctionDeclaration(
            name="write_file",
            description="Write content to a file. Creates parent directories if needed.",
            parameters=Schema(
                type=Type.OBJECT,
                properties={
                    "path": Schema(type=Type.STRING, description="The file path to write to"),
                    "content": Schema(type=Type.STRING, description="The content to write"),
                },
                required=["path", "content"],
            ),
        )

    async def _run_async_impl(self, *, tool_context: InvocationContext, args: dict[str, Any]) -> Any:
        path = args.get("path", "")
        content = args.get("content", "")
        return await self._workspace.filesystem().write_file(path=path, content=content)


class EditFileTool(_WorkspaceTool):
    """Replace text in workspace file."""

    def __init__(
        self,
        workspace: BaseWorkspace,
        filters_name: Optional[List[str]] = None,
        filters: Optional[List[BaseFilter]] = None,
    ) -> None:
        super().__init__(
            name="edit_file",
            description="Edit a file by replacing old_text with new_text.",
            workspace=workspace,
            filters_name=filters_name,
            filters=filters,
        )

    def _get_declaration(self) -> FunctionDeclaration:
        return FunctionDeclaration(
            name="edit_file",
            description="Edit a file by replacing old_text with new_text.",
            parameters=Schema(
                type=Type.OBJECT,
                properties={
                    "path": Schema(type=Type.STRING, description="The file path to edit"),
                    "old_text": Schema(type=Type.STRING, description="The text to find and replace"),
                    "new_text": Schema(type=Type.STRING, description="The replacement text"),
                    "replace_all": Schema(
                        type=Type.BOOLEAN,
                        description="Replace all occurrences (default false)",
                    ),
                },
                required=["path", "old_text", "new_text"],
            ),
        )

    async def _run_async_impl(self, *, tool_context: InvocationContext, args: dict[str, Any]) -> Any:
        path = args.get("path", "")
        old_text = args.get("old_text", "")
        new_text = args.get("new_text", "")
        replace_all = bool(args.get("replace_all", False))
        return await self._workspace.filesystem().edit_file(
            path=path,
            old_text=old_text,
            new_text=new_text,
            replace_all=replace_all,
        )


class ListDirTool(_WorkspaceTool):
    """List directory content in workspace."""

    def __init__(
        self,
        workspace: BaseWorkspace,
        filters_name: Optional[List[str]] = None,
        filters: Optional[List[BaseFilter]] = None,
    ) -> None:
        super().__init__(
            name="list_dir",
            description="List directory contents. Set recursive=true for nested entries.",
            workspace=workspace,
            filters_name=filters_name,
            filters=filters,
        )

    def _get_declaration(self) -> FunctionDeclaration:
        return FunctionDeclaration(
            name="list_dir",
            description="List directory contents. Set recursive=true for nested entries.",
            parameters=Schema(
                type=Type.OBJECT,
                properties={
                    "path": Schema(type=Type.STRING, description="The directory path to list"),
                    "recursive": Schema(type=Type.BOOLEAN, description="Recursively list all files"),
                    "max_entries": Schema(
                        type=Type.INTEGER,
                        description="Maximum entries to return",
                        minimum=1,
                    ),
                },
                required=["path"],
            ),
        )

    async def _run_async_impl(self, *, tool_context: InvocationContext, args: dict[str, Any]) -> Any:
        path = args.get("path", "")
        recursive = bool(args.get("recursive", False))
        max_entries = int(args.get("max_entries") or 200)
        return await self._workspace.filesystem().list_dir(path=path, recursive=recursive, max_entries=max_entries)


class GlobTool(_WorkspaceTool):
    """Find files by glob pattern in workspace."""

    def __init__(
        self,
        workspace: BaseWorkspace,
        filters_name: Optional[List[str]] = None,
        filters: Optional[List[BaseFilter]] = None,
    ) -> None:
        super().__init__(
            name="glob",
            description="Find files matching a glob pattern.",
            workspace=workspace,
            filters_name=filters_name,
            filters=filters,
        )

    def _get_declaration(self) -> FunctionDeclaration:
        return FunctionDeclaration(
            name="glob",
            description="Find files matching a glob pattern.",
            parameters=Schema(
                type=Type.OBJECT,
                properties={
                    "pattern": Schema(type=Type.STRING, description="Glob pattern, e.g. '**/*.py'"),
                    "path": Schema(type=Type.STRING, description="Base directory to search from"),
                },
                required=["pattern"],
            ),
        )

    async def _run_async_impl(self, *, tool_context: InvocationContext, args: dict[str, Any]) -> Any:
        pattern = args.get("pattern", "")
        path = args.get("path", ".")
        return await self._workspace.filesystem().glob(pattern=pattern, path=path)


class GrepTool(_WorkspaceTool):
    """Search text pattern in workspace files."""

    def __init__(
        self,
        workspace: BaseWorkspace,
        filters_name: Optional[List[str]] = None,
        filters: Optional[List[BaseFilter]] = None,
    ) -> None:
        super().__init__(
            name="grep",
            description="Search for a text pattern within files.",
            workspace=workspace,
            filters_name=filters_name,
            filters=filters,
        )

    def _get_declaration(self) -> FunctionDeclaration:
        return FunctionDeclaration(
            name="grep",
            description="Search for a text pattern within files.",
            parameters=Schema(
                type=Type.OBJECT,
                properties={
                    "pattern":
                    Schema(type=Type.STRING, description="Pattern to search for"),
                    "path":
                    Schema(type=Type.STRING, description="File or directory to search in"),
                    "glob":
                    Schema(type=Type.STRING, description="Optional glob file filter"),
                    "output_mode":
                    Schema(
                        type=Type.STRING,
                        enum=["files_with_matches", "content", "count"],
                        description="Result format",
                    ),
                },
                required=["pattern"],
            ),
        )

    async def _run_async_impl(self, *, tool_context: InvocationContext, args: dict[str, Any]) -> Any:
        pattern = args.get("pattern", "")
        path = args.get("path")
        glob = args.get("glob")
        output_mode = args.get("output_mode", "files_with_matches")
        return await self._workspace.filesystem().grep(
            pattern=pattern,
            path=path,
            glob=glob,
            output_mode=output_mode,
        )


class ExecTool(_WorkspaceTool):
    """Run shell command in workspace."""

    def __init__(
        self,
        workspace: BaseWorkspace,
        filters_name: Optional[List[str]] = None,
        filters: Optional[List[BaseFilter]] = None,
    ) -> None:
        super().__init__(
            name="exec",
            description="Execute a shell command and return its output.",
            workspace=workspace,
            filters_name=filters_name,
            filters=filters,
        )

    def _get_declaration(self) -> FunctionDeclaration:
        return FunctionDeclaration(
            name="exec",
            description="Execute a shell command and return its output.",
            parameters=Schema(
                type=Type.OBJECT,
                properties={
                    "command":
                    Schema(type=Type.STRING, description="The shell command to execute"),
                    "working_dir":
                    Schema(type=Type.STRING, description="Optional working directory"),
                    "timeout":
                    Schema(
                        type=Type.INTEGER,
                        description="Command timeout in seconds",
                        minimum=1,
                        maximum=600,
                    ),
                },
                required=["command"],
            ),
        )

    async def _run_async_impl(self, *, tool_context: InvocationContext, args: dict[str, Any]) -> Any:
        command = args.get("command", "")
        working_dir = args.get("working_dir")
        timeout = args.get("timeout")
        return await self._workspace.exec(command=command, working_dir=working_dir, timeout=timeout)
