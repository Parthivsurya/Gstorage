"""Replace the installed application with a verified update, and relaunch.

A running program cannot overwrite itself: on macOS the bundle is in use, on
Windows the executable is locked. So the swap is done by a small shell script
written to a temporary directory, launched detached, which waits for this
process to exit and only then moves anything.

The helper is a plain `sh` or `.bat` script on purpose. It must keep working
while the very application it is replacing is gone from disk, so it cannot
depend on that application's Python, its libraries, or anything inside the
bundle.

**The order matters more than anything else here.** The installed copy is
moved aside, never deleted, until the replacement is in place; if the move
fails the old one goes straight back. A user left with no working Gstore is
far worse than a user left on an old version, and that is the failure this
sequence is shaped to avoid.

Nothing of the user's is at risk either way: the index, the tokens and the
share records live in the application-data directory, never inside the
bundle. A failed update costs a reinstall, not data.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

from gstore.applog import get as get_logger

log = get_logger(__name__)

APP_NAME = "Gstore"

__all__ = [
    "InstallError",
    "begin_install",
    "can_install",
    "helper_script",
    "install_target",
    "stage_archive",
]


class InstallError(Exception):
    """The update could not be installed; the running copy is untouched."""


def install_target() -> Path | None:
    """The installed application this process runs from, or None.

    None means there is nothing to replace — the app is running from source,
    which is how a developer runs it. Offering to overwrite a checkout would
    be both wrong and destructive.
    """
    if not getattr(sys, "frozen", False):
        return None
    executable = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        for parent in executable.parents:
            if parent.suffix == ".app":
                return parent
        return None
    # Windows and Linux ship PyInstaller's COLLECT directory: the binary sits
    # at its top level, beside the files it needs.
    return executable.parent


def can_install() -> tuple[bool, str]:
    """Whether an update can be installed here, and why not if it cannot."""
    target = install_target()
    if target is None:
        return False, "Gstore is running from source, not an installed copy."
    if not os.access(target.parent, os.W_OK):
        return False, (
            f"{target.parent} is not writable, so Gstore cannot replace "
            "itself there. Install the update by hand."
        )
    return True, ""


def _staged_app(root: Path) -> Path:
    """Find the application inside an unpacked archive."""
    if sys.platform == "darwin":
        found = sorted(root.glob("*.app")) or sorted(root.glob("*/*.app"))
    else:
        found = [p for p in root.iterdir() if p.is_dir() and p.name == APP_NAME]
        if not found:
            found = [p for p in root.iterdir() if p.is_dir()]
    if not found:
        raise InstallError("the downloaded update did not contain the app")
    return found[0]


def stage_archive(archive: Path, workdir: Path) -> Path:
    """Unpack `archive` into `workdir`; return the new application's path.

    Raises :class:`InstallError` rather than leaving a half-unpacked tree
    that the swap might otherwise move into place.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    suffix = archive.name.lower()

    try:
        if suffix.endswith(".dmg"):
            mountpoint = workdir / "mnt"
            mountpoint.mkdir(exist_ok=True)
            subprocess.run(
                ["hdiutil", "attach", "-nobrowse", "-quiet",
                 "-mountpoint", str(mountpoint), str(archive)],
                check=True, capture_output=True,
            )
            try:
                source = _staged_app(mountpoint)
                destination = workdir / source.name
                # ditto, not copytree: it preserves the symlinks and extended
                # attributes a .app bundle relies on.
                subprocess.run(
                    ["ditto", str(source), str(destination)],
                    check=True, capture_output=True,
                )
            finally:
                subprocess.run(
                    ["hdiutil", "detach", "-quiet", str(mountpoint)],
                    check=False, capture_output=True,
                )
            return destination

        extracted = workdir / "unpacked"
        extracted.mkdir(exist_ok=True)
        if suffix.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(extracted)
        elif suffix.endswith((".tar.gz", ".tgz")):
            with tarfile.open(archive) as tf:
                tf.extractall(extracted)
        else:
            raise InstallError(f"unrecognised update archive: {archive.name}")
        return _staged_app(extracted)

    except subprocess.CalledProcessError as exc:
        raise InstallError(f"could not unpack the update: {exc}") from exc
    except (OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
        raise InstallError(f"the update archive is unreadable: {exc}") from exc


def helper_script(pid: int, staged: Path, target: Path, backup: Path) -> str:
    """The script that performs the swap once this process has exited.

    Written out rather than run inline because by the time it matters, the
    interpreter that would have run it is the thing being replaced.
    """
    if sys.platform.startswith("win"):
        return f"""@echo off
rem Wait for Gstore (PID {pid}) to exit before touching anything.
:wait
tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul
if not errorlevel 1 (
  timeout /t 1 /nobreak >nul
  goto wait
)

rem Move the old copy aside. Never delete it before the new one is in place.
move "{target}" "{backup}" >nul 2>&1
if errorlevel 1 exit /b 1

move "{staged}" "{target}" >nul 2>&1
if errorlevel 1 (
  rem Put the old one back rather than leave no app at all.
  move "{backup}" "{target}" >nul 2>&1
  exit /b 1
)

rmdir /s /q "{backup}" >nul 2>&1
start "" "{target}\\{APP_NAME}.exe"
"""

    launch = (
        f'open -a "{target}"'
        if sys.platform == "darwin"
        else f'"{target}/{APP_NAME}" &'
    )
    return f"""#!/bin/sh
# Wait for Gstore (PID {pid}) to exit before touching anything.
while kill -0 {pid} 2>/dev/null; do
  sleep 0.3
done

# Move the old copy aside. Never delete it before the new one is in place.
mv "{target}" "{backup}" || exit 1

if ! mv "{staged}" "{target}"; then
  # Put the old one back rather than leave no app at all.
  mv "{backup}" "{target}"
  exit 1
fi

rm -rf "{backup}"
{launch}
"""


def begin_install(archive: Path, workdir: Path) -> Path:
    """Stage the update and start the helper; the caller must then quit.

    Returns the helper's path. Nothing has been moved when this returns —
    the helper is waiting for this process to exit.
    """
    ok, why = can_install()
    if not ok:
        raise InstallError(why)

    target = install_target()
    assert target is not None  # can_install() just checked
    staged = stage_archive(Path(archive), Path(workdir))
    backup = target.with_name(target.name + ".old")

    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)

    script = Path(workdir) / (
        "gstore-update.bat" if sys.platform.startswith("win")
        else "gstore-update.sh"
    )
    script.write_text(
        helper_script(os.getpid(), staged, target, backup), encoding="utf-8"
    )
    script.chmod(0o755)

    log.info("update staged at %s; helper %s will swap it in", staged, script)
    command = (
        ["cmd", "/c", "start", "", "/min", str(script)]
        if sys.platform.startswith("win")
        else ["/bin/sh", str(script)]
    )
    subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # survives this process exiting
    )
    return script
