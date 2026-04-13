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
from trpc_agent_sdk.types import Part

from ._policy import HarnessPolicy
from ._policy import LoopControl
from ._policy import PolicyPlan
from ._policy import RunOutcome
from ._workspace import BaseWorkspace

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

        self._inner_agent = LlmAgent(
            name=self.name,
            description=self.description,
            model=self.model,
            instruction="",
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
        origin_prompt = await self._resolve_origin_instruction(ctx)
        plan = await self.policy.before_run(
            ctx=ctx,
            workspace=self.workspace,
            origin_prompt=origin_prompt,
        )
        if not isinstance(plan, PolicyPlan):
            raise TypeError("HarnessPolicy.before_run() must return PolicyPlan")

        composed_tools: list[ToolUnion] = [*plan.tools, *self.tools]
        override_messages = list(plan.override_messages) if plan.override_messages is not None else None

        self._inner_agent.tools = composed_tools
        self._inner_agent.instruction = plan.system_prompt
        self._inner_agent.parallel_tool_calls = plan.parallel_tool_calls

        max_iterations = max(1, int(plan.max_iterations))
        stop_reason = "completed"
        tools_used: list[str] = []
        final_content: Optional[str] = None
        iterations_run = 0
        previous_disable_react = self._inner_agent.disable_react_tool
        self._inner_agent.disable_react_tool = True

        if ctx.actions.state_delta:
            yield self._create_state_update_event(ctx)
            ctx.actions.state_delta.clear()

        try:
            for iteration in range(max_iterations):
                iterations_run = iteration + 1
                saw_tool_call = False
                round_control: Optional[LoopControl] = None
                force_stop = False

                llm_context = self._build_inner_context(
                    ctx=ctx,
                    override_messages=override_messages,
                )

                async for event in self._inner_agent.run_async(llm_context):
                    function_calls = event.get_function_calls()
                    if function_calls:
                        saw_tool_call = True
                        tools_used.extend(function_call.name for function_call in function_calls if function_call.name)

                    if not event.partial:
                        if event.content and not function_calls and not event.get_function_responses():
                            text = event.get_text()
                            if text:
                                final_content = text

                        # NOTE: Policy ``on_event`` is invoked only for non-partial events.
                        control = await self.policy.on_event(
                            ctx=ctx,
                            workspace=self.workspace,
                            event=event,
                            iteration=iteration,
                        )
                        if control is not None:
                            round_control = control

                        if ctx.actions.state_delta:
                            event.actions.state_delta.update(ctx.actions.state_delta)
                            ctx.actions.state_delta.clear()

                        if override_messages is not None and event.content is not None:
                            # Append after policy hook so any in-place content updates are preserved.
                            cloned_content = (event.content.model_copy(
                                deep=True) if hasattr(event.content, "model_copy") else event.content)
                            override_messages.append(cloned_content)

                    yield event
                    await ctx.raise_if_cancelled()

                    if isinstance(event, LongRunningEvent):
                        stop_reason = "long_running"
                        force_stop = True
                        break

                if force_stop:
                    break

                if ctx.actions.transfer_to_agent:
                    stop_reason = "transfer"
                    break

                if round_control == LoopControl.ABORT:
                    stop_reason = "loop_control_abort"
                    break

                if round_control == LoopControl.FINISH:
                    stop_reason = "loop_control_finish"
                    break

                if not saw_tool_call:
                    stop_reason = "completed"
                    break
            else:
                stop_reason = "max_iterations"
                budget_event = self._create_max_iterations_event(ctx=ctx, max_iterations=max_iterations)
                if ctx.actions.state_delta:
                    budget_event.actions.state_delta.update(ctx.actions.state_delta)
                    ctx.actions.state_delta.clear()
                yield budget_event
                final_content = budget_event.get_text() or final_content
        finally:
            self._inner_agent.disable_react_tool = previous_disable_react

        outcome = RunOutcome(
            final_content=final_content,
            stop_reason=stop_reason,
            iterations=iterations_run,
            tools_used=tools_used,
        )
        await self.policy.after_run(ctx=ctx, workspace=self.workspace, outcome=outcome)

        if ctx.actions.state_delta:
            yield self._create_state_update_event(ctx)
            ctx.actions.state_delta.clear()

        if ctx.actions.transfer_to_agent:
            return

    def _build_inner_context(
        self,
        *,
        ctx: InvocationContext,
        override_messages: Optional[list[Content]],
    ) -> InvocationContext:
        return ctx.model_copy(update={"agent": self._inner_agent, "override_messages": override_messages})

    async def _resolve_origin_instruction(self, ctx: InvocationContext) -> str:
        value: Union[str, Awaitable[str]]
        if isinstance(self.instruction, str):
            value = self.instruction
        else:
            value = self.instruction(ctx)
            if isawaitable(value):
                value = await value
        if not isinstance(value, str):
            return str(value)
        return value

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

    def _create_max_iterations_event(self, *, ctx: InvocationContext, max_iterations: int) -> Event:
        text = (f"I reached the policy iteration limit ({max_iterations}) before producing a final answer. "
                "Please narrow the scope or ask me to continue from the latest state.")
        return Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=Content(role="model", parts=[Part.from_text(text=text)]),
            branch=ctx.branch,
            partial=False,
        )
