# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Harness agent implementation."""

from __future__ import annotations

import json
from inspect import isawaitable
from typing import AsyncGenerator
from typing import Any
from typing import Awaitable
from typing import Callable
from typing import Optional
from typing import TypeAlias
from typing import Union
from typing_extensions import override

from pydantic import BaseModel
from pydantic import Field
from pydantic import PrivateAttr

from trpc_agent_sdk.agents import BaseAgent
from trpc_agent_sdk.agents import LlmAgent
from trpc_agent_sdk.agents.core import create_final_model_response_event
from trpc_agent_sdk.agents.core import get_structured_model_response
from trpc_agent_sdk.context import InvocationContext
from trpc_agent_sdk.events import Event
from trpc_agent_sdk.events import LongRunningEvent
from trpc_agent_sdk.models import LLMModel
from trpc_agent_sdk.tools import BaseTool
from trpc_agent_sdk.tools import BaseToolSet
from trpc_agent_sdk.tools import LongRunningFunctionTool
from trpc_agent_sdk.types import Content
from trpc_agent_sdk.types import FunctionCall
from trpc_agent_sdk.types import Part

from ._core import ToolCallConfirm
from ._core import ToolCallConfirmEvent
from ._core import ToolCallGate
from ._core import ToolCallGateAction
from ._policy import HarnessPolicy
from ._policy import LoopControl
from ._policy import PolicyPlan
from ._policy import RunOutcome
from ._workspace import BaseWorkspace

InstructionProvider = Callable[[InvocationContext], Union[str, Awaitable[str]]]
ToolUnion: TypeAlias = Union[BaseTool, BaseToolSet]
ModelFactory: TypeAlias = Callable[[dict[str, Any]], Awaitable[LLMModel]]

_PENDING_TOOL_CONFIRM_STATE_KEY = "_trpc_harness_tool_call_confirm"


