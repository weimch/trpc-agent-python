# Step 2: OpenClaw-Like Policy (Incremental)

## Goal

Design an OpenClaw-like harness policy by referencing `ref/nanobot/nanobot/agent/loop.py`, while keeping the first implementation small and low-risk.

First version focuses on:
- policy-controlled loop budget,
- OpenClaw-style prompt/tool composition,
- actionable tool-error feedback.

It intentionally does **not** include memory consolidation, cron, or full subagent orchestration yet.

## What We Reuse From Nanobot

Nanobot loop design gives three practical signals:

1. bounded loop control (`max_iterations`) to avoid infinite agent/tool ping-pong,
2. explicit run objects (`AgentRunSpec`, `AgentRunResult`) rather than implicit state spread across hooks,
3. standardized tool error hint appending for self-correction.

## Required Harness Design Change

Current harness design splits policy behavior into many micro-hooks.  
For OpenClaw-like behavior, we use **three hooks only**:
- `before_run`
- `on_event`
- `after_run`

with explicit data objects: `PolicyPlan`, `LoopControl`, `RunOutcome`.

### 1) Policy API: `PolicyPlan + LoopControl + RunOutcome`

`_policy/_base.py`

```python
@dataclass(slots=True)
class PolicyPlan:
    system_prompt: str
    tools: list[BaseTool]
    override_messages: list[Content] | None = None
    max_iterations: int = 12
    parallel_tool_calls: bool = False
    error_retry_hint: str | None = None


class LoopControl(Enum):
    CONTINUE = "continue"
    FINISH = "finish"
    ABORT = "abort"


@dataclass(slots=True)
class RunOutcome:
    final_content: str | None
    stop_reason: str
    iterations: int
    tools_used: list[str]


class HarnessPolicy:
    async def before_run(self, ctx, workspace, origin_prompt) -> PolicyPlan:
        ...

    async def on_event(self, ctx, workspace, event, iteration) -> LoopControl | None:
        # NOTE: on_event is invoked only for non-partial events.
        ...

    async def after_run(self, ctx, workspace, outcome: RunOutcome) -> None:
        ...
```

Naming note:
- Nanobot uses `AgentRunResult`; we use `RunOutcome` for the same role.
- We intentionally avoid `PolicyReport` naming.

### 2) Managed Loop in `HarnessAgent`

`_agent.py`

Managed-loop behavior:

1. resolve base instruction,
2. call `policy.before_run(...)` to get `PolicyPlan`,
3. apply plan to inner agent,
4. set `inner_agent.disable_react_tool = True`,
5. execute inner agent round-by-round up to `plan.max_iterations`.

Per round:
- stream partial events directly,
- call `policy.on_event(...)` only for non-partial events,
- apply `LoopControl` decision.

Stop conditions:
- `LoopControl.FINISH` or `LoopControl.ABORT`,
- no new tool calls in a round,
- long-running/transfer interruption,
- `max_iterations` reached.

End:
- build `RunOutcome`,
- call `policy.after_run(...)`.

### 3) Tool Error Hint Decorator

`_tool/_decorators.py` (new internal helper)

Add a thin wrapper:
- if tool result indicates failure (`"Error:"` prefix or error object), append configured hint:
  `"[Analyze the error above and try a different approach.]"`.

Controlled by `PolicyPlan.error_retry_hint`.

## OpenClaw Policy V1 Design

### Config Object

`_policy/_openclaw.py`

```python
class OpenClawPolicyConfig(BaseModel):
    max_iterations: int = 12
    append_error_hint: bool = True
    error_retry_hint: str = "[Analyze the error above and try a different approach.]"
    enable_web_tools: bool = False
    enable_spawn_tool: bool = False
```

Notes:
- `12` is a conservative default for first rollout.
- Later versions can raise to `40` to mirror nanobot defaults.

### Hook Behavior

1. `before_run`
   - returns `PolicyPlan` with:
     - OpenClaw-style prompt,
     - workspace toolset (`read_file`, `write_file`, `edit_file`, `list_dir`, `glob`, `grep`, `exec`),
     - iteration budget and optional error hint.
2. `on_event` (non-partial only)
   - tracks lightweight counters in `ctx.state`:
     - loop iteration count,
     - tool failure count,
     - last failing tool name.
   - may return `FINISH`/`ABORT` for policy-based early stop.
3. `after_run`
   - v1 no-op (reserved for metrics/memory trigger in later stages).

## Out of Scope for V1 (Later)

- memory consolidation loop (`MEMORY.md` + `HISTORY.md`),
- background subagent lifecycle,
- cron/heartbeat,
- MCP dynamic connect lifecycle,
- full progress-channel protocol parity.

## Implementation Order

1. Add `PolicyPlan`, `LoopControl`, `RunOutcome` and three-hook base policy.
2. Implement managed-loop path in `HarnessAgent` (non-partial `on_event` contract).
3. Add tool error-hint decorator.
4. Upgrade `OpenClawPolicy` to emit `PolicyPlan`.
5. Add tests:
   - loop stops at `max_iterations`,
   - `on_event` is not called for partial events,
   - decorator appends retry hint on tool errors.

## Why This First Slice

- Preserves nanobot’s core control behavior with minimal complexity.
- Keeps harness API small and coherent (three hooks).
- Creates a stable base for later memory/subagent features.
