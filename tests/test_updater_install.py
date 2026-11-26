"""Tests for replacing the installed app with a verified update.

This is the most destructive thing Gstore does, so the tests run the **real
helper script** against fake installations in a temp directory. The script is
the dangerous artefact — getting it wrong leaves someone with no working app
— so it is executed rather than merely inspected.

The property that matters above all: the installed copy is moved aside, never
deleted, until the replacement is in place. If the move fails, the old one
goes back. Being left on an old version is a disappointment; being left with
nothing is a disaster.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from gstore.updater import install as install_mod
from gstore.updater.install import (
    InstallError,
    can_install,
    helper_script,
    install_target,
    stage_archive,
)

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="the POSIX helper is exercised here; the .bat is generated only",
)


def _run_helper(script: str, tmp_path: Path) -> subprocess.CompletedProcess:
    path = tmp_path / "helper.sh"
    path.write_text(script)
    path.chmod(0o755)
    return subprocess.run(
        ["/bin/sh", str(path)], capture_output=True, text=True, timeout=30
    )


def _fake_app(root: Path, name: str, marker: str) -> Path:
    app = root / name
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "version.txt").write_text(marker)
    return app


# -- knowing whether we may install at all -----------------------------------


def test_running_from_source_has_no_install_target():
    """A checkout must never be overwritten by an update."""
    assert install_target() is None
    ok, why = can_install()
    assert ok is False
    assert "from source" in why


def test_a_frozen_mac_app_resolves_to_the_bundle(monkeypatch, tmp_path):
    bundle = tmp_path / "Gstore.app"
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    monkeypatch.setattr(install_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        install_mod.sys, "executable", str(bundle / "Contents/MacOS/Gstore")
    )
    monkeypatch.setattr(install_mod.sys, "platform", "darwin")

    assert install_target() == bundle


def test_a_frozen_linux_build_resolves_to_its_directory(monkeypatch, tmp_path):
    folder = tmp_path / "Gstore"
    folder.mkdir()
    monkeypatch.setattr(install_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(install_mod.sys, "executable", str(folder / "Gstore"))
    monkeypatch.setattr(install_mod.sys, "platform", "linux")

    assert install_target() == folder


def test_an_unwritable_location_is_refused(monkeypatch, tmp_path):
    """Installing into /Applications may need rights the user has not given."""
    parent = tmp_path / "locked"
    parent.mkdir()
    bundle = parent / "Gstore.app"
    bundle.mkdir()
    monkeypatch.setattr(install_mod, "install_target", lambda: bundle)
    parent.chmod(0o500)
    try:
        ok, why = can_install()
        assert ok is False
        assert "not writable" in why
    finally:
        parent.chmod(0o755)


# -- unpacking what was downloaded -------------------------------------------


def test_a_zip_yields_the_app_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(install_mod.sys, "platform", "linux")
    archive = tmp_path / "Gstore-windows-x64.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Gstore/Gstore.exe", "binary")
        zf.writestr("Gstore/base_library.zip", "libs")

    staged = stage_archive(archive, tmp_path / "work")

    assert staged.name == "Gstore"
    assert (staged / "Gstore.exe").exists()


def test_a_tarball_yields_the_app_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(install_mod.sys, "platform", "linux")
    payload = tmp_path / "Gstore"
    payload.mkdir()
    (payload / "Gstore").write_text("binary")
    archive = tmp_path / "Gstore-linux-x64.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(payload, arcname="Gstore")

    staged = stage_archive(archive, tmp_path / "work")

    assert (staged / "Gstore").exists()


def test_an_unrecognised_archive_is_refused(tmp_path):
    archive = tmp_path / "Gstore.rar"
    archive.write_bytes(b"not an archive")

    with pytest.raises(InstallError, match="unrecognised"):
        stage_archive(archive, tmp_path / "work")


def test_a_corrupt_zip_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(install_mod.sys, "platform", "linux")
    archive = tmp_path / "Gstore-windows-x64.zip"
    archive.write_bytes(b"PK\x03\x04 and then rubbish")

    with pytest.raises(InstallError):
        stage_archive(archive, tmp_path / "work")


# -- the swap itself, running the real script --------------------------------


def test_the_swap_replaces_the_app_and_removes_the_backup(tmp_path):
    installed = _fake_app(tmp_path, "Gstore.app", "old")
    staged = _fake_app(tmp_path / "staging", "Gstore.app", "new")
    backup = installed.with_name("Gstore.app.old")

    # PID 1 never exits, so use this process's parent trick: a PID that is
    # already gone makes the wait loop fall straight through.
    dead_pid = 999_999
    result = _run_helper(
        helper_script(dead_pid, staged, installed, backup).replace(
            'open -a "%s"' % installed, "true"
        ),
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert (installed / "Contents" / "version.txt").read_text() == "new"
    assert not backup.exists(), "the backup should be cleaned up on success"


def test_a_failed_swap_puts_the_old_app_back(tmp_path):
    """The disaster case: never leave the user with no application."""
    installed = _fake_app(tmp_path, "Gstore.app", "old")
    missing = tmp_path / "staging" / "Gstore.app"   # never created
    backup = installed.with_name("Gstore.app.old")

    result = _run_helper(
        helper_script(999_999, missing, installed, backup).replace(
            'open -a "%s"' % installed, "true"
        ),
        tmp_path,
    )

    assert result.returncode == 1
    assert installed.exists(), "the installed app was not restored"
    assert (installed / "Contents" / "version.txt").read_text() == "old"


def test_the_helper_waits_for_the_process_to_exit(tmp_path):
    """Swapping while the app still runs is how a bundle gets corrupted."""
    installed = _fake_app(tmp_path, "Gstore.app", "old")
    staged = _fake_app(tmp_path / "staging", "Gstore.app", "new")
    backup = installed.with_name("Gstore.app.old")

    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
    script = tmp_path / "helper.sh"
    script.write_text(
        helper_script(victim.pid, staged, installed, backup).replace(
            'open -a "%s"' % installed, "true"
        )
    )
    script.chmod(0o755)
    helper = subprocess.Popen(["/bin/sh", str(script)])

    # While the victim lives, nothing may have moved.
    import time
    time.sleep(0.8)
    assert victim.poll() is None, "the victim exited too early for this test"
    assert (installed / "Contents" / "version.txt").read_text() == "old", (
        "the helper swapped while the app was still running"
    )

    victim.wait(timeout=10)
    helper.wait(timeout=20)
    assert (installed / "Contents" / "version.txt").read_text() == "new"


def test_the_windows_helper_restores_on_failure(monkeypatch):
    """The .bat cannot run here, so check the ordering it encodes.

    Paths are rendered by the host's os.path, so this compares the order of
    the operations rather than their exact separators.
    """
    monkeypatch.setattr(install_mod.sys, "platform", "win32")
    app, new_, old_ = Path("C:/app"), Path("C:/new"), Path("C:/old")
    script = helper_script(123, new_, app, old_)

    move_aside = script.index(f'move "{app}" "{old_}"')
    move_in = script.index(f'move "{new_}" "{app}"')
    restore = script.index(f'move "{old_}" "{app}"')
    delete = script.index("rmdir /s /q")

    assert move_aside < move_in < restore < delete, (
        "the old copy must be moved aside first and only deleted last"
    )
    assert "tasklist" in script, "the helper must wait for the app to exit"


def test_no_script_deletes_before_it_has_replaced(monkeypatch):
    """Both platforms, stated as one rule."""
    for platform in ("darwin", "linux", "win32"):
        monkeypatch.setattr(install_mod.sys, "platform", platform)
        script = helper_script(1, Path("/new"), Path("/app"), Path("/old"))
        remove = script.find("rm -rf")
        if remove == -1:
            remove = script.find("rmdir")
        swap = max(script.find('"/new" "/app"'), script.find('"/new"'))
        assert swap < remove, f"{platform}: deletes before replacing"
