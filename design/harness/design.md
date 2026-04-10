# tRPC-Agent Harness Design

## Overview

An agent harness is the runtime and control layer around an AI model that turns it into a reliable, goal-oriented agent.

In one line:
Agent = Model (reasoning brain) + Harness (state, tools, policy, and execution)

The harness should provide the following capabilities:
- Planning and workflow control: The agent can decompose goals, create and track task state, and resume long-running work after interruptions.
- Task delegation and role isolation: The main agent can delegate scoped work to subagents with clear contracts and bounded context.
- Context and token management: For each LLM call, include only the minimum relevant context ("no more, no less").
  - Summarization mechanism: When history (tool parameters, tool results, and messages) becomes too long, automatically summarize and offload details to files/storage, then keep retrievable pointers in context.
  - Memory mechanism: For cross-session memory (such as username, preferences, and long-term decisions), support save/load memory via files or integrated memory backends (for example, Mem0).
  - Error-grounded feedback: Convert runtime/tool errors into precise, actionable signals so the agent can quickly self-correct instead of repeating failure loops.
- Controlled execution environment: Run the agent in a policy-controlled local environment or sandbox with least-privilege permissions.
  - Filesystem: Allow scoped read/write to local or sandbox storage with explicit permission boundaries. In practice, file-backed artifacts are also useful for token-efficient state management.
  - Shell: Allow command execution with permission controls (allowlists, approval gates, and timeouts) to balance autonomy with safety.
- Observability and governance: Capture traces, tool calls, decisions, and checkpoints, and support human approval for high-risk actions.

## Related Framework

### Overview

