# Step 0: Basic Harness Foundation

## Goal

Build the minimum harness foundation so later policy behaviors (OpenClaw, Deep, custom) can be added incrementally.

## Project Structure

```
trpc_agent_sdk/harness/
├── __init__.py                    # Public exports
├── _agent.py                      # HarnessAgent (agent loop)
├── _core/
│   ├── __init__.py
│   └── _tool_call_gate.py         # ToolCallGate, ToolCallGateAction
├── _policy/
│   ├── __init__.py
│   ├── _base.py                   # HarnessPolicy (abstract hooks)
│   └── _openclaw/
│       ├── __init__.py
│       └── _policy.py             # OpenClawPolicy (bootstrap policy)
├── _workspace/
│   ├── __init__.py
│   ├── _base_filesystem.py        # BaseFilesystem (abstract)
│   ├── _base_workspace.py         # BaseWorkspace (abstract)
│   └── _local/
│       ├── __init__.py
│       ├── _filesystem.py         # LocalFilesystem
│       └── _workspace.py          # LocalWorkspace
└── _tool/
    ├── __init__.py
    └── _filesystem.py             # Workspace-backed tool definitions
```

Internal files are prefixed with `_`. User-facing types are re-exported from `__init__.py`.

## Abstract Interfaces

### BaseFilesystem

`_workspace/_base_filesystem.py`

Abstract filesystem contract reusable across workspace implementations. All methods accept string paths and return string results (user-facing messages, not raw data).

```python
class BaseFilesystem(ABC):
    root: Path                      # Absolute root used to scope relative paths
    resolve_path(path, base_dir)    # Resolve user path against root or optional base
    read_file(path, offset, limit)  # Read with 1-indexed line pagination
    write_file(path, content)       # Write full content
    edit_file(path, old_text, new_text, replace_all)  # Find-and-replace edit
    list_dir(path, recursive, max_entries)             # Directory listing
    glob(pattern, path)             # Glob file search
    grep(pattern, path, glob, output_mode)             # Regex content search
```

`GrepOutputMode = Literal["files_with_matches", "content", "count"]`

Design decisions:
- All return types are `str` — tool output goes directly to the LLM, so the filesystem layer formats its own output rather than returning structured data.
- `resolve_path` is synchronous because path resolution is pure computation (no I/O).
- `edit_file` uses find-and-replace rather than line-based patching — LLMs produce more reliable edits with text matching than line number targeting.

### BaseWorkspace

`_workspace/_base_workspace.py`

Abstract workspace contract used by harness tools and policies. Composes a filesystem backend with shell execution.

```python
class BaseWorkspace(ABC):
    root: Path                          # Workspace root directory
    filesystem() -> BaseFilesystem      # Filesystem adapter for file operations
    exec(command, working_dir, timeout) # Shell command execution
```

Design decisions:
- Workspace owns both file and exec — policies/tools get a single workspace reference rather than separate filesystem + shell handles.
- `exec` returns `str` for the same reason as filesystem: tool output goes to the LLM.
- Shell execution is internalized (no `shell: LocalShell` parameter) — the workspace implementation decides how to run commands. Different workspace types (local, remote, sandbox) provide different exec backends.

### HarnessPolicy

`_policy/_base.py`

Base policy with three lifecycle hooks. Subclasses override these hooks to control per-run plan assembly, loop control, and post-run bookkeeping.

```python
class HarnessPolicy:
    before_run(ctx, workspace, origin_prompt) -> PolicyPlan
    on_event(ctx, workspace, event, iteration) -> LoopControl | None
    # NOTE: on_event is called only for non-partial events.
    after_run(ctx, workspace, outcome: RunOutcome) -> None
```

Design decisions:
- `before_run` centralizes policy wiring into one object (`PolicyPlan`): tools, prompt, messages, and loop knobs are assembled together for one invocation.
- `on_event` returns `LoopControl` to decide loop continuation. This moves OpenClaw-style stop/continue logic into policy.
- `on_event` runs on non-partial events only, so policy logic does not depend on token streaming noise.
- `after_run` receives `RunOutcome` (nanobot-aligned naming, similar role to `AgentRunResult`) for cleanup/metrics/memory triggers.
- Base class provides pass-through defaults so minimal policies can override only `before_run`.

## Agent Loop

