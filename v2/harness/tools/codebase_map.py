from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Directories that are never useful to map (would pollute the tree).
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn",
    "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    ".tox", ".venv", "venv", ".env", "env",
    "node_modules", ".next", "dist", "build",
    ".DS_Store",
})

# Extensions considered source code (determines "line count" eligibility).
_TEXT_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs",
    ".java", ".kt", ".scala",
    ".go", ".rs", ".c", ".cpp", ".h", ".hpp",
    ".rb", ".php", ".cs",
    ".sh", ".bash", ".zsh", ".fish",
    ".html", ".css", ".scss", ".sass",
    ".md", ".txt", ".rst",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".sql", ".graphql", ".proto",
    ".tf", ".hcl",
    ".dockerfile", ".Dockerfile",
    ".env.example",
})


@dataclass
class FileEntry:
    rel_path: str       # relative to project root, forward slashes
    extension: str      # e.g. ".py" (lowercase)
    line_count: int     # 0 if binary / unreadable


@dataclass
class CodebaseMap:
    root: str                               # absolute project root
    total_files: int
    total_lines: int
    by_extension: dict[str, int]            # ext → file count
    lines_by_extension: dict[str, int]      # ext → total line count
    tree_text: str                          # ASCII tree for display
    files: list[FileEntry] = field(default_factory=list)


