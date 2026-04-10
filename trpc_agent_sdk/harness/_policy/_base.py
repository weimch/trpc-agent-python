# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Policy abstractions for harness agents."""

from __future__ import annotations

from typing import Optional

from trpc_agent_sdk.context import InvocationContext
from trpc_agent_sdk.events import Event
from trpc_agent_sdk.tools import BaseTool
from trpc_agent_sdk.types import Content

from .._workspace import BaseWorkspace


class HarnessPolicy:
    """Base policy hooks used by ``HarnessAgent``.

    Subclasses can override these methods to control tool injection, prompt
    shaping, message composition, and event-time state updates.
    """

    async def build_tools(self, ctx: InvocationContext, workspace: BaseWorkspace) -> list[BaseTool]:
        """Build tools injected into ``HarnessAgent`` for this invocation."""
        return []

    async def build_system_prompt(
        self,
        ctx: InvocationContext,  # pylint: disable=unused-argument
        workspace: BaseWorkspace,  # pylint: disable=unused-argument
        origin_prompt: str,
    ) -> str:
        """Return final system prompt text used by the inner LLM agent."""
        return origin_prompt

    async def build_messages(
            self,
            ctx: InvocationContext,
            workspace: BaseWorkspace,  # pylint: disable=unused-argument
    ) -> Optional[list[Content]]:
        """Return override messages used for the internal LLM call."""
        return ctx.override_messages

    async def on_event(
            self,
            ctx: InvocationContext,  # pylint: disable=unused-argument
            workspace: BaseWorkspace,  # pylint: disable=unused-argument
            event: Event,  # pylint: disable=unused-argument
    ) -> None:
        """Observe each streamed event and optionally update policy state."""
        return None
