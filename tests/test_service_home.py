"""service.configure keeps every file inside the configured dirs: HOME stays empty (no network)."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from service_fixtures import Library, tree
from test_discogs_pacing import Resp

from music_manager import fs as fs_mod
from music_manager import journal as journal_mod
from music_manager import service
from music_manager.apply import apply_album
from music_manager.fs import collect_albums
from music_manager.providers import discogs as discogs_mod
from music_manager.providers.discogs import Discogs, flatten_tracklist, release_artists
from music_manager.service import JobState, MatchStatus
from music_manager.service._state import state


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty HOME, no XDG variables, and the module paths back on their ~ defaults."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "DISCOGS_TOKEN",
                "DISCOGS_KEY", "DISCOGS_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(journal_mod, "STATE_DIR", None)
    monkeypatch.setattr(discogs_mod, "CACHE_DIR", None)
    monkeypatch.setattr(discogs_mod, "TOKEN_PATH", None)
    monkeypatch.setattr(fs_mod, "TRASH_DIR", None)
    # without configure, every default would be under this HOME
    assert journal_mod.state_dir() == home / ".local" / "state" / "discogs-tag"
    assert discogs_mod.cache_dir() == home / ".cache" / "discogs-tag"
    assert discogs_mod.token_path() == home / ".config" / "discogs-tag" / "token"
    assert fs_mod.trash_root() == home / ".local" / "share" / "Trash"
    yield home
    service.reset()


class Transport:
    """session.get for the real client: releases, masters and images from the test library's data."""

    def __init__(self, lib):
        self.lib = lib
        self.urls: list[str] = []

    def get(self, url, params=None, timeout=None):
        self.urls.append(url)
        if "/masters/" in url:
            return Resp(body={"year": 1998})
        if "/releases/" in url:
            rid = int(url.rsplit("/", 1)[-1])
            return Resp(body=self.lib.discogs.releases[rid])
        resp = Resp(body={})
        resp.content = self.lib.discogs.images[url]
        return resp


def test_configure_keeps_home_empty_through_build_apply_undo_fetch_and_token(home, tmp_path):
    lib = Library(tmp_path)
    data, conf = tmp_path / "data", tmp_path / "config"
    service.configure(data_dir=data, config_dir=conf, discogs=lib.discogs)
    assert journal_mod.state_dir() == data / "state"
    assert discogs_mod.cache_dir() == data / "cache" / "discogs"
    assert discogs_mod.token_path() == conf / "discogs-token"
    assert fs_mod.trash_root() == data / "Trash"
    service.settings.set(library_roots=[lib.root])
    assert service.jobs.wait(service.library.scan().id).state is JobState.DONE
    d = lib.album_id(lib.double)
    (lib.double / "cover.jpg").unlink()  # so apply saves folder.jpg and undo trashes it
    assert service.jobs.wait(service.library.scan().id).state is JobState.DONE
    lib.find(d)
    service.match.choose(d, 1000004)
    assert tree(home) == {}

    # token read: the real client finds the token in config_dir, not in ~/.config
    discogs_mod.save_token("tok-123")
    assert (conf / "discogs-token").read_text(encoding="utf-8").strip() == "tok-123"
    st = state()
    st.discogs = None
    client = st.client()
    assert isinstance(client, Discogs) and client.session.headers["Authorization"] == "Discogs token=tok-123"
    transport = Transport(lib)
    client.session = transport
    client.min_interval = 0

    # fetch: a stale release comes through plan.build's job and the real client (cache under data_dir)
    with st.index.tx() as conn:
        conn.execute("UPDATE candidates SET fetched_at = ? WHERE album_id = ?", (time.time() - 7 * 3600, d))
    pending = service.plan.build([d])
    assert pending.fetch_job is not None
    assert service.jobs.wait(pending.fetch_job).state is JobState.DONE
    assert any(u.endswith("/releases/1000004") for u in transport.urls)
    assert list((data / "cache" / "discogs" / "release").glob("*.json"))
    assert tree(home) == {}

    # build + apply: the journal lands in data_dir/journals
    plan = service.plan.build([d])
    assert plan.fetch_job is None and plan.albums[0].problems == []
    job = service.jobs.wait(service.plan.apply(plan.id).id)
    assert job.state is JobState.DONE, job.message
    journals = sorted((data / "journals").glob("apply-*.jsonl"))
    assert len(journals) == 1
    (row,) = st.index.query("SELECT id, journal FROM operations")
    assert Path(row["journal"]) == journals[0]
    assert (plan.albums[0].folder_after / "folder.jpg").is_file()
    assert tree(home) == {}

    # list + undo read that same journal; the trashed folder.jpg goes to data_dir/Trash
    ops = service.journal.list(0, 10).items
    assert [o.id for o in ops] == [row["id"]] and ops[0].undoable
    undo = service.journal.undo_plan(row["id"])
    job = service.jobs.wait(service.journal.undo(row["id"], undo.id).id)
    assert job.state is JobState.DONE, job.message
    assert service.library.album(d).status is MatchStatus.NEEDS_REVIEW
    assert (data / "Trash" / "files" / "folder.jpg").is_file()
    assert (data / "Trash" / "info" / "folder.jpg.trashinfo").is_file()
    assert sorted((data / "journals").glob("undo-*.jsonl"))  # the undo's own journal, same folder
    assert tree(home) == {}

    # a discogs-tag journal written in this process goes to data_dir/state and is listed from there
    copy = tmp_path / "cli" / "Band - Album"
    copy.mkdir(parents=True)
    for name in ("1-01 One", "1-02 Two", "2-01 Three", "2-02 Four"):
        (copy / f"{name}.flac").write_bytes((lib.double / f"{name}.flac").read_bytes())
    album = next(a for a in collect_albums(copy) if a.path == copy)
    rel = lib.multi
    apply_album(album, tmp_path / "cli-lib", rel, flatten_tracklist(rel, release_artists(rel)), "1998",
                dry_run=False)
    assert list((data / "state" / "journal").glob("apply-*.jsonl"))
    assert len(service.journal.list(0, 10).items) == 3
    assert tree(home) == {}
    assert threading.current_thread() is threading.main_thread()


def test_reset_puts_the_module_paths_back(home, tmp_path):
    service.configure(data_dir=tmp_path / "d", config_dir=tmp_path / "c", discogs=object())
    assert journal_mod.STATE_DIR == tmp_path / "d" / "state"
    service.reset()
    assert (journal_mod.STATE_DIR, discogs_mod.CACHE_DIR, discogs_mod.TOKEN_PATH, fs_mod.TRASH_DIR) == (None,) * 4
    assert journal_mod.state_dir() == home / ".local" / "state" / "discogs-tag"


def test_configure_without_dirs_keeps_the_user_paths(home, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    service.configure(discogs=object())
    assert discogs_mod.token_path() == home / ".config" / "discogs-tag" / "token"  # discogs-tag's own token
    assert fs_mod.trash_root() == tmp_path / "xdg-data" / "Trash"
    assert state().data_dir == tmp_path / "xdg-data" / "music-manager"
    assert not os.listdir(home)
