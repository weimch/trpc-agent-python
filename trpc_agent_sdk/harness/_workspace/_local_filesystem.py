# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Local filesystem implementation for harness workspace backends."""

from __future__ import annotations

import glob as pyglob
import re
import subprocess
from pathlib import Path
from typing import Optional

from trpc_agent_sdk.code_executors.utils import collect_files_with_glob

from ._base_filesystem import BaseFilesystem
from ._base_filesystem import GrepOutputMode


def _find_match(content: str, old_text: str) -> tuple[Optional[str], int]:
    """Find a replace target in file content.

    Args:
        content: Full file content.
        old_text: Text to locate in ``content``.

    Returns:
        tuple[Optional[str], int]: The matched source text and occurrence count.
    """
    if old_text in content:
        return old_text, content.count(old_text)

    old_lines = old_text.splitlines()
    if not old_lines:
        return None, 0

    stripped_old = [line.strip() for line in old_lines]
    content_lines = content.splitlines()
    candidates: list[str] = []

    for idx in range(len(content_lines) - len(stripped_old) + 1):
        window = content_lines[idx:idx + len(stripped_old)]
        if [line.strip() for line in window] == stripped_old:
            candidates.append("\n".join(window))

    if candidates:
        return candidates[0], len(candidates)
    return None, 0


def _not_found_msg(old_text: str, content: str, path: str) -> str:
    """Build a user-facing message when ``old_text`` is not found.

    Args:
        old_text: Text expected in the file.
        content: Actual file content.
        path: User-provided path used in error output.

    Returns:
        str: Error message, optionally including a unified diff for the closest match.
    """
    import difflib

    lines = content.splitlines(keepends=True)
    old_lines = old_text.splitlines(keepends=True)
    window = len(old_lines)
    best_ratio = 0.0
    best_start = 0

    for idx in range(max(1, len(lines) - window + 1)):
        ratio = difflib.SequenceMatcher(None, old_lines, lines[idx:idx + window]).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = idx

    if best_ratio > 0.5:
        diff = "\n".join(
            difflib.unified_diff(
                old_lines,
                lines[best_start:best_start + window],
                fromfile="old_text (provided)",
                tofile=f"{path} (actual, line {best_start + 1})",
                lineterm="",
            ))
        return (f"old_text not found in {path}.\n"
                f"Best match ({best_ratio:.0%} similar) at line {best_start + 1}:\n{diff}")
    return f"old_text not found in {path}. No similar text found. Verify the file content."


