# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Harness agent implementation."""

from __future__ import annotations

from inspect import isawaitable
from typing import AsyncGenerator
from typing import Any
from typing import Awaitable
from typing import Callable
from typing import Optional
from typing import TypeAlias
from typing import Union
from typing_extensions import override

from pydantic import Field
from pydantic import PrivateAttr

from trpc_agent_sdk.agents import BaseAgent
from trpc_agent_sdk.agents import LlmAgent
from trpc_agent_sdk.context import InvocationContext
from trpc_agent_sdk.events import Event
from trpc_agent_sdk.events import LongRunningEvent
from trpc_agent_sdk.models import LLMModel
from trpc_agent_sdk.tools import BaseTool
from trpc_agent_sdk.tools import BaseToolSet
from trpc_agent_sdk.types import Content

from ._workspace import BaseWorkspace
from ._policy import HarnessPolicy

InstructionProvider = Callable[[InvocationContext], Union[str, Awaitable[str]]]
ToolUnion: TypeAlias = Union[BaseTool, BaseToolSet]
ModelFactory: TypeAlias = Callable[[dict[str, Any]], Awaitable[LLMModel]]


class HarnessAgent(BaseAgent):
    """BaseAgent wrapper that composes an internal LlmAgent."""

    workspace: BaseWorkspace
    """Workspace where harness tools and shell/file execution are scoped."""

    policy: HarnessPolicy
    """Policy that provides prompt shaping, tool injection, and event hooks."""

    model: Union[str, LLMModel, ModelFactory] = ""
    """Model configuration forwarded to the internal LlmAgent."""

    instruction: Union[str, InstructionProvider] = ""
    """Base instruction before policy-specific prompt augmentation."""

    tools: list[ToolUnion] = Field(default_factory=list)
    """Additional user tools appended after policy-provided tools."""

    _inner_agent: LlmAgent = PrivateAttr()
    """Internal execution agent initialized in model_post_init."""

    @override
    def model_post_init(self, __context: Any) -> None:
        super().model_post_init(__context)

        merged_instruction = self._merge_instruction(self.instruction)
        self._inner_agent = LlmAgent(
            name=self.name,
            description=self.description,
            model=self.model,
            instruction=merged_instruction,
            tools=[*self.tools],
        )

    @property
    def inner_agent(self) -> LlmAgent:
        """Returns the wrapped LlmAgent."""
        return self._inner_agent

    @override
    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        await ctx.raise_if_cancelled()

        # Keep build-phase state updates isolated from previous turns.
        ctx.actions.state_delta.clear()

        # Build tools once for this invocation. The internal LlmAgent handles
        # its own tool-reaction loop when disable_react_tool=False.
        policy_tools = await self.policy.build_tools(ctx=ctx, workspace=self.workspace)
        self._inner_agent.tools = [*policy_tools, *self.tools]

        policy_messages = await self.policy.build_messages(ctx=ctx, workspace=self.workspace)

        if ctx.actions.state_delta:
            yield self._create_state_update_event(ctx)
            ctx.actions.state_delta.clear()

        llm_context = self._build_inner_context(
            ctx=ctx,
            override_messages=list(policy_messages) if policy_messages is not None else None,
        )

        async for event in self._inner_agent.run_async(llm_context):
            await self.policy.on_event(ctx=ctx, workspace=self.workspace, event=event)
            if not event.partial and ctx.actions.state_delta:
                event.actions.state_delta.update(ctx.actions.state_delta)
                ctx.actions.state_delta.clear()

            yield event
            await ctx.raise_if_cancelled()

            if isinstance(event, LongRunningEvent):
                return

        if ctx.actions.transfer_to_agent:
            return

    def _merge_instruction(self, instruction: Union[str, InstructionProvider]) -> InstructionProvider:
        """Create the instruction callback consumed by ``self._inner_agent``.

        The returned callback is invoked at runtime by ``LlmAgent`` to resolve
        the base instruction (static or dynamic) and then build the final
        system prompt via ``HarnessPolicy``.
        """

        async def _wrapped(invocation_context: InvocationContext) -> str:
            """Resolve per-invocation system prompt for the inner agent."""
            if isinstance(instruction, str):
                value = instruction
            else:
                value = instruction(invocation_context)
                if isawaitable(value):
                    value = await value
            if not isinstance(value, str):
                value = str(value)
            return await self.policy.build_system_prompt(
                invocation_context,
                self.workspace,
                value,
            )

        return _wrapped

    def _build_inner_context(
        self,
        *,
        ctx: InvocationContext,
        override_messages: Optional[list[Content]],
    ) -> InvocationContext:
        return ctx.model_copy(update={"agent": self._inner_agent, "override_messages": override_messages})

    def _create_state_update_event(self, ctx: InvocationContext) -> Event:
        event = Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=None,
            branch=ctx.branch,
            partial=False,
        )
        event.actions.state_delta = dict(ctx.actions.state_delta)
        return event
