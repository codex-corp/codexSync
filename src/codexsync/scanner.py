from __future__ import annotations

from pathlib import Path

from .exceptions import ConfigError
from .filters import PathFilter
from .models import FileMeta


def scan_tree(base_dir: Path, include_roots: list[str], path_filter: PathFilter) -> dict[str, FileMeta]:
    """
    Returns file index by normalized relative path.
    """
    result: dict[str, FileMeta] = {}
    # No roots means nothing is synchronised -- never "the whole directory",
    # which is where `auth.json` lives (CS-289). Validation refuses an empty
    # list; this is the second line.
    roots = include_roots
    base_resolved = base_dir.resolve()

    for root in roots:
        root_candidate = Path(root)
        if root_candidate.is_absolute():
            raise ConfigError(f"targets.include_roots must be relative paths: {root}")

        root_path = (base_resolved / root_candidate).resolve()
        if not _is_subpath(root_path, base_resolved):
            raise ConfigError(f"targets.include_roots points outside state dir: {root}")
        if root_path == base_resolved:
            raise ConfigError(
                f"targets.include_roots must name a folder or file inside the state dir, not the dir itself: {root!r}"
            )

        if not root_path.exists():
            continue

        if root_path.is_file():
            rel = root_path.relative_to(base_resolved).as_posix()
            if path_filter.is_excluded(rel):
                continue
            st = root_path.stat()
            result[rel] = FileMeta(rel, root_path, st.st_mtime_ns, st.st_size)
            continue

        for path in root_path.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(base_resolved).as_posix()
            if path_filter.is_excluded(rel):
                continue
            st = path.stat()
            result[rel] = FileMeta(rel, path, st.st_mtime_ns, st.st_size)

    return result


def _is_subpath(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True
