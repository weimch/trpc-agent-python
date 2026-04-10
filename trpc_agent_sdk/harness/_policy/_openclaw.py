# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Minimal OpenClaw-style harness policy."""

from __future__ import annotations

from typing import Optional

from trpc_agent_sdk.context import InvocationContext
from trpc_agent_sdk.events import Event
from trpc_agent_sdk.tools import BaseTool
from trpc_agent_sdk.types import Content

from .._workspace import BaseWorkspace
from .._tool import EditFileTool
from .._tool import ExecTool
from .._tool import GlobTool
from .._tool import GrepTool
from .._tool import ListDirTool
from .._tool import ReadFileTool
from .._tool import WriteFileTool
from ._base import HarnessPolicy


class OpenClawPolicy(HarnessPolicy):
    """Minimal policy that wires workspace tools and execution guidance."""

    async def build_tools(
        self,
        ctx: InvocationContext,  # pylint: disable=unused-argument
        workspace: BaseWorkspace,
    ) -> list[BaseTool]:
        tools: list[BaseTool] = [
            ReadFileTool(workspace=workspace),
            WriteFileTool(workspace=workspace),
            EditFileTool(workspace=workspace),
            ListDirTool(workspace=workspace),
            GlobTool(workspace=workspace),
            GrepTool(workspace=workspace),
            ExecTool(workspace=workspace),
        ]
        return tools

    async def build_system_prompt(
        self,
        ctx: InvocationContext,  # pylint: disable=unused-argument
        workspace: BaseWorkspace,
        origin_prompt: str,
    ) -> str:
        base_instruction = origin_prompt.strip() if origin_prompt else "You are a coding assistant."
        appendix = ("You have direct tool access to the workspace.\n"
                    f"- Workspace root: {workspace.root}\n"
                    "- Prefer read/list/glob/grep before edits.\n"
                    "- Use edit_file for focused replacements and write_file for full rewrites.\n"
                    "- Use exec only when command output is necessary to validate changes.")
        return f"{base_instruction}\n\n{appendix}"

    async def build_messages(
            self,
            ctx: InvocationContext,
            workspace: BaseWorkspace,  # pylint: disable=unused-argument
    ) -> Optional[list[Content]]:
        # NOTE: Policy-specific message/state management can be implemented later via ctx.state.
        if ctx.override_messages is None:
            return None
        return ctx.override_messages

    async def on_event(
            self,
            ctx: InvocationContext,  # pylint: disable=unused-argument
            workspace: BaseWorkspace,  # pylint: disable=unused-argument
            event: Event,  # pylint: disable=unused-argument
    ) -> None:
        # NOTE: Policy-specific state updates can be implemented later via ctx.state.
        return None