### HarnessAgent

`_agent.py`

`HarnessAgent` extends `BaseAgent` and wraps an internal `LlmAgent`. The inner agent runs with `disable_tool_execution=True`, so it yields LLM responses (including function_call parts) but does not execute tools. HarnessAgent's outer loop owns tool execution — it resolves `ToolCallGate` per tool to gate each call before invoking `ToolsProcessor`.

```python
class HarnessAgent(BaseAgent):
    workspace: BaseWorkspace       # Scoped file/shell execution
    policy: HarnessPolicy          # Hook-based policy
    model: str | LLMModel          # Forwarded to inner agent
    instruction: str | Callable    # Base instruction (static or dynamic)
    tools: list[ToolUnion]         # Additional user tools
    _inner_agent: LlmAgent         # Internal execution agent (disable_tool_execution=True)
```

**Invocation flow** (`_run_async_impl`):

```
1. Resolve base instruction value (static or callback result)
2. Build plan:    policy.before_run(ctx, workspace, origin_prompt) → PolicyPlan
3. Apply plan:    _inner_agent tools/prompt/messages from plan
                  _inner_agent.disable_tool_execution = True

4. Outer loop:
   a. Stream inner agent (LLM call only, no tool execution):
        for event from inner_agent.run_async(ctx):
          → if event.partial: yield passthrough only
          → else:
            - track pending_tool_calls from function_call parts
            - decision = policy.on_event(ctx, workspace, event, iteration)
            - yield event

   b. If no pending_tool_calls → break (LLM finished, no tools requested)

   c. Level 1 — Static gate:
        for each call: look up ToolCallGate, run before_call
        DENY → create deny response, append to session
        ALLOW → apply modified_args if any, continue to level 2

   d. Level 2 — User gate:
        for each surviving call: check gate.user_confirm
        user_confirm=True  → add to confirm_batch
        user_confirm=False → add to execute_batch

   e. If confirm_batch is not empty:
        yield ToolCallConfirmEvent(pending_calls=confirm_batch)
        return (PAUSE — user resumes via next runner.run_async call)

   f. Execute allowed tools via ToolsProcessor:
        for event from tools_processor.execute_tools_async(execute_batch):
          yield event, append to session

   g. Level 1 post-processing:
        for each executed tool with gate.after_call:
          run after_call to inspect/modify result

   h. Continue → next iteration (inner agent sees tool results, calls LLM again)

   i. Stop when: decision=FINISH/ABORT, transfer/long-running, or max_iterations reached

5. Finalize:     policy.after_run(ctx, workspace, RunOutcome(...))
```

**Instruction handling** (`before_run` owns final prompt):

The user's instruction (static string or async callback) is first resolved by `HarnessAgent`, then passed into `before_run`:
1. Resolves the base instruction value
2. `before_run` returns `PolicyPlan.system_prompt`
3. `HarnessAgent` applies the plan's prompt to inner `LlmAgent`

This keeps all policy-level prompt/tool/message setup in one hook instead of splitting logic across multiple methods.

Design decisions:
- Inner agent runs with `disable_tool_execution=True` so HarnessAgent owns the tool execution breakpoint. This enables permission gating and human approval without modifying `BaseTool` or `ToolsProcessor`.
- Three-hook policy surface keeps the policy API small while still expressing OpenClaw loop control.
- State delta flushing happens both before the inner run (from `before_run` side effects) and during streaming (from `on_event` side effects).
- `_build_inner_context` creates a copy of the invocation context with the inner agent and override messages. This ensures the inner agent sees itself as the active agent.

## Tool Pattern

`_tool/_filesystem.py`

All harness tools extend `_WorkspaceTool`, which holds a `BaseWorkspace` reference. Each tool:
- Defines a `FunctionDeclaration` (name, description, parameter schema)
- Delegates to `workspace.filesystem()` or `workspace.exec()` in `_run_async_impl`

```
_WorkspaceTool(BaseTool)
  ├── ReadFileTool    → workspace.filesystem().read_file(path, offset, limit)
  ├── WriteFileTool   → workspace.filesystem().write_file(path, content)
  ├── EditFileTool    → workspace.filesystem().edit_file(path, old_text, new_text, replace_all)
  ├── ListDirTool     → workspace.filesystem().list_dir(path, recursive, max_entries)
  ├── GlobTool        → workspace.filesystem().glob(pattern, path)
  ├── GrepTool        → workspace.filesystem().grep(pattern, path, glob, output_mode)
  └── ExecTool        → workspace.exec(command, working_dir, timeout)
```