def _count_lines(path: Path) -> int:
    """Return line count for text files; 0 for binaries or unreadable files."""
    try:
        with path.open("rb") as f:
            chunk = f.read(8192)
            if b"\x00" in chunk:
                return 0
            # Re-open as text for full count
        with path.open(encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def _build_tree(
    root: Path,
    entries: list[FileEntry],
    max_depth: int = 6,
) -> str:
    """Build an ASCII tree from the flat file list."""
    # Reconstruct the directory structure from rel_paths
    from collections import OrderedDict

    class _Node:
        def __init__(self):
            self.children: dict[str, _Node] = OrderedDict()
            self.is_file: bool = False

    tree_root = _Node()
    for entry in entries:
        parts = entry.rel_path.replace("\\", "/").split("/")
        node = tree_root
        for part in parts[:-1]:
            node = node.children.setdefault(part, _Node())
        leaf = node.children.setdefault(parts[-1], _Node())
        leaf.is_file = True

    lines: list[str] = [str(root.name) + "/"]

    def _render(node: _Node, prefix: str, depth: int) -> None:
        if depth > max_depth:
            lines.append(prefix + "... (truncated)")
            return
        items = list(node.children.items())
        for i, (name, child) in enumerate(items):
            connector = "└── " if i == len(items) - 1 else "├── "
            suffix = "" if child.is_file else "/"
            lines.append(prefix + connector + name + suffix)
            if not child.is_file:
                extension = "    " if i == len(items) - 1 else "│   "
                _render(child, prefix + extension, depth + 1)

    _render(tree_root, "", 1)
    return "\n".join(lines)


def build_codebase_map(
    project_root: Path,
    max_files: int = 2000,
) -> CodebaseMap:
    """
    Walk project_root and return a lightweight CodebaseMap.

    No AST parsing — only: file paths, extensions, line counts.
    Skips _SKIP_DIRS and respects max_files limit.
    """
    root = project_root.resolve()
    entries: list[FileEntry] = []
    ext_count: dict[str, int] = defaultdict(int)
    ext_lines: dict[str, int] = defaultdict(int)

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skipped directories in-place so os.walk doesn't recurse into them
        dirnames[:] = [d for d in sorted(dirnames) if d not in _SKIP_DIRS]

        for fname in sorted(filenames):
            if len(entries) >= max_files:
                log.warning("codebase_map: max_files=%d reached, stopping walk", max_files)
                break

            full_path = Path(dirpath) / fname
            try:
                rel = full_path.relative_to(root).as_posix()
            except ValueError:
                continue

            ext = full_path.suffix.lower() or ""
            lines = _count_lines(full_path) if ext in _TEXT_EXTENSIONS else 0

            entries.append(FileEntry(rel_path=rel, extension=ext, line_count=lines))
            ext_count[ext] += 1
            ext_lines[ext] += lines

    total_lines = sum(e.line_count for e in entries)
    tree = _build_tree(root, entries)

    log.info(
        "codebase_map built: %d files, %d lines, root=%s",
        len(entries),
        total_lines,
        root,
    )

    return CodebaseMap(
        root=str(root),
        total_files=len(entries),
        total_lines=total_lines,
        by_extension=dict(ext_count),
        lines_by_extension=dict(ext_lines),
        tree_text=tree,
        files=entries,
    )


_CACHE_FILENAME = "codebase-map.json"
_CACHE_VERSION = 1


def _cache_key(root: Path) -> str:
    """
    Compute a lightweight invalidation key from file count and cumulative mtime.

    Walks the tree (skipping _SKIP_DIRS) and sums integer mtimes of all files.
    If any file changes, is added, or is removed, this key changes.
    Fast enough for typical project sizes; no hashing needed.
    """
    total_mtime = 0
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            try:
                mtime = int((Path(dirpath) / fname).stat().st_mtime * 1000)
                total_mtime += mtime
                count += 1
            except OSError:
                pass
    return f"v{_CACHE_VERSION}:{count}:{total_mtime}"


def load_cached_map(cache_dir: Path, project_root: Path) -> CodebaseMap | None:
    """
    Load a cached CodebaseMap if it is still valid.

    Validity is determined by comparing the stored cache_key against a freshly
    computed one. Returns None if the cache is missing, stale, or corrupt.
    """
    cache_path = cache_dir / _CACHE_FILENAME
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        stored_key = data.get("cache_key", "")
        current_key = _cache_key(project_root)
        if stored_key != current_key:
            log.debug("Codebase map cache stale (key changed)")
            return None
        files = [FileEntry(**f) for f in data.get("files", [])]
        cmap = CodebaseMap(
            root=data["root"],
            total_files=data["total_files"],
            total_lines=data["total_lines"],
            by_extension=data["by_extension"],
            lines_by_extension=data["lines_by_extension"],
            tree_text=data["tree_text"],
            files=files,
        )
        log.debug("Codebase map loaded from cache (%d files)", cmap.total_files)
        return cmap
    except Exception as exc:
        log.warning("Could not read codebase map cache: %s", exc)
        return None


def save_cached_map(cmap: CodebaseMap, cache_dir: Path, project_root: Path) -> None:
    """Persist a CodebaseMap to cache/codebase-map.json."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / _CACHE_FILENAME
    data = {
        "cache_key": _cache_key(project_root),
        "root": cmap.root,
        "total_files": cmap.total_files,
        "total_lines": cmap.total_lines,
        "by_extension": cmap.by_extension,
        "lines_by_extension": cmap.lines_by_extension,
        "tree_text": cmap.tree_text,
        "files": [asdict(f) for f in cmap.files],
    }
    cache_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.debug("Codebase map saved to cache (%d files)", cmap.total_files)


def get_or_build_codebase_map(
    project_root: Path,
    cache_dir: Path | None = None,
    max_files: int = 2000,
) -> CodebaseMap:
    """
    Return a CodebaseMap, loading from cache if valid or rebuilding if stale.

    Args:
        project_root: absolute project root to map
        cache_dir:    directory for cache/codebase-map.json (default: project_root/cache)
        max_files:    hard cap on file count during rebuild
    """
    if cache_dir is None:
        cache_dir = project_root / "cache"

    cached = load_cached_map(cache_dir, project_root)
    if cached is not None:
        return cached

    log.info("Rebuilding codebase map for %s", project_root)
    cmap = build_codebase_map(project_root, max_files=max_files)
    save_cached_map(cmap, cache_dir, project_root)
    return cmap


def format_codebase_map(cmap: CodebaseMap, include_file_list: bool = False) -> str:
    """Render a CodebaseMap as a human-readable (and LLM-readable) string."""
    lines: list[str] = [
        "# Codebase Map",
        "",
        f"Root:         {cmap.root}",
        f"Total files:  {cmap.total_files}",
        f"Total lines:  {cmap.total_lines:,}",
        "",
        "## File tree",
        "",
        "```",
        cmap.tree_text,
        "```",
        "",
        "## By extension",
        "",
    ]

    if cmap.by_extension:
        sorted_exts = sorted(
            cmap.by_extension.items(), key=lambda kv: -kv[1]
        )
        lines.append(f"{'Extension':<14}  {'Files':>6}  {'Lines':>8}")
        lines.append("-" * 34)
        for ext, count in sorted_exts:
            lcount = cmap.lines_by_extension.get(ext, 0)
            label = ext if ext else "(no ext)"
            lines.append(f"{label:<14}  {count:>6}  {lcount:>8,}")
    else:
        lines.append("(no files found)")

    if include_file_list:
        lines += ["", "## All files", ""]
        for entry in cmap.files:
            lc = f"  ({entry.line_count:,} lines)" if entry.line_count else ""
            lines.append(f"  {entry.rel_path}{lc}")

    return "\n".join(lines)
