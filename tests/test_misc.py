"""Token file permissions, symlinked entry point, --self-test."""

import os
import stat
import subprocess
import sys

from conftest import REPO


def test_token_never_world_readable(dt, tmp_path, monkeypatch):
    path = tmp_path / "cfg" / "token"
    old = os.umask(0o022)
    seen = {}
    real_replace = os.replace

    def spy(src, dst):
        seen["mode"] = stat.S_IMODE(os.stat(src).st_mode)
        return real_replace(src, dst)

    monkeypatch.setattr(dt.os, "replace", spy)
    try:
        dt.save_token("  secret  ", path)
    finally:
        os.umask(old)
    assert seen["mode"] == 0o600  # already private before it got its real name
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == "secret\n"
    assert [p.name for p in path.parent.iterdir()] == ["token"]

    # replacing an existing, too-open token file also ends at 0600
    path.chmod(0o644)
    dt.save_token("new", path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.read_text() == "new\n"


def test_runs_through_symlink(tmp_path):
    link = tmp_path / "bin" / "discogs-tag"
    link.parent.mkdir()
    link.symlink_to(REPO / "discogs-tag.py")
    result = subprocess.run([sys.executable, str(link), "--self-test"], capture_output=True, text=True, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "self-test ok" in result.stdout

    m3u_link = tmp_path / "bin" / "album-m3u"
    m3u_link.symlink_to(REPO / "album-m3u")
    result = subprocess.run([sys.executable, str(m3u_link), "--help"], capture_output=True, text=True, cwd=tmp_path)
    assert result.returncode == 0, result.stderr


def test_help_lists_old_and_new_flags():
    result = subprocess.run([sys.executable, str(REPO / "discogs-tag.py"), "--help"], capture_output=True, text=True)
    for flag in ("--dest", "--dry-run", "--auto", "--yes", "--skip-tagged", "--release-id", "--limit",
                 "--save-token", "--self-test", "--clear-other-tags"):
        assert flag in result.stdout