Design decisions:
- Tools are thin wrappers — all logic lives in the workspace/filesystem layer. This means swapping workspace implementation (local → remote) automatically changes tool behavior.
- Tools accept `filters` for framework-level tool filtering (approval gates, etc.) — the extension point for future governance.

## Tool Permission

### Motivation

The harness needs to control tool execution — auto-allowing safe reads, blocking dangerous commands, and pausing for human approval on writes. The key constraint is that `runner.run_async` is a one-directional async generator: the user consumes events but cannot send decisions back mid-stream. Human approval therefore requires a **pause/resume** cycle: the agent yields an approval request, stops, and the user resumes with a decision in the next `runner.run_async` call.

This means permission cannot live inside `BaseTool` or the filter pipeline — those layers have no channel to communicate with the user. Instead, permission belongs at the **HarnessAgent outer loop** level, where it can control tool execution flow and interact with the user through the event stream.

### LlmAgent Change: `disable_tool_execution`

`LlmAgent` gains a boolean `disable_tool_execution: bool = False`. When `True`, the agent yields the LLM response (which already contains `function_call` parts) but **returns before executing any tools**. This gives the outer controller (HarnessAgent) a breakpoint to inspect tool calls and decide what to execute.

Current `_run_async_impl` flow:

```
while running:
  1. Build LLM request (includes tool results from previous round)
  2. Call LLM, stream response, yield events      ← function_call events yielded here
  3. Collect tool calls from response
  4. Execute tools, yield function_response events  ← tool execution happens here
  5. If disable_react_tool: return (skip re-loop)
  6. Continue loop (next LLM call sees tool results)
```

With `disable_tool_execution=True`, step 4 is skipped — the agent returns after step 3. The change is a single guard inserted at `CHECKPOINT 4`:

```python
if collected_tool_calls and self.disable_tool_execution:
    return
```

Comparison with existing flags:

| Flag | Executes tools? | Loops back to LLM? | Used by |
|------|----------------|---------------------|---------|
| default | Yes | Yes | Normal LlmAgent |
| `disable_react_tool=True` | Yes | **No** | TeamAgent (external orchestrator) |
| `disable_tool_execution=True` | **No** | No | HarnessAgent (permission gating) |

Backward compatibility: default is `False`, no behavior change for existing agents.

### ToolCallGate

`_core/_tool_call_gate.py`

A `ToolCallGate` controls whether and how a tool call executes. It has **two levels**, both producing the same `ToolCallGateAction`:

| | Level 1 — Static | Level 2 — User |
|---|---|---|
| **How** | Programmatic callbacks, runs automatically | Pause/resume, human decides interactively |
| **Before execution** | `before_call` callback → `ToolCallGateAction` | `user_confirm=True` → yield `ToolCallConfirmEvent`, user responds with `ToolCallGateAction` |
| **After execution** | `after_call` callback → modified result | (same pattern, future extension) |

When both levels are configured, level 1 runs first as a pre-filter. If level 1 denies, the call is blocked immediately without reaching level 2.

#### ToolCallGateAction

Action produced by either level of a gate. Both static callbacks and user responses return this type.

```python
@dataclass
class ToolCallGateAction:
    class Type(Enum):
        ALLOW = "allow"       # Proceed with (possibly modified) args
        DENY = "deny"         # Block execution, return deny_response to the LLM

    type: Type = Type.ALLOW
    modified_args: dict[str, Any] | None = None    # used with ALLOW to replace original args
    deny_response: str | None = None               # used with DENY as the message to the LLM

    @staticmethod
    def allow(modified_args: dict[str, Any] | None = None) -> ToolCallGateAction:
        return ToolCallGateAction(type=ToolCallGateAction.Type.ALLOW, modified_args=modified_args)

    @staticmethod
    def deny(reason: str = "") -> ToolCallGateAction:
        return ToolCallGateAction(type=ToolCallGateAction.Type.DENY, deny_response=reason)
```

#### Callback types