class _PendingToolConfirmState(BaseModel):
    pending_calls: list[FunctionCall] = Field(default_factory=list)
    execute_batch: list[FunctionCall] = Field(default_factory=list)


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
        paused_for_confirm = False

        previous_disable_react = self._inner_agent.disable_react_tool
        previous_disable_tool_execution = self._inner_agent.disable_tool_execution
        self._inner_agent.disable_react_tool = True
        self._inner_agent.disable_tool_execution = True

        if ctx.actions.state_delta:
            yield self._create_state_update_event(ctx)
            ctx.actions.state_delta.clear()

        try:
            for iteration in range(max_iterations):
                iterations_run = iteration + 1
                round_control: Optional[LoopControl] = None
                execute_batch: list[FunctionCall] = []
                resumed_from_pending = False

                pending_state = self._load_pending_tool_confirm_state(ctx)
                if pending_state is not None:
                    resumed_from_pending = True
                    tool_confirms = self._extract_tool_confirms(ctx.user_content)
                    approved_calls: list[FunctionCall] = []
                    denied_calls: list[tuple[FunctionCall, str]] = []
                    still_pending: list[FunctionCall] = []

                    for pending_call in pending_state.pending_calls:
                        call_id = pending_call.id or ""
                        confirm = tool_confirms.get(call_id)
                        if confirm is None:
                            still_pending.append(pending_call)
                            continue

                        action = confirm.action
                        if action.type == ToolCallGateAction.Type.DENY:
                            denied_reason = action.deny_response or (
                                f"Tool call `{pending_call.name}` was denied by user confirmation gate.")
                            denied_calls.append((pending_call, denied_reason))
                            continue

                        if action.modified_args is not None:
                            pending_call.args = action.modified_args
                        approved_calls.append(pending_call)

                    for denied_call, denied_reason in denied_calls:
                        denied_event = self._create_gate_deny_event(
                            ctx=ctx,
                            tool_call=denied_call,
                            deny_reason=denied_reason,
                        )
                        control = await self._prepare_non_partial_event(
                            ctx=ctx,
                            event=denied_event,
                            iteration=iteration,
                            override_messages=override_messages,
                        )
                        if control is not None:
                            round_control = control
                        yield denied_event
                        await ctx.raise_if_cancelled()

                    merged_execute_batch = [*pending_state.execute_batch, *approved_calls]
                    if still_pending:
                        self._save_pending_tool_confirm_state(
                            ctx=ctx,
                            payload=_PendingToolConfirmState(
                                pending_calls=self._clone_function_calls(still_pending),
                                execute_batch=self._clone_function_calls(merged_execute_batch),
                            ),
                        )
                        confirm_event = self._create_confirm_event(
                            ctx=ctx,
                            pending_calls=still_pending,
                            execute_batch=merged_execute_batch,
                        )
                        control = await self._prepare_non_partial_event(
                            ctx=ctx,
                            event=confirm_event,
                            iteration=iteration,
                            override_messages=override_messages,
                        )
                        if control is not None:
                            round_control = control
                        yield confirm_event
                        stop_reason = "tool_call_confirm"
                        paused_for_confirm = True
                        break

                    self._clear_pending_tool_confirm_state(ctx)
                    execute_batch = merged_execute_batch
                else:
                    pending_tool_calls: list[FunctionCall] = []

                    llm_context = self._build_inner_context(
                        ctx=ctx,
                        override_messages=override_messages,
                    )

                    async for event in self._inner_agent.run_async(llm_context):
                        if not event.partial:
                            function_calls = event.get_function_calls()
                            if function_calls:
                                pending_tool_calls.extend(self._clone_function_calls(function_calls))
                                tools_used.extend(function_call.name for function_call in function_calls
                                                  if function_call.name)

                            if event.content and not function_calls and not event.get_function_responses():
                                text = event.get_text()
                                if text:
                                    final_content = text

                            control = await self._prepare_non_partial_event(
                                ctx=ctx,
                                event=event,
                                iteration=iteration,
                                override_messages=override_messages,
                            )
                            if control is not None:
                                round_control = control

                        yield event
                        await ctx.raise_if_cancelled()

                    if round_control == LoopControl.ABORT:
                        stop_reason = "loop_control_abort"
                        break

                    if round_control == LoopControl.FINISH:
                        stop_reason = "loop_control_finish"
                        break

                    if not pending_tool_calls:
                        stop_reason = "completed"
                        break

                    confirm_batch: list[FunctionCall] = []
                    for tool_call in pending_tool_calls:
                        gate = plan.tool_call_gates.get(tool_call.name)
                        action = ToolCallGateAction.allow()
                        if gate is not None and gate.before_call is not None:
                            action = gate.before_call(
                                tool_call.name,
                                dict(self._coerce_call_args(tool_call.args)),
                            )
                            if not isinstance(action, ToolCallGateAction):
                                raise TypeError("ToolCallGate.before_call must return ToolCallGateAction")

                        if action.type == ToolCallGateAction.Type.DENY:
                            denied_reason = action.deny_response or (
                                f"Tool call `{tool_call.name}` was denied by policy gate.")
                            denied_event = self._create_gate_deny_event(
                                ctx=ctx,
                                tool_call=tool_call,
                                deny_reason=denied_reason,
                            )
                            control = await self._prepare_non_partial_event(
                                ctx=ctx,
                                event=denied_event,
                                iteration=iteration,
                                override_messages=override_messages,
                            )
                            if control is not None:
                                round_control = control
                            yield denied_event
                            await ctx.raise_if_cancelled()
                            continue

                        if action.modified_args is not None:
                            tool_call.args = action.modified_args

                        if gate is not None and gate.user_confirm:
                            confirm_batch.append(tool_call)
                        else:
                            execute_batch.append(tool_call)

                    if round_control == LoopControl.ABORT:
                        stop_reason = "loop_control_abort"
                        break

                    if round_control == LoopControl.FINISH:
                        stop_reason = "loop_control_finish"
                        break

                    if confirm_batch:
                        self._save_pending_tool_confirm_state(
                            ctx=ctx,
                            payload=_PendingToolConfirmState(
                                pending_calls=self._clone_function_calls(confirm_batch),
                                execute_batch=self._clone_function_calls(execute_batch),
                            ),
                        )
                        confirm_event = self._create_confirm_event(
                            ctx=ctx,
                            pending_calls=confirm_batch,
                            execute_batch=execute_batch,
                        )
                        control = await self._prepare_non_partial_event(
                            ctx=ctx,
                            event=confirm_event,
                            iteration=iteration,
                            override_messages=override_messages,
                        )
                        if control is not None:
                            round_control = control
                        yield confirm_event
                        stop_reason = "tool_call_confirm"
                        paused_for_confirm = True
                        break

                if round_control == LoopControl.ABORT:
                    stop_reason = "loop_control_abort"
                    break

                if round_control == LoopControl.FINISH:
                    stop_reason = "loop_control_finish"
                    break

                if not execute_batch:
                    # Continue so the next LLM turn can consume deny responses.
                    if resumed_from_pending:
                        continue
                    continue

                extended_tools_processor = self._inner_agent._get_extended_tools_processor(ctx)
                long_running_tool_ids: set[str] = set()
                call_by_id: dict[str, FunctionCall] = {}
                gate_by_call_id: dict[str, ToolCallGate | None] = {}

                for tool_call in execute_batch:
                    if tool_call.name:
                        tools_used.append(tool_call.name)
                    call_id = tool_call.id or ""
                    if call_id:
                        call_by_id[call_id] = tool_call
                        gate_by_call_id[call_id] = plan.tool_call_gates.get(tool_call.name)
                    tool = await extended_tools_processor.find_tool(ctx, tool_call)
                    if tool and isinstance(tool, LongRunningFunctionTool):
                        long_running_tool_ids.add(call_id)

                last_tool_event: Optional[Event] = None
                stop_after_tools = False

                async for tool_event in extended_tools_processor.execute_tools_async(execute_batch, ctx):
                    last_tool_event = tool_event
                    long_running_call: Optional[FunctionCall] = None
                    long_running_response = None

                    if tool_event.content and tool_event.content.parts:
                        for part in tool_event.content.parts:
                            function_response = part.function_response
                            if function_response is None:
                                continue

                            call_id = function_response.id or ""
                            call = call_by_id.get(call_id)
                            gate = gate_by_call_id.get(call_id)
                            if call is not None and gate is not None and gate.after_call is not None:
                                function_response.response = gate.after_call(
                                    call.name,
                                    self._coerce_call_args(call.args),
                                    function_response.response,
                                )

                            if call is not None and call_id in long_running_tool_ids:
                                long_running_call = call
                                long_running_response = function_response

                    control = await self._prepare_non_partial_event(
                        ctx=ctx,
                        event=tool_event,
                        iteration=iteration,
                        override_messages=override_messages,
                    )
                    if control is not None:
                        round_control = control

                    yield tool_event
                    await ctx.raise_if_cancelled()

                    if ctx.actions.transfer_to_agent:
                        ctx.actions.transfer_to_agent = None
                        stop_reason = "transfer"
                        stop_after_tools = True
                        break

                    if long_running_call is not None and long_running_response is not None:
                        long_running_event = LongRunningEvent(
                            invocation_id=ctx.invocation_id,
                            author=self.name,
                            function_call=long_running_call.model_copy(deep=True),
                            function_response=long_running_response.model_copy(deep=True),
                            branch=ctx.branch,
                        )
                        yield long_running_event
                        stop_reason = "long_running"
                        stop_after_tools = True
                        break

                if stop_after_tools:
                    break

                if json_response := get_structured_model_response(last_tool_event):
                    final_event = create_final_model_response_event(ctx, json_response)
                    self._inner_agent._save_output_to_state(ctx, final_event)
                    control = await self._prepare_non_partial_event(
                        ctx=ctx,
                        event=final_event,
                        iteration=iteration,
                        override_messages=override_messages,
                    )
                    if control is not None:
                        round_control = control
                    yield final_event
                    stop_reason = "completed"
                    break

                if round_control == LoopControl.ABORT:
                    stop_reason = "loop_control_abort"
                    break

                if round_control == LoopControl.FINISH:
                    stop_reason = "loop_control_finish"
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
            self._inner_agent.disable_tool_execution = previous_disable_tool_execution

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

        if paused_for_confirm:
            return

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

    async def _prepare_non_partial_event(
        self,
        *,
        ctx: InvocationContext,
        event: Event,
        iteration: int,
        override_messages: Optional[list[Content]],
    ) -> Optional[LoopControl]:
        control = await self.policy.on_event(
            ctx=ctx,
            workspace=self.workspace,
            event=event,
            iteration=iteration,
        )

        if ctx.actions.state_delta:
            event.actions.state_delta.update(ctx.actions.state_delta)
            ctx.actions.state_delta.clear()

        if override_messages is not None and event.content is not None:
            cloned_content = (event.content.model_copy(deep=True)
                              if hasattr(event.content, "model_copy") else event.content)
            override_messages.append(cloned_content)

        return control

    @staticmethod
    def _coerce_call_args(raw_args: Any) -> dict[str, Any]:
        if isinstance(raw_args, dict):
            return raw_args
        if isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args)
            except json.JSONDecodeError:
                return {}
            if isinstance(parsed, dict):
                return parsed
        return {}

    @staticmethod
    def _clone_function_calls(calls: list[FunctionCall]) -> list[FunctionCall]:
        return [call.model_copy(deep=True) if hasattr(call, "model_copy") else call for call in calls]

    def _extract_tool_confirms(self, user_content: Optional[Content]) -> dict[str, ToolCallConfirm]:
        confirms: dict[str, ToolCallConfirm] = {}
        if not ToolCallConfirm.is_confirm(user_content):
            return confirms

        for confirm in ToolCallConfirm.from_content(user_content):
            confirms[confirm.call_id] = confirm
        return confirms

    def _create_gate_deny_event(
        self,
        *,
        ctx: InvocationContext,
        tool_call: FunctionCall,
        deny_reason: str,
    ) -> Event:
        response_part = Part.from_function_response(
            name=tool_call.name or "unknown_tool",
            response={"error": deny_reason},
        )
        response_part.function_response.id = tool_call.id or "unknown_tool_call_id"
        return Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=Content(role="user", parts=[response_part]),
            branch=ctx.branch,
            partial=False,
        )

    def _create_confirm_event(
        self,
        *,
        ctx: InvocationContext,
        pending_calls: list[FunctionCall],
        execute_batch: list[FunctionCall],
    ) -> ToolCallConfirmEvent:
        return ToolCallConfirmEvent(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=None,
            branch=ctx.branch,
            partial=False,
            pending_calls=self._clone_function_calls(pending_calls),
            execute_batch=self._clone_function_calls(execute_batch),
        )

    def _load_pending_tool_confirm_state(self, ctx: InvocationContext) -> Optional[_PendingToolConfirmState]:
        raw = ctx.state.get(_PENDING_TOOL_CONFIRM_STATE_KEY)
        if not isinstance(raw, dict):
            return None
        try:
            return _PendingToolConfirmState.model_validate(raw)
        except Exception:  # pylint: disable=broad-except
            return None

    def _save_pending_tool_confirm_state(self, *, ctx: InvocationContext, payload: _PendingToolConfirmState) -> None:
        ctx.state[_PENDING_TOOL_CONFIRM_STATE_KEY] = payload.model_dump(mode="json")

    def _clear_pending_tool_confirm_state(self, ctx: InvocationContext) -> None:
        ctx.state[_PENDING_TOOL_CONFIRM_STATE_KEY] = None

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
