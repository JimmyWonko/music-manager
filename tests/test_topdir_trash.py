"""Per-drive trash (freedesktop Trash spec, "Trash directories"), with a fake topdir.

The "drive" is a tmp folder: fs._device and fs._mount_point are monkeypatched so files under
it look like another filesystem mounted there. Nothing touches a real mount; the optional
real-tmpfs test runs in a private, unprivileged mount namespace and is skipped without one.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from urllib.parse import unquote

import pytest

from music_manager import fs
from music_manager import journal as journal_mod
from music_manager import moves as moves_mod
from music_manager.journal import Journal, undo_journal

PENDING = pytest.mark.xfail(strict=True, reason="no per-drive trash yet")
UID = os.getuid()


@pytest.fixture
def drive(tmp_path, monkeypatch):
    """tmp/drive is a fake mount (device 2); everything else, the home trash included, is device 1."""
    home_trash = tmp_path / "home" / "Trash"
    top = tmp_path / "drive"
    (top / "Music").mkdir(parents=True)
    monkeypatch.setattr(fs, "TRASH_DIR", home_trash)
    monkeypatch.setattr(journal_mod, "STATE_DIR", tmp_path / "state")

    def on_drive(p):
        p = Path(os.path.abspath(p))
        return p == top or top in p.parents

    monkeypatch.setattr(fs, "_device", lambda p: 2 if on_drive(p) else 1, raising=False)
    monkeypatch.setattr(fs, "_mount_point", lambda p: top if on_drive(p) else Path("/"), raising=False)
    return top, home_trash


def playlist(top, name="My War.m3u", data=b"1. My War.flac\n"):
    path = top / "Music" / "Black Flag" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def info_of(trashed):
    text = (trashed.parent.parent / "info" / f"{trashed.name}.trashinfo").read_text(encoding="utf-8")
    return dict(line.split("=", 1) for line in text.splitlines()[1:])


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


@PENDING
def test_uses_dot_trash_dash_uid_created_0700(drive):
    top, home = drive
    src = playlist(top)
    trashed = fs.send_to_trash(src)
    root = top / f".Trash-{UID}"
    assert trashed == root / "files" / "My War.m3u" and trashed.read_bytes() == b"1. My War.flac\n"
    assert root.is_dir() and mode(root) == 0o700 and not src.exists()
    assert info_of(trashed)["Path"] == "Music/Black%20Flag/My%20War.m3u"  # relative to the topdir
    assert unquote(info_of(trashed)["Path"]) == str(src.relative_to(top))
    assert not home.exists()  # the home trash was not involved


@PENDING
def test_uses_the_shared_dot_trash_when_it_is_sticky(drive):
    top, _home = drive
    (top / ".Trash").mkdir()
    os.chmod(top / ".Trash", 0o1777)
    trashed = fs.send_to_trash(playlist(top))
    root = top / ".Trash" / str(UID)
    assert trashed == root / "files" / "My War.m3u" and mode(root) == 0o700
    assert info_of(trashed)["Path"] == "Music/Black%20Flag/My%20War.m3u"
    assert not (top / f".Trash-{UID}").exists()


@PENDING
@pytest.mark.parametrize("bad", ["no-sticky", "symlink", "file"])
def test_a_bad_shared_dot_trash_is_skipped_and_reported(drive, caplog, bad):
    top, _home = drive
    if bad == "no-sticky":
        (top / ".Trash").mkdir(mode=0o777)
        os.chmod(top / ".Trash", 0o777)
    elif bad == "symlink":
        (top / "elsewhere").mkdir()
        os.chmod(top / "elsewhere", 0o1777)
        (top / ".Trash").symlink_to(top / "elsewhere")
    else:
        (top / ".Trash").write_text("x", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="music_manager.fs"):
        trashed = fs.send_to_trash(playlist(top))
    assert trashed.parent.parent == top / f".Trash-{UID}"  # fell through to rule 2
    words = {"no-sticky": "sticky", "symlink": "symbolic link", "file": "not a directory"}[bad]
    assert any(str(top / ".Trash") in r.getMessage() and words in r.getMessage() for r in caplog.records)
    assert not any((top / "elsewhere").iterdir()) if bad == "symlink" else True


@PENDING
def test_shared_user_dir_that_is_a_symlink_is_not_used(drive, caplog):
    top, _home = drive
    (top / ".Trash").mkdir()
    os.chmod(top / ".Trash", 0o1777)
    (top / "mine").mkdir(mode=0o700)
    (top / ".Trash" / str(UID)).symlink_to(top / "mine")
    with caplog.at_level(logging.WARNING, logger="music_manager.fs"):
        trashed = fs.send_to_trash(playlist(top))
    assert trashed.parent.parent == top / f".Trash-{UID}" and not any((top / "mine").iterdir())
    assert any("symbolic link" in r.getMessage() for r in caplog.records)


@PENDING
@pytest.mark.parametrize("bad", ["mode", "owner", "symlink"])
def test_an_unsafe_dot_trash_dash_uid_falls_back_to_the_verified_copy(drive, monkeypatch, caplog, bad):
    top, home = drive
    uid = UID
    if bad == "mode":
        (top / f".Trash-{uid}").mkdir()
        os.chmod(top / f".Trash-{uid}", 0o755)
    elif bad == "owner":
        uid = UID + 4242  # the folder below belongs to us, not to this (pretend) user
        monkeypatch.setattr(fs, "_uid", lambda: uid, raising=False)
        (top / f".Trash-{uid}").mkdir(mode=0o700)
    else:
        (top / "elsewhere").mkdir(mode=0o700)
        (top / f".Trash-{uid}").symlink_to(top / "elsewhere")
    calls = []
    real = moves_mod._copy_verified
    monkeypatch.setattr(moves_mod, "_copy_verified", lambda a, b: (calls.append(a), real(a, b))[1])
    src = playlist(top)
    with caplog.at_level(logging.WARNING, logger="music_manager.fs"):
        trashed = fs.send_to_trash(src)
    assert trashed == home / "files" / "My War.m3u" and calls == [src] and not src.exists()
    assert info_of(trashed)["Path"] == str(src).replace(" ", "%20")  # absolute in the home trash
    words = {"mode": "0755", "owner": "owned by", "symlink": "symbolic link"}[bad]
    assert any(f".Trash-{uid}" in r.getMessage() and words in r.getMessage() for r in caplog.records)
    assert mode(top / f".Trash-{uid}") == (0o755 if bad == "mode" else mode(top / f".Trash-{uid}"))


@PENDING
@pytest.mark.skipif(os.getuid() == 0, reason="root can write a read-only folder")
def test_an_unwritable_topdir_falls_back_to_the_home_trash(drive, caplog):
    top, home = drive
    src = playlist(top)
    os.chmod(top, 0o555)
    try:
        with caplog.at_level(logging.WARNING, logger="music_manager.fs"):
            trashed = fs.send_to_trash(src)
    finally:
        os.chmod(top, 0o755)
    assert trashed.parent.parent == home and not (top / f".Trash-{UID}").exists()
    assert any(f".Trash-{UID}" in r.getMessage() for r in caplog.records)


@PENDING
def test_same_device_is_a_plain_rename_into_the_home_trash(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "TRASH_DIR", tmp_path / "home" / "Trash")
    src = tmp_path / "lib" / "a b.m3u"
    src.parent.mkdir()
    src.write_text("x", encoding="utf-8")
    ino = os.lstat(src).st_ino
    monkeypatch.setattr(moves_mod, "_copy_verified", lambda *_a: pytest.fail("no copy on one drive"))
    trashed = fs.send_to_trash(src)
    assert trashed.parent.parent == tmp_path / "home" / "Trash" and os.lstat(trashed).st_ino == ino
    assert not any(p.name.startswith(".Trash") for p in tmp_path.rglob("*"))
    target = fs.trash_for(tmp_path / "lib" / "other.m3u", create=False)
    assert (target.kind, target.root, target.topdir) == ("home", tmp_path / "home" / "Trash", None)


@PENDING
@pytest.mark.parametrize("where", ["topdir_user", "topdir", "home"])
def test_the_journal_records_the_trash_and_undo_restores_from_it(drive, monkeypatch, where):
    top, home = drive
    if where == "topdir":
        (top / ".Trash").mkdir()
        os.chmod(top / ".Trash", 0o1777)
    elif where == "home":
        (top / f".Trash-{UID}").mkdir()
        os.chmod(top / f".Trash-{UID}", 0o750)
    src = playlist(top, data=b"tracks\n" * 50)
    journal = Journal("test-trash")
    moves_mod._trash_logged(journal, src)
    journal.close()
    entries = [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines()]
    [entry] = [e for e in entries if e["action"] == "trashed"]
    expected_root = {"topdir_user": top / f".Trash-{UID}", "topdir": top / ".Trash" / str(UID), "home": home}[where]
    assert entry["trash_kind"] == where and entry["trash_dir"] == str(expected_root)
    assert Path(entry["trashed"]).parent.parent == expected_root
    assert bool(entry.get("trash_notes")) == (where == "home")
    assert undo_journal(journal.path, apply=True) == 0
    assert src.read_bytes() == b"tracks\n" * 50
    assert not any((expected_root / "files").iterdir()) and not any((expected_root / "info").iterdir())


@PENDING
def test_service_undo_preview_points_at_the_drives_trash(drive):
    top, _home = drive
    target = fs.trash_for(top / "Music" / "x.m3u", create=False)
    assert (target.kind, target.root, target.topdir) == ("topdir_user", top / f".Trash-{UID}", top)
    assert not (top / f".Trash-{UID}").exists()  # create=False only looks


REAL = textwrap.dedent("""
    import os, subprocess, sys
    from pathlib import Path
    tmp, src_dir = Path(sys.argv[1]), sys.argv[2]
    sys.path.insert(0, src_dir)
    mnt = tmp / "mnt"
    subprocess.run(["mount", "-t", "tmpfs", "-o", "size=4m", "none", str(mnt)], check=True)
    from music_manager import fs, journal as jm
    from music_manager.journal import Journal, undo_journal
    from music_manager.moves import _trash_logged
    fs.TRASH_DIR = tmp / "home" / "Trash"
    jm.STATE_DIR = tmp / "state"
    f = mnt / "Music" / "a b.m3u"
    f.parent.mkdir(parents=True)
    f.write_text("x\\n")
    assert os.stat(mnt).st_dev != os.stat(tmp).st_dev
    j = Journal("real-tmpfs")
    t = _trash_logged(j, f)
    j.close()
    assert t.parent.parent == mnt / f".Trash-{os.getuid()}", t
    info = (t.parent.parent / "info" / (t.name + ".trashinfo")).read_text()
    assert "Path=Music/a%20b.m3u" in info, info
    assert undo_journal(j.path, apply=True) == 0 and f.read_text() == "x\\n"
    subprocess.run(["umount", str(mnt)], check=True)
    print("REAL-TMPFS-OK")
""")


def _unshare_ok() -> bool:
    if sys.platform != "linux" or shutil.which("unshare") is None:
        return False
    probe = subprocess.run(["unshare", "-rm", "true"], capture_output=True, check=False)
    return probe.returncode == 0


@PENDING
@pytest.mark.skipif(not _unshare_ok(), reason="no unprivileged mount namespace (unshare -rm) here")
def test_real_tmpfs_in_a_private_mount_namespace(tmp_path):
    (tmp_path / "mnt").mkdir()
    src_dir = str(Path(fs.__file__).resolve().parents[1])
    env = {k: v for k, v in os.environ.items() if k != "DEFAULT_MUSIC"} | {"PYTHONDONTWRITEBYTECODE": "1"}
    run = subprocess.run(["unshare", "-rm", sys.executable, "-c", REAL, str(tmp_path), src_dir],
                         capture_output=True, text=True, env=env, timeout=60, check=False)
    assert run.returncode == 0 and "REAL-TMPFS-OK" in run.stdout, run.stdout + run.stderr
