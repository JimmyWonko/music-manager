"""No built-in library path: a missing destination stops the run instead of
filing albums into whatever library happens to be mounted."""

import os
import subprocess
import sys

from conftest import REPO

import music_manager
from music_manager.cli.discogs_tag import default_dest
from music_manager.providers.discogs import USER_AGENT


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if k != "DEFAULT_MUSIC"}
    env.update(extra)
    return env


def test_no_hardcoded_library_paths():
    names = ["discogs-tag.py", "album-m3u", "README.md"]
    names += [str(p.relative_to(REPO)) for p in sorted((REPO / "src" / "music_manager").rglob("*.py"))]
    assert len(names) > 10
    for name in names:
        assert "/run/media/" not in (REPO / name).read_text(encoding="utf-8"), name


def test_user_agent_names_the_app():
    assert USER_AGENT.startswith(f"music-manager/{music_manager.__version__} ")
    assert "https://github.com/JimmyWonko/music-manager" in USER_AGENT
    assert "xen" not in USER_AGENT.casefold()


def test_default_dest_none_without_library(tmp_path):
    missing = tmp_path / "not-there"
    assert default_dest(tmp_path / "Artist - Album", library=missing) is None


def test_default_dest_uses_configured_library(tmp_path):
    lib = tmp_path / "Library"
    lib.mkdir()
    assert default_dest(tmp_path / "in" / "Artist - Album", library=lib) == lib.resolve()


def test_default_dest_featuring_wins(tmp_path):
    lib = tmp_path / "Library"
    lib.mkdir()
    album = tmp_path / "Other" / "Aesop Rock" / "Featuring" / "Some Album"
    assert default_dest(album, library=lib) == (tmp_path / "Other" / "Aesop Rock" / "Featuring").resolve()


def test_discogs_tag_refuses_without_dest(tmp_path, sample):
    album = tmp_path / "Artist - Album"
    sample("flac", "1. Song.flac", folder=album)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    result = subprocess.run(
        [sys.executable, str(REPO / "discogs-tag.py"), "--dry-run", str(album)],
        capture_output=True, text=True, env=_env(HOME=str(tmp_path)), cwd=tmp_path,
    )
    assert result.returncode == 2
    assert "no destination library" in result.stderr
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before


def test_album_m3u_refuses_without_root(tmp_path):
    result = subprocess.run(
        [sys.executable, str(REPO / "album-m3u"), "update", "--dry-run"],
        capture_output=True, text=True, env=_env(), cwd=tmp_path,
    )
    assert result.returncode == 2
    assert "no library root" in result.stderr


def test_album_m3u_uses_env_root(tmp_path):
    lib = tmp_path / "Library"
    (lib / "Album").mkdir(parents=True)
    result = subprocess.run(
        [sys.executable, str(REPO / "album-m3u"), "update", "--dry-run"],
        capture_output=True, text=True, env=_env(DEFAULT_MUSIC=str(lib)), cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