| Capability | Deepagents | Nanobot |
|---|---|---|
| **Planning and workflow control** | LangChain agent loop (recursion limit 1000). Explicit `write_todos` tool for structured task tracking. LangGraph checkpointing for resumption. | Tool-use iteration loop (max 40 iterations). No explicit planner — LLM plans via conversation history. JSONL session persistence for resumption. Cron tool for scheduled/recurring tasks. |
| **Task delegation and role isolation** | `task` tool with sync subagents (blocking, own middleware stack) and async subagents (remote, non-blocking with check/update/cancel). Subagents **can** spawn further subagents. | `spawn` tool with async subagents (background asyncio tasks). Subagents **cannot** spawn further subagents or message users. Reduced iteration budget (15 vs 40). |
| **Summarization** | SummarizationMiddleware triggers at 85% of max input tokens. Tool argument truncation as lightweight pre-pass. Offloads to `/conversation_history/{thread_id}.md`. Manual `compact_conversation` tool available. | MemoryConsolidator triggers when prompt exceeds context window, targets 50% usage. LLM-powered consolidation via `save_memory` tool. Offloads to `HISTORY.md`. Up to 5 iterative rounds. |
| **Memory** | `AGENTS.md` files injected into system prompt via MemoryMiddleware. Agent updates memory by editing the file directly. | Two-layer: `MEMORY.md` (always in context, key facts) + `HISTORY.md` (append-only, grep-searchable event log, not in context). Updated during consolidation or directly by agent. |
| **Error-grounded feedback** | Prompt-guided ("stop and analyze why"). Specific error strings returned in ToolMessage. | ToolRegistry appends actionable hint to all errors: `[Analyze the error above and try a different approach.]` |
| **Filesystem** | 7 tools (`ls`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`, `execute`). Pluggable BackendProtocol (StateBackend, FilesystemBackend, StoreBackend, LangSmithSandbox, etc.). | 4 tools (`read_file`, `write_file`, `edit_file`, `list_dir`). `restrict_to_workspace` path boundary check. |
| **Shell** | `execute` tool via SandboxBackendProtocol. Backend-level isolation (container/VM/remote). 1-hour default timeout. | `exec` tool with deny-pattern regex blocklist, optional allowlist, SSRF protection, workspace containment. 60s default timeout. |
| **Observability and governance** | LangGraph checkpointing + LangSmith tracing. HumanInTheLoopMiddleware for interrupt-before approval gates. Prompt caching. Dangling tool call repair. | Loguru structured logging. JSONL session traces. Real-time progress streaming (thought + tool hints). `/status` introspection. Heartbeat service. Slash command governance. |

### Deepagents

**Planning and workflow control** — Uses LangChain's `create_agent` which builds a LangGraph state graph. Recursion limit of 1000 steps. `TodoListMiddleware` provides a `write_todos` tool for the agent to create/track structured task lists. LangGraph checkpointing enables mid-task resumption from any checkpoint. Skills serve as planning guides injected on demand.

**Task delegation and role isolation** — `SubAgentMiddleware` exposes a `task(description, subagent_type)` tool. Sync subagents run inline with state isolation (parent messages/todos excluded) and their own middleware stack. `AsyncSubAgentMiddleware` handles remote subagents on LangGraph deployments, returning a task_id immediately with `check/update/cancel` tools. A default `general-purpose` subagent is auto-added if none specified. Sync subagents can nest further subagents.

**Context and token management** — `SummarizationMiddleware` triggers at 85% of max input tokens. Pre-pass truncates old `write_file`/`edit_file` tool arguments. Evicted messages are offloaded to `/conversation_history/{thread_id}.md` with a summary + pointer kept in context. `FilesystemMiddleware` evicts tool results exceeding 20K tokens to `/large_tool_results/`, replacing with head+tail preview. `SkillsMiddleware` uses two-tier progressive loading (catalog in prompt, full content on demand). `MemoryMiddleware` injects `AGENTS.md` content into the system prompt for cross-session persistence.

**Controlled execution environment** — All file/shell operations go through `BackendProtocol`, an abstract interface with pluggable backends (StateBackend, FilesystemBackend, StoreBackend, LocalShellBackend, LangSmithSandbox, CompositeBackend). The `execute` tool is dynamically filtered based on whether the backend supports `SandboxBackendProtocol`. Path validation enforces absolute paths; deeper security is backend-specific.

**Observability and governance** — LangGraph checkpoints state after every transition, enabling replay and thread isolation. LangSmith integration provides tracing of every LLM call and tool execution. `HumanInTheLoopMiddleware` enables interrupt-before approval on configurable tools. `PatchToolCallsMiddleware` repairs dangling tool calls from interrupted executions. `AnthropicPromptCachingMiddleware` optimizes repeated LLM calls.

### Nanobot

**Planning and workflow control** — Core engine is `_run_agent_loop()` with a max 40 iteration budget. No explicit planner — the LLM plans by seeing its own prior tool calls and results. Sessions persist as JSONL files, enabling resumption by replaying full conversation history. Skills (`SKILL.md`) inject domain-specific planning guidance. Cron tool supports deferred and recurring tasks (`at`, `every_seconds`, `cron_expr`) running in isolated sessions.

**Task delegation and role isolation** — `spawn(task, label)` tool creates background asyncio tasks via `SubagentManager`. Subagents get a reduced tool set (file tools + exec + web, no `message`/`spawn`/`cron`/MCP), a minimal system prompt, and a 15-iteration budget. Results are announced back through the message bus as system-channel inbound messages. Subagents cannot nest or message users directly.

**Context and token management** — `MemoryConsolidator` triggers when estimated prompt size exceeds the context window, targeting below 50%. A dedicated LLM call produces a `save_memory(history_entry, memory_update)` output — `history_entry` appends to `HISTORY.md`, `memory_update` refreshes `MEMORY.md`. Up to 5 consolidation rounds run iteratively. Two-layer memory: `MEMORY.md` (always in context) for key facts, `HISTORY.md` (grep-searchable, not in context) for event log. Skills use two-tier progressive loading (XML catalog + on-demand `read_file`). Tool results are truncated at 16K chars; images replaced with placeholders.

**Controlled execution environment** — Four file tools with `restrict_to_workspace` enforcement. `exec` tool uses layered security: deny-pattern regex blocklist, optional allowlist, SSRF protection (DNS-based URL validation against private IP ranges), workspace path containment, and 60s default timeout. Web tools (`web_search`, `web_fetch`) include pre-fetch and post-redirect SSRF checks, HTML sanitization, untrusted content banners, and size limits. MCP integration extends tools dynamically with per-server allowlists and timeout isolation.

**Observability and governance** — Loguru structured logging for tool calls, subagent lifecycle, consolidation, and MCP events. Full session traces in JSONL. Real-time progress streaming (thought text + tool hints) configurable per channel. `/status` command reports model, tokens, context usage, session size, and uptime. Heartbeat service enables periodic autonomous monitoring. Slash commands (`/stop`, `/new`, `/restart`) provide deterministic session control.

## tRPC-Agent Design Overview

### Architecture

```
                          ┌─────────────────────────────────────────┐
                          │               Runner                     │
                          │  app_name, session_service,              │
                          │  memory_service, artifact_service        │
                          └──────────────────┬──────────────────────┘
                                             │ run_async()
                                             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                      HarnessAgent (extends BaseAgent)                     │
│                                                                          │
│  ┌─ Capabilities (building blocks composed by policies) ───────────┐    │
│  │  ┌────────────┐ ┌────────┐ ┌────────┐ ┌──────┐ ┌────────────┐  │    │
│  │  │ Filesystem │ │ Memory │ │ Skills │ │ Todo │ │ Delegation │  │    │
│  │  │            │ │        │ │        │ │      │ │            │  │    │
│  │  │ evict big  │ │ r/w    │ │ catalog│ │ plan │ │ delegate   │  │    │
│  │  │ results    │ │ facts  │ │ + load │ │ mgmt │ │ to members │  │    │
│  │  └────────────┘ └────────┘ └────────┘ └──────┘ └────────────┘  │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                                                          │
│  ┌─ SummarizationStrategy ───────────────────────────────────────────┐  │
│  │  ┌──────────────────┐ ┌─────────────────────┐ ┌────────────────┐ │  │
│  │  │ Threshold        │ │ Consolidation       │ │ None           │ │  │
│  │  │ (Deepagents)     │ │ (Nanobot)           │ │                │ │  │
│  │  │                  │ │                     │ │ no summary,    │ │  │
│  │  │ trigger: 85%     │ │ trigger: overflow   │ │ short tasks    │ │  │
│  │  │ → LLM summary    │ │ → consolidate into  │ │ or external    │ │  │
│  │  │ → /history/*.md  │ │   MEMORY + HISTORY  │ │ management     │ │  │
│  │  └──────────────────┘ └─────────────────────┘ └────────────────┘ │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│         │                          │                                     │
│         │   compose                │                                     │
│         ▼                          ▼                                     │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │ HarnessPolicy (hook-based extension model)                         │  │
│  │                                                                    │  │
│  │  Policies compose capabilities and summarization into four hooks:  │  │
│  │    - system prompt shaping (augment instruction with policy state) │  │
│  │    - tool injection (select capability-specific tools per turn)    │  │
│  │    - message composition (shape/override conversation history)     │  │
│  │    - event observation (react to streamed events, update state)    │  │
│  │                                                                    │  │
│  │  Policy profiles (e.g. OpenClaw, Deep, custom) differ in which    │  │
│  │  capabilities they activate and which summarization strategy       │  │
│  │  they apply.                                                       │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                         │                                │
│                                         ▼                                │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │ _inner_agent: LlmAgent                                             │  │
│  │                                                                    │  │
│  │  Receives policy-shaped instruction, tools, and messages.          │  │
│  │  Handles its own tool-reaction loop internally.                    │  │
│  └───────────────────────────┬────────────────────────────────────────┘  │
│                              │ file/shell tools route through            │
│                              ▼                                           │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │ Workspace                                                          │  │
│  │                                                                    │  │
│  │  Provides filesystem backend and shell execution scoped to a root │  │
│  │  directory. Pluggable implementations (local, remote/sandbox).     │  │
│  │                                                                    │  │
│  │  Future: virtual path mounts for service-backed state:             │  │
│  │    /memory/*  → MemoryServiceBackend                               │  │
│  │    /history/* → SessionServiceBackend                              │  │
│  │    /**        → FilesystemBackend(root)                            │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
                                   │
               Workspace routes by path prefix (future)
               ┌───────────────────┼──────────────────┐
               ▼                   ▼                   ▼
  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────┐
  │ /memory/*        │  │ /history/*       │  │ /** (default)       │
  │                 │  │                 │  │                     │
  │ MemoryService   │  │ SessionService  │  │ Real Filesystem     │
  │ (mem/sql/redis) │  │ (mem/sql/redis) │  │ (workspace root)    │
  │                 │  │                 │  │                     │
  │ MEMORY.md ←→    │  │ summaries ←→    │  │ src/, docs/, ...    │
  │  read/write     │  │  read/write     │  │ large_tool_results/ │
  │ HISTORY.md ←→   │  │ session.state   │  │ (project files)     │
  │  append/search  │  │                 │  │                     │
  └─────────────────┘  └─────────────────┘  └─────────────────────┘
```

**How it connects**: `Runner` creates `InvocationContext` with `session_service`, `memory_service`, and `session`. The `Workspace` provides filesystem and shell execution scoped to a root directory. Policies use hook methods to shape the system prompt, select tools, manage conversation history, and react to events — composing capabilities and summarization strategies appropriate to the policy profile. Future workspace evolution will add virtual path routing so the LLM sees a unified filesystem while harness state (memory, history) is backed by the framework's persistence services.
