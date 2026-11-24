"""Turn a local directory into a plan for uploading it.

Pure filesystem work: no Drive, no database, no Qt, so the decisions about
*what* gets uploaded can be tested without touching any of them.

The decisions, and why:

* **Symlinks are skipped, never followed.** A link pointing at a parent makes
  the walk infinite, and one pointing outside the chosen folder would upload
  files the user did not choose.
* **Dot-entries are skipped**, directories included. Nobody picking "Photos"
  means to upload `.DS_Store`, and a stray `.git` would be thousands of files
  the user never saw. They are reported rather than silently dropped.
* **Empty directories are kept.** They cost nothing and a folder that came
  back missing its empty subfolders would not be the folder that went up.
* **Unreadable entries are recorded, not raised.** One bad file should not
  stop the other 499.
* **Dependency directories are skipped**, `node_modules` above all. They are
  rebuilt from a manifest rather than authored, and they dwarf everything
  else: a real project measured here held 28,481 files, 69 of them the
  user's. Pass ``skip_dependencies=False`` to take them anyway.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["PlannedFile", "FolderPlan", "scan_folder"]

SKIPPED_HIDDEN = "hidden"
SKIPPED_SYMLINK = "symlink"
SKIPPED_UNREADABLE = "unreadable"
SKIPPED_DEPENDENCY = "dependency"

# Directories holding machine-generated content that is rebuilt from a
# manifest, not authored. A real project measured here was 28,481 files, of
# which 69 were the user's -- the rest was node_modules. Uploading that costs
# hours to store something `npm install` reproduces exactly.
#
# Skipped by default and always reported, never silently: the count is shown
# before anything is uploaded so the choice stays the user's.
DEPENDENCY_DIRS = frozenset({
    "node_modules",
    "__pycache__",
    "venv",
    "env",
    "vendor",
    "Pods",
    "build",
    "dist",
    "target",
    "out",
    ".gradle",
    "DerivedData",
    "bower_components",
    "site-packages",
})


@dataclass(frozen=True)
class PlannedFile:
    """One file to upload, and where it belongs relative to the chosen root."""

    path: Path
    # Directory names between the chosen folder and this file. Empty tuple
    # means the file sits directly in the chosen folder.
    rel_dir: tuple[str, ...]
    size_bytes: int


@dataclass
class FolderPlan:
    root: Path
    files: list[PlannedFile] = field(default_factory=list)
    # Every directory to recreate, parents before children, so a caller can
    # make them in order without sorting.
    dirs: list[tuple[str, ...]] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)

    @property
    def file_count(self) -> int:
        return len(self.files)


def _is_hidden(name: str) -> bool:
    return name.startswith(".")


def scan_folder(
    root: str | Path, skip_dependencies: bool = True
) -> FolderPlan:
    """Walk `root` and return everything that should be uploaded from it.

    Directories come back parents-first; files carry the directory path they
    belong under. Nothing here raises for a single unreadable entry — those
    land in `skipped` with a reason.
    """
    base = Path(root)
    plan = FolderPlan(root=base)

    def walk(directory: Path, rel: tuple[str, ...]) -> None:
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            plan.skipped.append((directory, SKIPPED_UNREADABLE))
            return

        for entry in entries:
            if entry.is_symlink():
                plan.skipped.append((entry, SKIPPED_SYMLINK))
                continue
            if _is_hidden(entry.name):
                plan.skipped.append((entry, SKIPPED_HIDDEN))
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                plan.skipped.append((entry, SKIPPED_UNREADABLE))
                continue

            if is_dir:
                if skip_dependencies and entry.name in DEPENDENCY_DIRS:
                    plan.skipped.append((entry, SKIPPED_DEPENDENCY))
                    continue
                child = rel + (entry.name,)
                plan.dirs.append(child)       # parent already appended: ordered
                walk(entry, child)
                continue

            try:
                size = entry.stat().st_size
            except OSError:
                plan.skipped.append((entry, SKIPPED_UNREADABLE))
                continue
            plan.files.append(
                PlannedFile(path=entry, rel_dir=rel, size_bytes=size)
            )

    walk(base, ())
    return plan
