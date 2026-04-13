# Step 0: Basic Harness Foundation

## Goal

Build the minimum harness foundation so later policy behaviors (OpenClaw, Deep, custom) can be added incrementally.

## Project Structure

```
trpc_agent_sdk/harness/
├── __init__.py                    # Public exports
├── _agent.py                      # HarnessAgent (agent loop)
├── _policy/
│   ├── __init__.py
│   ├── _base.py                   # HarnessPolicy (abstract hooks)
│   └── _openclaw.py               # OpenClawPolicy (bootstrap policy)
├── _workspace/
│   ├── __init__.py
│   ├── _base_filesystem.py        # BaseFilesystem (abstract)
│   ├── _base_workspace.py         # BaseWorkspace (abstract)
│   ├── _local_filesystem.py       # LocalFilesystem
│   └── _local_workspace.py        # LocalWorkspace
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

`HarnessAgent` extends `BaseAgent` and wraps an internal `LlmAgent`. In step 0, it delegates to the inner loop. In later stages, it can run a policy-managed outer loop for OpenClaw-like bounded iterations.

```python
class HarnessAgent(BaseAgent):
    workspace: BaseWorkspace       # Scoped file/shell execution
    policy: HarnessPolicy          # Hook-based policy
    model: str | LLMModel          # Forwarded to inner agent
    instruction: str | Callable    # Base instruction (static or dynamic)
    tools: list[ToolUnion]         # Additional user tools
    _inner_agent: LlmAgent         # Internal execution agent
```

**Invocation flow** (`_run_async_impl`):

```
1. Resolve base instruction value (static or callback result)
2. Build plan:    policy.before_run(ctx, workspace, origin_prompt) → PolicyPlan
3. Apply plan:    _inner_agent tools/prompt/messages/loop settings from plan
4. Outer loop:    run inner agent in single-round mode (disable_react_tool=True)
5. Stream events: for each event from inner agent:
                    → if event.partial: passthrough only
                    → else:
                       - decision = policy.on_event(ctx, workspace, event, iteration)
                       - merge state_delta and yield event
6. Stop when:     decision=FINISH/ABORT, no new tool calls, transfer/long-running, or max_iterations reached
7. Finalize:      policy.after_run(ctx, workspace, RunOutcome(...))
```

**Instruction handling** (`before_run` owns final prompt):

The user's instruction (static string or async callback) is first resolved by `HarnessAgent`, then passed into `before_run`:
1. Resolves the base instruction value
2. `before_run` returns `PolicyPlan.system_prompt`
3. `HarnessAgent` applies the plan's prompt to inner `LlmAgent`

This keeps all policy-level prompt/tool/message setup in one hook instead of splitting logic across multiple methods.

Design decisions:
- Default path can still use pass-through behavior, but managed-loop mode is the extension point used by OpenClaw-like policy.
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

## Bootstrap Policy

### OpenClawPolicy

`_policy/_openclaw.py`

Minimal policy that wires the seven workspace tools and appends execution guidance to the system prompt through `before_run`.

- `before_run`: returns `PolicyPlan` with all seven workspace tools and prompt guidance
- `on_event`: no-op loop decision (`None`, equivalent to CONTINUE)
- `after_run`: no-op

This is intentionally lightweight — it demonstrates the policy pattern and provides a functional baseline. OpenClaw-specific behaviors (managed loop, iteration budget, error hints, memory strategy) are documented in step 2.

## Public Exports

```python
# trpc_agent_sdk/harness/__init__.py
__all__ = [
    "HarnessAgent",
    "HarnessPolicy",   "OpenClawPolicy",
    "BaseFilesystem",  "BaseWorkspace",  "GrepOutputMode",
    "LocalFilesystem", "LocalWorkspace",
    "ReadFileTool",    "WriteFileTool",  "EditFileTool",
    "ListDirTool",     "GlobTool",       "GrepTool",       "ExecTool",
]
```

## Current Constraints (Expected in Step 0)

- No permission/approval policy yet for high-risk operations.
- No remote/sandbox workspace implementation yet.
- No summarization/compaction mechanism yet.
- No memory/history virtual path integration yet.
- No delegation/sub-agent policy behavior yet.
- No advanced governance/observability layer owned by harness policy yet.
