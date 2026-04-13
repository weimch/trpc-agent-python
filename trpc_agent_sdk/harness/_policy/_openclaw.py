# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Minimal OpenClaw-style harness policy."""

from __future__ import annotations

from typing import Any
from typing import Optional

from pydantic import BaseModel
from pydantic import Field

from trpc_agent_sdk.context import InvocationContext
from trpc_agent_sdk.events import Event
from trpc_agent_sdk.tools import BaseTool

from .._workspace import BaseWorkspace
from .._tool import EditFileTool
from .._tool import ExecTool
from .._tool import GlobTool
from .._tool import GrepTool
from .._tool import ListDirTool
from .._tool import ReadFileTool
from .._tool import WriteFileTool
from ._base import HarnessPolicy
from ._base import LoopControl
from ._base import PolicyPlan
from ._base import RunOutcome

_OPENCLAW_STATE_KEY = "_trpc_harness_openclaw"


class _OpenClawRuntimeState(BaseModel):
    iteration: int = 0
    tool_failures: int = 0
    last_tool_call: Optional[str] = None
    last_failing_tool: Optional[str] = None


class _OpenClawLastRunState(BaseModel):
    stop_reason: Optional[str] = None
    iterations: int = 0
    tools_used: list[str] = Field(default_factory=list)


class _OpenClawPolicyState(BaseModel):
    version: int = 1
    runtime: _OpenClawRuntimeState = Field(default_factory=_OpenClawRuntimeState)
    last_run: _OpenClawLastRunState = Field(default_factory=_OpenClawLastRunState)


class OpenClawPolicyConfig(BaseModel):
    """Config for ``OpenClawPolicy``."""

    max_iterations: int = Field(default=12, ge=1)
    append_error_hint: bool = True
    error_retry_hint: str = "[Analyze the error above and try a different approach.]"
    enable_web_tools: bool = False
    enable_spawn_tool: bool = False


class OpenClawPolicy(HarnessPolicy):
    """Minimal policy that wires workspace tools and execution guidance."""

    def __init__(self, config: Optional[OpenClawPolicyConfig] = None) -> None:
        self._config = config or OpenClawPolicyConfig()

    async def before_run(
        self,
        ctx: InvocationContext,
        workspace: BaseWorkspace,
        origin_prompt: str = "",
    ) -> PolicyPlan:
        """Build OpenClaw run plan for one harness invocation."""
        tools: list[BaseTool] = [
            ReadFileTool(workspace=workspace),
            WriteFileTool(workspace=workspace),
            EditFileTool(workspace=workspace),
            ListDirTool(workspace=workspace),
            GlobTool(workspace=workspace),
            GrepTool(workspace=workspace),
            ExecTool(workspace=workspace),
        ]
        base_instruction = origin_prompt.strip() if origin_prompt else "You are a coding assistant."
        appendix = ("You have direct tool access to the workspace.\n"
                    f"- Workspace root: {workspace.root}\n"
                    "- Prefer read/list/glob/grep before edits.\n"
                    "- Use edit_file for focused replacements and write_file for full rewrites.\n"
                    "- Use exec only when command output is necessary to validate changes.\n"
                    "- If a tool fails, analyze the error and retry with a different approach.")
        hint = self._config.error_retry_hint if self._config.append_error_hint else None
        return PolicyPlan(
            system_prompt=f"{base_instruction}\n\n{appendix}",
            tools=tools,
            override_messages=ctx.override_messages,
            max_iterations=self._config.max_iterations,
            parallel_tool_calls=False,
            error_retry_hint=hint,
        )

    async def on_event(
        self,
        ctx: InvocationContext,
        workspace: BaseWorkspace,  # pylint: disable=unused-argument
        event: Event,
        iteration: int = 0,
    ) -> Optional[LoopControl]:
        """Track counters and enrich failed tool responses with retry hints."""
        policy_state = self._load_policy_state(ctx)
        runtime = policy_state.runtime

        runtime.iteration = max(
            int(runtime.iteration),
            iteration + 1,
        )

        function_calls = event.get_function_calls()
        if function_calls:
            runtime.last_tool_call = function_calls[-1].name

        failures = 0
        last_failing_tool = None
        retry_hint = self._config.error_retry_hint.strip() if self._config.append_error_hint else ""
        for response in event.get_function_responses():
            if self._is_failed_tool_response(response.response):
                failures += 1
                last_failing_tool = response.name
                self._append_retry_hint_to_response(response.response, retry_hint)
        if failures:
            runtime.tool_failures = int(runtime.tool_failures) + failures
            if last_failing_tool:
                runtime.last_failing_tool = last_failing_tool

        self._save_policy_state(ctx, policy_state)

        return None

    async def after_run(
        self,
        ctx: InvocationContext,
        workspace: BaseWorkspace,  # pylint: disable=unused-argument
        outcome: RunOutcome | None = None,
    ) -> None:
        """Persist compact run summary into state for later introspection."""
        if outcome is None:
            return None
        policy_state = self._load_policy_state(ctx)
        policy_state.last_run = _OpenClawLastRunState(
            stop_reason=outcome.stop_reason,
            iterations=int(outcome.iterations),
            tools_used=[str(name) for name in outcome.tools_used if name],
        )
        self._save_policy_state(ctx, policy_state)
        return None

    @staticmethod
    def _is_failed_tool_response(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        status = payload.get("status")
        if isinstance(status, str) and status.lower() == "failed":
            return True
        error_text = payload.get("error")
        if isinstance(error_text, str) and error_text.strip():
            return True
        return False

    @staticmethod
    def _append_retry_hint_to_response(payload: Any, hint: str) -> None:
        if not hint or not isinstance(payload, dict):
            return None

        message = payload.get("message")
        if isinstance(message, str):
            if hint not in message:
                payload["message"] = f"{message.rstrip()}\n{hint}" if message.strip() else hint
            return None
        payload["message"] = hint
        return None

    def _load_policy_state(self, ctx: InvocationContext) -> _OpenClawPolicyState:
        raw = ctx.state.get(_OPENCLAW_STATE_KEY, {})
        return _OpenClawPolicyState.model_validate(raw)

    @staticmethod
    def _save_policy_state(ctx: InvocationContext, payload: _OpenClawPolicyState) -> None:
        # Assign as one JSON-serializable payload so state delta stays explicit.
        ctx.state[_OPENCLAW_STATE_KEY] = payload.model_dump(mode="json")