```python
BeforeCallGateFn = Callable[[str, dict[str, Any]], ToolCallGateAction]
AfterCallGateFn = Callable[[str, dict[str, Any], Any], Any]
```

- `BeforeCallGateFn(tool_name, args)` → `ToolCallGateAction` — runs before execution, can allow/deny/modify args.
- `AfterCallGateFn(tool_name, args, result)` → `result` — runs after execution, can inspect or transform the result before it is returned to the LLM.

#### ToolCallGate

```python
@dataclass
class ToolCallGate:
    """Permission gate for a single tool.

    Two levels, both produce ToolCallGateAction:
      Level 1 — Static: programmatic before/after callbacks
      Level 2 — User: interactive pause/resume for human decision
    """

    # Level 1: Static
    before_call: BeforeCallGateFn | None = None
    after_call: AfterCallGateFn | None = None

    # Level 2: User
    user_confirm: bool = False
```

**Evaluation flow** for a single tool call:

```
1. Level 1 — Static before_call
   │  if gate.before_call is set:
   │    action = gate.before_call(tool_name, args)
   │    if DENY  → blocked, return deny_response to LLM. Stop.
   │    if ALLOW → apply modified_args if provided, continue.
   │
2. Level 2 — User confirm
   │  if gate.user_confirm is True:
   │    → add to confirm_batch (will pause for user decision)
   │    → user responds with ToolCallGateAction (allow/deny/modify)
   │  else:
   │    → add to execute_batch (auto-execute)
   │
3. Tool execution
   │  Execute approved tools via ToolsProcessor.
   │
4. Level 1 — Static after_call
   │  if gate.after_call is set:
   │    result = gate.after_call(tool_name, args, result)
   │    → modified result returned to LLM
```

### ToolCallConfirm

When HarnessAgent pauses with a `ToolCallConfirmEvent`, the user resumes by passing `ToolCallConfirm` objects as the `new_message` in the next `runner.run_async` call. Each response wraps a `call_id` and a `ToolCallGateAction` — the same action type used by static callbacks, making both levels symmetric.

```python
@dataclass
class ToolCallConfirm:
    """User's response to a ToolCallConfirmEvent.

    Wraps a ToolCallGateAction — the same type that static gate callbacks return.
    """
    call_id: str
    action: ToolCallGateAction

    @staticmethod
    def approve(call_id: str) -> ToolCallConfirm:
        """Approve the tool call as-is."""
        return ToolCallConfirm(call_id, ToolCallGateAction.allow())

    @staticmethod
    def reject(call_id: str, reason: str = "") -> ToolCallConfirm:
        """Reject the tool call. The reason is returned to the LLM."""
        return ToolCallConfirm(call_id, ToolCallGateAction.deny(reason))

    @staticmethod
    def modify(call_id: str, modified_args: dict[str, Any]) -> ToolCallConfirm:
        """Approve the tool call with modified args."""
        return ToolCallConfirm(call_id, ToolCallGateAction.allow(modified_args))
```

### HarnessAgent Outer Loop — Tool Execution Controller

With `disable_tool_execution=True` on the inner agent, HarnessAgent's outer loop becomes the tool execution controller. After the inner agent yields function_call events and returns, HarnessAgent looks up the `ToolCallGate` for each pending call and runs two-level evaluation (level 1 static → level 2 user).

**Updated invocation flow** (`_run_async_impl`):

