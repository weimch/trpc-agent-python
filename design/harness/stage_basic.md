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

Base policy with four hook methods. Subclasses override to control tool injection, prompt shaping, message composition, and event-time state updates.

```python
class HarnessPolicy:
    build_tools(ctx, workspace) -> list[BaseTool]
    build_system_prompt(ctx, workspace, origin_prompt) -> str
    build_messages(ctx, workspace) -> Optional[list[Content]]
    on_event(ctx, workspace, event) -> None
```

Design decisions:
- Hook-based rather than monolithic: each concern (tools, prompt, messages, events) is a separate override point. Policies can override one hook without touching others.
- `build_system_prompt` receives `origin_prompt` (user's base instruction) and returns the final prompt. This lets policies augment rather than replace the instruction.
- `build_messages` returns `None` to use the default session messages, or a list to override. This is the extension point for future summarization/compaction.
- `on_event` is fire-and-forget observation. Policies can track state (e.g., token counts, tool call history) without blocking the event stream.
- Base class provides pass-through defaults so minimal policies only need to override `build_tools`.

## Agent Loop

### HarnessAgent

`_agent.py`

`HarnessAgent` extends `BaseAgent` and wraps an internal `LlmAgent`. It does not run its own iteration loop — it delegates to the inner agent's tool-reaction loop and intercepts the event stream.

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
1. Build tools:    policy.build_tools(ctx, workspace) + user tools → _inner_agent.tools
2. Build messages: policy.build_messages(ctx, workspace) → override_messages
3. Flush state:    if state_delta accumulated during build, yield state update event
4. Run inner:      _inner_agent.run_async(inner_context)
5. Stream events:  for each event from inner agent:
                     → policy.on_event(ctx, workspace, event)
                     → merge any state_delta into event
                     → yield event
                     → stop on LongRunningEvent or transfer
```

**Instruction merging** (`_merge_instruction`):

The user's instruction (static string or async callback) is wrapped into an async callback that:
1. Resolves the base instruction value
2. Passes it through `policy.build_system_prompt(ctx, workspace, value)`
3. Returns the policy-augmented prompt to the inner `LlmAgent`

This callback is set as the inner agent's `instruction` at init time and evaluated per-invocation by the framework.

Design decisions:
- No outer while-loop — the inner `LlmAgent` handles its own tool-reaction cycle. The harness layer is a single-pass wrapper that shapes inputs and observes outputs.
- State delta flushing happens both before the inner run (from build-phase side effects) and during streaming (from on_event side effects).
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

Minimal policy that wires the seven workspace tools and appends execution guidance to the system prompt.

- `build_tools`: returns all seven workspace tools
- `build_system_prompt`: appends workspace root path and tool usage guidance to the base instruction
- `build_messages`: pass-through (returns `ctx.override_messages`)
- `on_event`: no-op

This is intentionally lightweight — it demonstrates the policy pattern and provides a functional baseline. OpenClaw-specific behaviors (memory consolidation, iteration budget, security controls) are planned for step 2.

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
