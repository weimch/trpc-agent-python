# Step 1: Workspace Implementation

## Goal

Document the concrete workspace and filesystem implementations that back the abstract interfaces from step 0.

## LocalFilesystem

`_workspace/_local/_filesystem.py`

Filesystem implementation rooted at a local host directory. All operations resolve paths against this root.

### Constants

| Constant | Value | Purpose |
|---|---|---|
| `_DEFAULT_READ_LIMIT` | 2000 | Default max lines returned by `read_file` when no limit specified |
| `_MAX_READ_CHARS` | 128,000 | Hard character cap on `read_file` output |
| `_MAX_LIST_ENTRIES` | 200 | Hard cap on `list_dir` entries |
| `_IGNORE_DIRS` | `.git`, `node_modules`, `__pycache__`, `.venv`, `venv`, `dist`, `build`, `.tox`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.coverage`, `htmlcov` | Directories excluded from `list_dir` and `rglob` traversal |

### Path Resolution

`resolve_path(path, base_dir=None) -> Path`

- Expands `~` via `Path.expanduser()`
- Relative paths resolve against `base_dir` (if given) or `root`
- Returns normalized absolute path via `Path.resolve()`
- No permission boundary enforcement (planned for later steps)

### read_file

- Returns numbered lines in `{line_no}| {content}` format
- Supports 1-indexed `offset` and optional `limit` for pagination
- Adds pagination hints: `(Showing lines X-Y of Z. Use offset=N to continue.)` or `(End of file — N lines total)`
- Truncates output at `_MAX_READ_CHARS` by dropping trailing lines

### write_file

- Creates parent directories automatically (`mkdir(parents=True, exist_ok=True)`)
- Writes UTF-8 encoded content
- Returns byte count confirmation

### edit_file

Two-phase find-and-replace:

1. **Exact match**: Direct string search in file content
2. **Fuzzy match**: If exact match fails, strips leading/trailing whitespace from each line and slides a window over file content to find whitespace-insensitive matches

Match validation:
- If no match found: returns error with best-match diff (if similarity > 50%) using `difflib.unified_diff`
- If multiple matches and `replace_all=False`: returns warning asking for more context
- Handles CRLF files: normalizes to LF for matching, restores CRLF on write

### list_dir

- Non-recursive: `D name` / `F name` format for immediate children
- Recursive: relative path format, with `/` suffix for directories
- Skips `_IGNORE_DIRS` entries
- Truncates at `_MAX_LIST_ENTRIES` with count annotation

### glob

- Relative patterns: delegated to `collect_files_with_glob` (framework utility)
- Absolute patterns: uses Python `glob.glob(recursive=True)`
- Returns deduplicated, sorted relative paths

### grep

Primary: shells out to `rg` (ripgrep) with appropriate flags per output mode:
- `files_with_matches` → `rg --files-with-matches`
- `content` → `rg -n` (line numbers)
- `count` → `rg -c`

Supports optional `--glob` file filter.

Fallback: if `rg` is not installed (`FileNotFoundError`), uses a pure-Python implementation with `re.compile` + file iteration. Same output format as the `rg` version.

## LocalWorkspace

`_workspace/_local/_workspace.py`

Local workspace that composes `LocalFilesystem` + `LocalProgramRunner` from the framework's code executor runtime.

### Constants

| Constant | Value | Purpose |
|---|---|---|
| `_MAX_EXEC_OUTPUT` | 10,000 | Character cap on `exec` output |

### Initialization

```python
def __init__(self, root: str | Path):
    self._filesystem = LocalFilesystem(root=root)
    self._runner = LocalProgramRunner()
    self._workspace_info = WorkspaceInfo(id="harness_local_workspace", path=self.root.as_posix())
```

- Creates `LocalFilesystem` with the given root
- Creates `LocalProgramRunner` from the framework's `code_executors.local` module
- Builds a `WorkspaceInfo` descriptor for the runner

### exec

Shell command execution via `LocalProgramRunner.run_program`:

- Resolves `working_dir` through `LocalFilesystem.resolve_path`, defaults to workspace root
- Prefers `/bin/bash -lc` (login shell for env setup), falls back to `/bin/sh -lc`
- Default timeout: 60 seconds
- Output format: stdout + stderr (prefixed `STDERR:`) + exit code
- Truncation: head/tail with `... (N chars truncated) ...` middle marker at `_MAX_EXEC_OUTPUT`
- Timeout: returns explicit error message rather than raising

## Tool Declarations

`_tool/_filesystem.py`

Each tool extends `_WorkspaceTool(BaseTool)` and defines a `FunctionDeclaration` with JSON Schema parameters. Tools are thin delegates — they extract args and call through to workspace.

### Tool Summary

| Tool | Parameters | Required | Delegates to |
|---|---|---|---|
| `read_file` | `path: str`, `offset: int`, `limit: int` | `path` | `filesystem().read_file` |
| `write_file` | `path: str`, `content: str` | `path`, `content` | `filesystem().write_file` |
| `edit_file` | `path: str`, `old_text: str`, `new_text: str`, `replace_all: bool` | `path`, `old_text`, `new_text` | `filesystem().edit_file` |
| `list_dir` | `path: str`, `recursive: bool`, `max_entries: int` | `path` | `filesystem().list_dir` |
| `glob` | `pattern: str`, `path: str` | `pattern` | `filesystem().glob` |
| `grep` | `pattern: str`, `path: str`, `glob: str`, `output_mode: enum` | `pattern` | `filesystem().grep` |
| `exec` | `command: str`, `working_dir: str`, `timeout: int(1-600)` | `command` | `workspace.exec` |

### _WorkspaceTool Base

```python
class _WorkspaceTool(BaseTool):
    def __init__(self, *, name, description, workspace, filters_name=None, filters=None):
        super().__init__(name=name, description=description, ...)
        self._workspace = workspace
```

All tools receive `workspace: BaseWorkspace` at construction. `OpenClawPolicy.build_tools` instantiates all seven tools per invocation, passing the workspace reference.

Tools also accept optional `filters` / `filters_name` for framework-level tool filtering — the extension point for future approval gates.