```
1. Resolve base instruction, build plan via policy.before_run()
2. Apply plan to inner agent (set disable_tool_execution=True)

3. Outer loop:
   ┌─ Stream inner agent ──────────────────────────────────────┐
   │  for event in inner_agent.run_async(ctx):                  │
   │    yield event (user sees LLM text + function_call parts)  │
   │    track pending_tool_calls from non-partial events        │
   │    policy.on_event(ctx, workspace, event, iteration)       │
   └────────────────────────────────────────────────────────────┘
   Inner agent returns WITHOUT executing tools.

   if no pending_tool_calls → break (LLM finished)

   ┌─ Level 1: Static gate ─────────────────────────────────────┐
   │  for each call in pending_tool_calls:                       │
   │    gate = gates.get(call.name)                               │
   │    if gate is None → add to execute_batch (no gating)       │
   │    elif gate.before_call:                                   │
   │      action = gate.before_call(name, args)                  │
   │      if DENY  → create deny response, append to session    │
   │      if ALLOW → apply modified_args, continue to level 2   │
   │    else → continue to level 2                               │
   └─────────────────────────────────────────────────────────────┘

   ┌─ Level 2: User gate ───────────────────────────────────────┐
   │  for each surviving call:                                   │
   │    if gate.user_confirm → add to confirm_batch              │
   │    else                 → add to execute_batch              │
   └─────────────────────────────────────────────────────────────┘

   if confirm_batch is not empty:
     yield ToolCallConfirmEvent(pending_calls=confirm_batch,
                                execute_batch=execute_batch)
     return  ← PAUSE (wait for user to resume via runner.run_async)

   ┌─ Execute tools ────────────────────────────────────────────┐
   │  for event in tools_processor.execute_tools_async():        │
   │    yield event (user sees function_response)                │
   │    append to session                                        │
   └─────────────────────────────────────────────────────────────┘

   ┌─ Level 1: Static post-processing ─────────────────────────┐
   │  for each executed tool with gate.after_call:               │
   │    result = gate.after_call(name, args, result)             │
   └─────────────────────────────────────────────────────────────┘

   continue → next round (inner agent sees tool results, calls LLM again)

4. Finalize: policy.after_run(ctx, workspace, RunOutcome(...))
```

### Human Approval Flow (user_confirm)

When a `ToolCallGate` has `user_confirm=True` and level 1 does not deny, HarnessAgent pauses and yields a `ToolCallConfirmEvent`. The user inspects the pending calls, constructs `ToolCallConfirm` objects (which wrap `ToolCallGateAction`), and resumes via `runner.run_async`. This follows the existing `LongRunningFunctionTool` pause/resume pattern.

**Example interaction:**

```
Turn 1: runner.run_async(message="fix the bug in utils.py")
  │
  ├─ LLM: "I'll read the file first"
  ├─ function_call(read_file, {path: "utils.py"})
  │    → gate: ToolCallGate() (no gating) → executes
  ├─ function_response(read_file, "file content...")
  ├─ LLM: "I see the issue, let me fix it"
  ├─ function_call(write_file, {path: "utils.py", ...})
  │    → gate: ToolCallGate(user_confirm=True)
  │    → level 1: no before_call → pass
  │    → level 2: user_confirm=True → confirm_batch
  ├─ yield ToolCallConfirmEvent(pending_calls=[write_file(...)])
  └─ PAUSE (run_async returns)

User sees: "Agent wants to write_file(utils.py, ...) — approve / reject / modify?"

Turn 2: runner.run_async(message=ToolCallConfirm.approve(call_id))
  │
  ├─ HarnessAgent resumes, executes write_file
  ├─ function_response(write_file, "ok")
  ├─ LLM: "Done, I fixed the null check on line 42"
  └─ END
```

**From the user's code perspective** (compare with `examples/llmagent/run_agent.py`):

```python
async for event in runner.run_async(user_id=uid, session_id=sid, new_message=content):
    if isinstance(event, ToolCallConfirmEvent):
        for call in event.pending_calls:
            answer = await prompt_user(
                f"Allow {call.name}({call.args})? [approve/reject/modify]"
            )
            if answer == "approve":
                response = ToolCallConfirm.approve(call.id)
            elif answer == "reject":
                response = ToolCallConfirm.reject(call.id, reason="user declined")
            elif answer == "modify":
                new_args = await get_modified_args(call.args)
                response = ToolCallConfirm.modify(call.id, modified_args=new_args)
        # Resume with the response
        break

    # Normal event handling (same as before)
    ...
```

### Relationship to Existing Mechanisms

| Mechanism | Scope | Where it lives | When to use |
|-----------|-------|----------------|-------------|
| `ToolCallGate` level 1 (static) | Per-tool, arg-aware | HarnessAgent (harness-only) | Programmatic gating: path scoping, command allowlists, arg sanitization, result redaction |
| `ToolCallGate` level 2 (user) | Per-tool | HarnessAgent (harness-only) | Human-in-the-loop: approve/reject/modify before execution |
| `BaseTool.filters` | Per-tool | BaseTool (framework) | Low-level pipeline extension: tracing, metrics, caching |
| `LlmAgent.before_tool_callback` | All tools on agent | LlmAgent (framework) | Cross-cutting: logging, global rate limiting |
| `HarnessPolicy.on_event` | All events | HarnessAgent (harness-only) | Post-execution observation: loop control, error feedback |

