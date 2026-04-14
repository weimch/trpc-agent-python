# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Policy abstractions for harness agents."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from trpc_agent_sdk.context import InvocationContext
from trpc_agent_sdk.events import Event
from trpc_agent_sdk.tools import BaseTool
from trpc_agent_sdk.types import Content

from .._core import ToolCallGate
from .._workspace import BaseWorkspace


@dataclass(slots=True)
class PolicyPlan:
    """Per-run policy plan resolved in ``before_run``."""

    system_prompt: str
    tools: list[BaseTool]
    tool_call_gates: dict[str, ToolCallGate]
    override_messages: Optional[list[Content]] = None
    max_iterations: int = 12
    parallel_tool_calls: bool = False
    error_retry_hint: Optional[str] = None


class LoopControl(str, Enum):
    """Loop control decisions returned by ``on_event``."""

    CONTINUE = "continue"
    FINISH = "finish"
    ABORT = "abort"


@dataclass(slots=True)
class RunOutcome:
    """Outcome summary passed into ``after_run``."""

    final_content: Optional[str]
    stop_reason: str
    iterations: int
    tools_used: list[str]


class HarnessPolicy:
    """Three-hook policy interface used by ``HarnessAgent``."""

    async def before_run(
        self,
        ctx: InvocationContext,  # pylint: disable=unused-argument
        workspace: BaseWorkspace,  # pylint: disable=unused-argument
        origin_prompt: str = "",
    ) -> PolicyPlan:
        """Build a policy plan for one harness invocation."""
        return PolicyPlan(
            system_prompt=origin_prompt,
            tools=[],
            tool_call_gates={},
            override_messages=ctx.override_messages,
        )

    async def on_event(
        self,
        ctx: InvocationContext,  # pylint: disable=unused-argument
        workspace: BaseWorkspace,  # pylint: disable=unused-argument
        event: Event,  # pylint: disable=unused-argument
        iteration: int = 0,  # pylint: disable=unused-argument
    ) -> Optional[LoopControl]:
        """Observe non-partial events and optionally control loop progress.

        NOTE:
            ``HarnessAgent`` only calls this hook for non-partial events.
        """
        return None

    async def after_run(
        self,
        ctx: InvocationContext,  # pylint: disable=unused-argument
        workspace: BaseWorkspace,  # pylint: disable=unused-argument
        outcome: RunOutcome | None = None,  # pylint: disable=unused-argument
    ) -> None:
        """Post-run hook for final bookkeeping (metrics/memory/etc.)."""
        return None
