"""Tool call gate primitives for HarnessAgent."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any
from typing import Callable

from pydantic import Field

from trpc_agent_sdk.events import Event
from trpc_agent_sdk.types import Content
from trpc_agent_sdk.types import FunctionCall
from trpc_agent_sdk.types import Part

_TOOL_CALL_CONFIRM_NAME = "_harness_tool_call_confirm"


@dataclass(slots=True)
class ToolCallGateAction:
    """Action returned by static gates and user confirmations."""

    class Type(str, Enum):
        ALLOW = "allow"
        DENY = "deny"

    type: Type = Type.ALLOW
    modified_args: dict[str, Any] | None = None
    deny_response: str | None = None

    @staticmethod
    def allow(modified_args: dict[str, Any] | None = None) -> "ToolCallGateAction":
        return ToolCallGateAction(type=ToolCallGateAction.Type.ALLOW, modified_args=modified_args)

    @staticmethod
    def deny(reason: str = "") -> "ToolCallGateAction":
        return ToolCallGateAction(type=ToolCallGateAction.Type.DENY, deny_response=reason)

    def to_payload(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "modified_args": self.modified_args,
            "deny_response": self.deny_response,
        }

    @staticmethod
    def from_payload(payload: Any) -> "ToolCallGateAction | None":
        if not isinstance(payload, dict):
            return None
        raw_type = payload.get("type")
        if not isinstance(raw_type, str):
            return None
        try:
            action_type = ToolCallGateAction.Type(raw_type)
        except ValueError:
            return None

        modified_args = payload.get("modified_args")
        if modified_args is not None and not isinstance(modified_args, dict):
            modified_args = None

        deny_response = payload.get("deny_response")
        if deny_response is not None and not isinstance(deny_response, str):
            deny_response = str(deny_response)

        return ToolCallGateAction(
            type=action_type,
            modified_args=modified_args,
            deny_response=deny_response,
        )


BeforeToolCallGateFn = Callable[[str, dict[str, Any]], ToolCallGateAction]
AfterToolCallGateFn = Callable[[str, dict[str, Any], Any], Any]


@dataclass(slots=True)
class ToolCallGate:
    """Per-tool two-level gate configuration."""

    before_call: BeforeToolCallGateFn | None = None
    after_call: AfterToolCallGateFn | None = None
    user_confirm: bool = False


@dataclass(slots=True)
class ToolCallConfirm:
    """User response for one pending tool call."""

    call_id: str
    action: ToolCallGateAction

    @staticmethod
    def approve(call_id: str) -> Content:
        return ToolCallConfirm(call_id=call_id, action=ToolCallGateAction.allow()).to_content()

    @staticmethod
    def reject(call_id: str, reason: str = "") -> Content:
        return ToolCallConfirm(call_id=call_id, action=ToolCallGateAction.deny(reason)).to_content()

    @staticmethod
    def modify(call_id: str, modified_args: dict[str, Any]) -> Content:
        return ToolCallConfirm(call_id=call_id, action=ToolCallGateAction.allow(modified_args)).to_content()

    def to_part(self) -> Part:
        part = Part.from_function_response(
            name=_TOOL_CALL_CONFIRM_NAME,
            response={"action": self.action.to_payload()},
        )
        part.function_response.id = self.call_id
        return part

    def to_content(self) -> Content:
        return Content(role="user", parts=[self.to_part()])

    @staticmethod
    def to_content_batch(confirms: list["ToolCallConfirm"]) -> Content:
        return Content(role="user", parts=[confirm.to_part() for confirm in confirms])

    @staticmethod
    def is_confirm(content: Content | None) -> bool:
        if content is None or not content.parts:
            return False
        for part in content.parts:
            function_response = part.function_response
            if function_response is None:
                continue
            if function_response.name == _TOOL_CALL_CONFIRM_NAME and function_response.id:
                return True
        return False

    @staticmethod
    def from_content(content: Content | None) -> list["ToolCallConfirm"]:
        confirms: list[ToolCallConfirm] = []
        if content is None or not content.parts:
            return confirms

        for part in content.parts:
            function_response = part.function_response
            if function_response is None:
                continue
            if function_response.name != _TOOL_CALL_CONFIRM_NAME or not function_response.id:
                continue
            confirm = ToolCallConfirm.from_response_payload(
                call_id=function_response.id,
                payload=function_response.response,
            )
            if confirm is not None:
                confirms.append(confirm)
        return confirms

    @staticmethod
    def from_response_payload(call_id: str, payload: Any) -> "ToolCallConfirm | None":
        if not isinstance(payload, dict):
            return None
        if "action" in payload:
            action = ToolCallGateAction.from_payload(payload.get("action"))
        else:
            action = ToolCallGateAction.from_payload(payload)
        if action is None:
            return None
        return ToolCallConfirm(call_id=call_id, action=action)


class ToolCallConfirmEvent(Event):
    """Pause event emitted when user confirmation is required."""

    pending_calls: list[FunctionCall] = Field(default_factory=list)
    execute_batch: list[FunctionCall] = Field(default_factory=list)