Design decisions:
- **No changes to `BaseTool`** — permission is not a per-tool-instance concern. It is an execution-control concern owned by the harness outer loop.
- **No changes to `ToolsProcessor`** — HarnessAgent reuses `ToolsProcessor.execute_tools_async` for allowed tool calls. The processor does not need to know about permissions.
- **`LlmAgent` change is minimal** — a single boolean flag `disable_tool_execution` with a one-line guard. It is orthogonal to `disable_react_tool` and fully backward compatible.
- **Two-level gate, one action type** — `ToolCallGate` has two levels (static callbacks + user confirm), both producing `ToolCallGateAction`. The unified action type makes the design symmetric: static `before_call` returns `ToolCallGateAction`, user responds with `ToolCallGateAction` (via `ToolCallConfirm`).
- **`ToolCallGate` is a dataclass, not a base class** — the two levels are composed declaratively per gate. No subclassing needed.
- **Simple gate resolution** — `dict[str, ToolCallGate]` keyed by `tool.name`. Lookup is `gates.get(tool_name)`. Tools not in the dict have no gating (auto-execute).
- **`ToolCallGateAction` provides factory helpers** — `allow()` and `deny()` are used by both static callbacks and `ToolCallConfirm` helpers, keeping the API consistent across levels.
- **Two-level evaluation** — level 1 (static) runs first and can short-circuit with DENY or modify args. Level 2 (user) only runs for calls that survive level 1. This ensures programmatic rules are always enforced before human approval.

### File Placement

`ToolCallGate`, `ToolCallGateAction`, `ToolCallConfirm`, `ToolCallConfirmEvent`, and callback type aliases live in `trpc_agent_sdk/harness/` since they are harness-specific:

```
trpc_agent_sdk/harness/
├── __init__.py                    # Re-exports ToolCallGate, ToolCallGateAction,
│                                  #   ToolCallConfirm, ToolCallConfirmEvent
├── _agent.py                      # HarnessAgent runs two-level evaluation in outer loop
├── _core/
│   ├── __init__.py
│   └── _tool_call_gate.py         # ToolCallGate, ToolCallGateAction,
│                                  #   ToolCallConfirm, BeforeCallGateFn, AfterCallGateFn
├── _policy/
│   ├── __init__.py
│   ├── _base.py                   # HarnessPolicy, PolicyPlan
│   └── _openclaw/
│       ├── __init__.py
│       └── _policy.py             # OpenClawPolicy
└── ...
```

`LlmAgent` gains `disable_tool_execution` in `trpc_agent_sdk/agents/_llm_agent.py` (framework-level, not harness-specific).

## Bootstrap Policy

### OpenClawPolicy

`_policy/_openclaw/_policy.py`

Minimal policy that wires the seven workspace tools, appends execution guidance to the system prompt, and configures tool permissions through `before_run`.

- `before_run`: returns `PolicyPlan` with all seven workspace tools and prompt guidance
- `on_event`: no-op loop decision (`None`, equivalent to CONTINUE)
- `after_run`: no-op

This is intentionally lightweight — it demonstrates the policy pattern and provides a functional baseline. OpenClaw-specific behaviors (managed loop, iteration budget, error hints, memory strategy) are documented in step 2.

## Public Exports

```python
# trpc_agent_sdk/harness/__init__.py
__all__ = [
    "HarnessAgent",
    "HarnessPolicy",       "OpenClawPolicy",
    "ToolCallGate",        "ToolCallGateAction",
    "ToolCallConfirmEvent", "ToolCallConfirm",
    "BaseFilesystem",      "BaseWorkspace",       "GrepOutputMode",
    "LocalFilesystem",     "LocalWorkspace",
    "ReadFileTool",        "WriteFileTool",       "EditFileTool",
    "ListDirTool",         "GlobTool",            "GrepTool",         "ExecTool",
]
```

## Current Constraints (Expected in Step 0)

- No remote/sandbox workspace implementation yet.
- No summarization/compaction mechanism yet.
- No memory/history virtual path integration yet.
- No delegation/sub-agent policy behavior yet.
- No advanced governance/observability layer owned by harness policy yet.
