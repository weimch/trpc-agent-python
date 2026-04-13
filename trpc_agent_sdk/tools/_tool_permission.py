"""Tool permission primitives for tool invocation governance."""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING
from typing import Any
from typing_extensions import override

from trpc_agent_sdk.context import AgentContext
from trpc_agent_sdk.context import InvocationContext
from trpc_agent_sdk.filter import BaseFilter
from trpc_agent_sdk.filter import FilterResult
from trpc_agent_sdk.filter import FilterType

if TYPE_CHECKING:
    from ._base_tool import BaseTool


class ToolCallAction(Enum):
    """Action returned by `ToolPermission.before_tool_call`."""

    ALLOW = "allow"
    DENY = "deny"
    MODIFY = "modify"


@dataclass
class ToolCallDecision:
    """Decision object returned by `ToolPermission.before_tool_call`."""

    action: ToolCallAction = ToolCallAction.ALLOW
    modified_args: dict[str, Any] | None = None
    deny_response: dict[str, Any] | None = None


class ToolPermission(ABC):
    """Permission hooks for a single tool invocation."""

    async def before_tool_call(
        self,
        context: InvocationContext,
        tool: BaseTool,
        args: dict[str, Any],
    ) -> ToolCallDecision:
        """Inspect and optionally gate/modify a tool call before execution."""
        return ToolCallDecision(action=ToolCallAction.ALLOW)

    async def after_tool_call(
        self,
        context: InvocationContext,
        tool: BaseTool,
        args: dict[str, Any],
        result: Any,
    ) -> Any:
        """Inspect and optionally transform a tool result after execution."""
        return result


class _ToolPermissionFilter(BaseFilter):
    """Filter adapter that executes `ToolPermission` hooks."""

    def __init__(
        self,
        *,
        permission: ToolPermission,
        tool: BaseTool,
        invocation_context: InvocationContext,
    ):
        super().__init__()
        self._type = FilterType.TOOL
        self._name = "tool_permission"
        self._permission = permission
        self._tool = tool
        self._invocation_context = invocation_context

    @override
    async def _before(self, ctx: AgentContext, req: Any, rsp: FilterResult) -> None:
        if not isinstance(req, dict):
            return

        decision = await self._permission.before_tool_call(
            context=self._invocation_context,
            tool=self._tool,
            args=req,
        )

        if decision.action == ToolCallAction.DENY:
            rsp.rsp = decision.deny_response or {
                "error": f"TOOL_PERMISSION_DENIED: tool `{self._tool.name}` denied by permission policy",
            }
            rsp.error = None
            rsp.is_continue = False
            return

        if decision.action == ToolCallAction.MODIFY and decision.modified_args is not None:
            req.clear()
            req.update(decision.modified_args)

    @override
    async def _after(self, ctx: AgentContext, req: Any, rsp: FilterResult) -> None:
        if not isinstance(req, dict):
            return

        rsp.rsp = await self._permission.after_tool_call(
            context=self._invocation_context,
            tool=self._tool,
            args=req,
            result=rsp.rsp,
        )