class LocalFilesystem(BaseFilesystem):
    """Filesystem implementation rooted at a local host directory."""

    _DEFAULT_READ_LIMIT = 2000
    _MAX_READ_CHARS = 128_000
    _MAX_LIST_ENTRIES = 200

    _IGNORE_DIRS = {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".coverage",
        "htmlcov",
    }

    def __init__(self, root: str | Path):
        """Initialize filesystem with a normalized root directory.

        Args:
            root: Workspace root path for all filesystem operations.
        """
        root_path = Path(root).expanduser().resolve()
        root_path.mkdir(parents=True, exist_ok=True)
        self._root = root_path

    @property
    def root(self) -> Path:
        """Return configured filesystem root.

        Returns:
            Path: Absolute root path.
        """
        return self._root

    def resolve_path(self, path: str, base_dir: Optional[Path] = None) -> Path:
        """Resolve a user path to an absolute path.

        Args:
            path: Relative or absolute path.
            base_dir: Optional base directory for relative paths.

        Returns:
            Path: Normalized absolute path.
        """
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = (base_dir or self._root) / p
        return p.resolve()

    async def read_file(self, path: str, offset: int = 1, limit: Optional[int] = None) -> str:
        """Read a file and return numbered lines with pagination hints.

        Args:
            path: File path to read.
            offset: 1-indexed start line.
            limit: Optional maximum number of lines to return.

        Returns:
            str: Numbered file content.
        """
        fp = self.resolve_path(path)
        if not fp.exists():
            raise FileNotFoundError(f"File not found: {path}")
        if not fp.is_file():
            raise IsADirectoryError(f"Not a file: {path}")

        try:
            lines = fp.read_text(encoding="utf-8").splitlines()
        except Exception as ex:  # pylint: disable=broad-except
            raise RuntimeError(f"Failed to read file '{path}': {ex}") from ex

        total = len(lines)
        if total == 0:
            return f"(Empty file: {path})"

        if offset < 1:
            offset = 1
        if offset > total:
            raise ValueError(f"offset {offset} is beyond end of file ({total} lines)")

        start = offset - 1
        end = min(start + (limit or self._DEFAULT_READ_LIMIT), total)
        numbered = [f"{start + idx + 1}| {line}" for idx, line in enumerate(lines[start:end])]
        result = "\n".join(numbered)

        if len(result) > self._MAX_READ_CHARS:
            trimmed: list[str] = []
            current = 0
            for line in numbered:
                current += len(line) + 1
                if current > self._MAX_READ_CHARS:
                    break
                trimmed.append(line)
            end = start + len(trimmed)
            result = "\n".join(trimmed)

        if end < total:
            result += f"\n\n(Showing lines {offset}-{end} of {total}. Use offset={end + 1} to continue.)"
        else:
            result += f"\n\n(End of file — {total} lines total)"
        return result

    async def write_file(self, path: str, content: str) -> str:
        """Write text content to a file.

        Args:
            path: Destination file path.
            content: Full content to write.

        Returns:
            str: Operation result message.
        """
        fp = self.resolve_path(path)
        try:
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")
        except Exception as ex:  # pylint: disable=broad-except
            raise RuntimeError(f"Failed to write file '{path}': {ex}") from ex
        return f"Successfully wrote {len(content)} bytes to {fp}"

    async def edit_file(self, path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
        """Edit a file by replacing target text with new text.

        Args:
            path: File path to edit.
            old_text: Source text to replace.
            new_text: Replacement text.
            replace_all: Whether to replace all matches.

        Returns:
            str: Operation result message.
        """
        if old_text == "":
            raise ValueError("old_text must not be empty")

        fp = self.resolve_path(path)
        if not fp.exists():
            raise FileNotFoundError(f"File not found: {path}")
        if not fp.is_file():
            raise IsADirectoryError(f"Not a file: {path}")

        try:
            raw = fp.read_bytes()
        except Exception as ex:  # pylint: disable=broad-except
            raise RuntimeError(f"Failed to read file '{path}' for editing: {ex}") from ex

        uses_crlf = b"\r\n" in raw
        content = raw.decode("utf-8").replace("\r\n", "\n")

        match, count = _find_match(content, old_text.replace("\r\n", "\n"))
        if match is None:
            raise ValueError(_not_found_msg(old_text, content, path))
        if count > 1 and not replace_all:
            raise ValueError(f"old_text appears {count} times. Provide more context to make it unique, "
                             "or set replace_all=true.")

        normalized_new = new_text.replace("\r\n", "\n")
        if replace_all:
            new_content = content.replace(match, normalized_new)
        else:
            new_content = content.replace(match, normalized_new, 1)

        if uses_crlf:
            new_content = new_content.replace("\n", "\r\n")
        try:
            fp.write_bytes(new_content.encode("utf-8"))
        except Exception as ex:  # pylint: disable=broad-except
            raise RuntimeError(f"Failed to write edited file '{path}': {ex}") from ex
        return f"Successfully edited {fp}"

    async def list_dir(self, path: str, recursive: bool = False, max_entries: int = 200) -> str:
        """List directory entries with optional recursive traversal.

        Args:
            path: Directory path to list.
            recursive: Whether to include nested entries.
            max_entries: Maximum entries to include in output.

        Returns:
            str: Formatted listing.
        """
        dp = self.resolve_path(path)
        if not dp.exists():
            raise FileNotFoundError(f"Directory not found: {path}")
        if not dp.is_dir():
            raise NotADirectoryError(f"Not a directory: {path}")

        try:
            cap = (min(max_entries, self._MAX_LIST_ENTRIES) if max_entries > 0 else self._MAX_LIST_ENTRIES)
            items: list[str] = []
            total = 0

            if recursive:
                for item in sorted(dp.rglob("*")):
                    if any(part in self._IGNORE_DIRS for part in item.parts):
                        continue
                    total += 1
                    if len(items) < cap:
                        rel = item.relative_to(dp)
                        items.append(f"{rel}/" if item.is_dir() else str(rel))
            else:
                for item in sorted(dp.iterdir()):
                    if item.name in self._IGNORE_DIRS:
                        continue
                    total += 1
                    if len(items) < cap:
                        items.append(f"D {item.name}" if item.is_dir() else f"F {item.name}")
        except Exception as ex:  # pylint: disable=broad-except
            raise RuntimeError(f"Failed to list directory '{path}': {ex}") from ex

        if total == 0:
            return f"Directory {path} is empty"

        result = "\n".join(items)
        if total > cap:
            result += f"\n\n(truncated, showing first {cap} of {total} entries)"
        return result

    async def glob(self, pattern: str, path: str = ".") -> str:
        """Return files matching a glob pattern from a base directory.

        Args:
            pattern: Glob expression (e.g. ``**/*.py``).
            path: Base directory path.

        Returns:
            str: Newline-delimited matching file paths.
        """
        if not pattern:
            raise ValueError("pattern is required")

        base = self.resolve_path(path)
        if not base.exists():
            raise FileNotFoundError(f"Directory not found: {path}")
        if not base.is_dir():
            raise NotADirectoryError(f"Not a directory: {path}")

        try:
            if Path(pattern).is_absolute():
                matches = [Path(m) for m in pyglob.glob(pattern, recursive=True) if Path(m).is_file()]
            else:
                matches = [Path(m) for m in collect_files_with_glob(base.as_posix(), pattern)]
        except Exception as ex:  # pylint: disable=broad-except
            raise RuntimeError(f"Failed to glob files with pattern '{pattern}': {ex}") from ex

        if not matches:
            return "No files found."

        rows: list[str] = []
        for match in sorted(set(matches)):
            try:
                rows.append(match.relative_to(base).as_posix())
            except ValueError:
                rows.append(match.as_posix())
        return "\n".join(rows)

    async def grep(
        self,
        pattern: str,
        path: Optional[str] = None,
        glob: Optional[str] = None,
        output_mode: GrepOutputMode = "files_with_matches",
    ) -> str:
        """Search files by regex pattern and format output by mode.

        Args:
            pattern: Regex pattern to search.
            path: Optional file or directory path.
            glob: Optional glob filter for candidate files.
            output_mode: Output format for search results.

        Returns:
            str: Search results.
        """
        if not pattern:
            raise ValueError("pattern is required")

        base = self.resolve_path(path or ".")
        if not base.exists():
            raise FileNotFoundError(f"path not found: {path or '.'}")

        if output_mode not in {"files_with_matches", "content", "count"}:
            raise ValueError(f"unsupported output_mode '{output_mode}'")

        cmd = ["rg", "--no-messages"]
        if output_mode == "files_with_matches":
            cmd.append("--files-with-matches")
        elif output_mode == "count":
            cmd.append("-c")
        else:
            cmd.append("-n")

        if glob:
            cmd.extend(["--glob", glob])
        cmd.extend([pattern, base.as_posix()])

        try:
            process = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if process.returncode == 1:
                return "No matches found."
            if process.returncode > 1:
                stderr = process.stderr.strip() if process.stderr else "unknown error"
                raise RuntimeError(f"grep failed: {stderr}")

            output = (process.stdout or "").strip()
            return output or "No matches found."
        except FileNotFoundError as ex:
            if ex.filename and ex.filename != "rg":
                raise
            return self._grep_python_fallback(pattern=pattern, base=base, glob_pattern=glob, output_mode=output_mode)
        except Exception as ex:  # pylint: disable=broad-except
            raise RuntimeError(f"Failed to run grep: {ex}") from ex

    def _grep_python_fallback(
        self,
        pattern: str,
        base: Path,
        glob_pattern: Optional[str],
        output_mode: GrepOutputMode,
    ) -> str:
        """Fallback grep implementation used when ``rg`` is unavailable.

        Args:
            pattern: Regex pattern to search.
            base: Base file/directory path to scan.
            glob_pattern: Optional glob filter.
            output_mode: Output format for search results.

        Returns:
            str: Search results.
        """
        try:
            regex = re.compile(pattern)
        except re.error as ex:
            raise ValueError(f"invalid regex pattern: {ex}") from ex

        files: list[Path] = []
        if base.is_file():
            files = [base]
        else:
            for candidate in base.rglob("*"):
                if not candidate.is_file():
                    continue
                if glob_pattern and not candidate.match(glob_pattern):
                    continue
                files.append(candidate)

        file_hits: dict[Path, int] = {}
        content_hits: list[str] = []
        for file_path in files:
            try:
                for line_no, line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), start=1):
                    if regex.search(line):
                        file_hits[file_path] = file_hits.get(file_path, 0) + 1
                        if output_mode == "content":
                            try:
                                rel = file_path.relative_to(base.parent if base.is_file() else base).as_posix()
                            except ValueError:
                                rel = file_path.as_posix()
                            content_hits.append(f"{rel}:{line_no}:{line}")
            except Exception:  # pylint: disable=broad-except
                continue

        if not file_hits:
            return "No matches found."
        if output_mode == "files_with_matches":
            rows = []
            for file_path in sorted(file_hits):
                try:
                    rows.append(file_path.relative_to(base.parent if base.is_file() else base).as_posix())
                except ValueError:
                    rows.append(file_path.as_posix())
            return "\n".join(rows)
        if output_mode == "count":
            rows = []
            for file_path in sorted(file_hits):
                try:
                    rel = file_path.relative_to(base.parent if base.is_file() else base).as_posix()
                except ValueError:
                    rel = file_path.as_posix()
                rows.append(f"{rel}:{file_hits[file_path]}")
            return "\n".join(rows)
        return "\n".join(content_hits)
