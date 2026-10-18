"""Review item 1: flat two-disc rips whose numbering restarts per disc ("Live At The Other End").

39 tracks in one folder (disc 1: 20, disc 2: 19), with CD1/ and CD2/ holding only sidecars
(.cue, .log, artwork). The pairing must be right or the album must be held (disc_ambiguous),
in the service and the CLI; the sidecar-only disc folders are neither albums nor disc hints.
Synthetic files only: each file's length says which track it really is.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json

import pytest
from disc_fixtures import FIXTURES, silent_flac
from mutagen.flac import FLAC
from service_fixtures import FakeDiscogs, search_result, tree

from music_manager import service
from music_manager.cli import discogs_tag as cli
from music_manager.fs import collect_albums
from music_manager.match import align_tracks
from music_manager.providers.discogs import flatten_tracklist, release_artists
from music_manager.service import JobState

RID = 6262
WORDS = ("Amber Fox", "Billy The Bum", "Copper Kettle", "Dust Devil", "Ember Road", "Forbidden Jimmy",
         "Glass Harbor", "Hollow Pine", "Iron Lantern", "Jade Window", "Kite String", "Lemon Moon",
         "Marble Stair", "Night Ferry", "Oak Parlor", "Paper Crown", "Quiet Engine", "River Clock",
         "Silver Dune", "Tin Soldier",
         "Atlas Hotel", "Flashback Blues", "Cobalt Rain", "Living In The Future", "Echo Valley",
         "Feather Bell", "Granite Song", "Harvest Wire", "Ivory Gate", "Juniper Lane", "Kettle Drum",
         "Lantern Bay", "Midnight Mill", "North Station", "Orchid Smoke", "Pepper Field", "Quarry Light",
         "Rust Bucket", "Sable Coast")
DISC1, DISC2 = WORDS[:20], WORDS[20:]
TRACKS = [(1, n, t, 100 + i) for i, (n, t) in enumerate(enumerate(DISC1, 1))] + \
         [(2, n, t, 200 + i) for i, (n, t) in enumerate(enumerate(DISC2, 1))]
TRUTH = {s: (d, n, t) for d, n, t, s in TRACKS}
FOLDER = ("Live Band", "[1975]  Live At The Other End [OE-2]")
TARGET = ("Live Band", "[1975]  Live At The Other End 2CD [OE-2]")
SIDECARS = ("CD1/Live At The Other End CD1.cue", "CD1/Live At The Other End CD1.log", "CD1/Artwork/front.jpg",
            "CD2/Live At The Other End CD2.cue", "CD2/Live At The Other End CD2.log", "CD2/back.jpg")


def make_release(tracks=TRACKS) -> dict:
    rel = copy.deepcopy(json.loads((FIXTURES / "release_multi_cd.json").read_text(encoding="utf-8")))
    rel.update(id=RID, title="Live At The Other End", year=1975, released="1975", master_id=0)
    rel["artists"] = [{"name": "Live Band"}]
    rel["labels"] = [{"name": "Other End", "catno": "OE-2"}]
    rel["tracklist"] = [{"position": f"{d}-{n}", "type_": "track", "title": t, "duration": f"{s // 60}:{s % 60:02d}"}
                        for d, n, t, s in tracks]
    return rel


def make_flat(folder, tracks=TRACKS, names=None):
    """The flat rip: "1. Title.flac" restarting per disc, sidecars only in CD1/ and CD2/."""
    for i, (_d, n, t, s) in enumerate(tracks):
        name = names[i] if names else f"{n}. {t}"
        silent_flac(folder / f"{name}.flac", s)
    for rel in SIDECARS:
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"sidecar " + rel.encode())
    return folder


def identity(path, truth=TRUTH):
    return truth[round(FLAC(path).info.length)]


def tagged(path):
    tags = FLAC(path)
    return int(tags["discnumber"][0].split("/")[0]), int(tags["tracknumber"][0].split("/")[0]), tags["title"][0]


def swapped(root, truth=TRUTH):
    return sorted(p.name for p in root.rglob("*.flac") if tagged(p) != identity(p, truth))


def test_the_layout_is_one_album_and_the_sidecar_folders_are_extras(tmp_path):
    folder = make_flat(tmp_path.joinpath(*FOLDER))
    albums = collect_albums(tmp_path)
    assert [a.path for a in albums] == [folder]  # CD1/ and CD2/ are not albums
    album = albums[0]
    assert len(album.tracks) == 39 and all(t.parent == folder for t in album.tracks)
    assert [d.name for d in album.extra_dirs] == ["CD1", "CD2"]
    # some disc-2 titles sort before their disc-1 namesakes: the old pairing swapped exactly these
    names = [t.name for t in album.tracks]
    assert names.index("2. Billy The Bum.flac") < names.index("2. Flashback Blues.flac")


def test_engine_pairs_every_track_on_its_disc(tmp_path):
    folder = make_flat(tmp_path.joinpath(*FOLDER))
    album = collect_albums(tmp_path)[0]
    rel = make_release()
    triples, _ = align_tracks(album, flatten_tracklist(rel, release_artists(rel)))
    assert len(triples) == 39
    for local, dt, method in triples:
        assert method == "number" and (dt.disc, dt.number, dt.title) == identity(local), local.name
    assert folder.is_dir()


# The review's own example: file numbers that disagree with the release (Billy The Bum is 1-2 on
# Discogs but "1." on disk). Number and title contradict each other, so nothing is guessed.
REVIEW_RELEASE = [(1, 1, "Flashback Blues", 181), (1, 2, "Billy The Bum", 202), (2, 1, "Forbidden Jimmy", 244),
                  (2, 2, "Living In The Future", 265)]
REVIEW_NAMES = ["1. Flashback Blues", "1. Billy The Bum", "2. Forbidden Jimmy", "2. Living In The Future"]


def test_engine_holds_the_reviews_contradictory_example(tmp_path):
    folder = make_flat(tmp_path / "x" / "Live At The Other End", REVIEW_RELEASE, REVIEW_NAMES)
    album = next(a for a in collect_albums(tmp_path) if a.path == folder)
    rel = make_release(REVIEW_RELEASE)
    triples, _ = align_tracks(album, flatten_tracklist(rel, release_artists(rel)))
    got = {local.name: (method, dt.title if dt else None) for local, dt, method in triples}
    # "1. Billy The Bum" names a disc-1 title but not track 1, so neither "1." file can be placed;
    # the same for "2. Forbidden Jimmy". A group is placed whole or not at all.
    assert got == {f"{n}.flac": ("ambiguous_disc", None) for n in REVIEW_NAMES}


# --- service ---------------------------------------------------------------------------------

@pytest.fixture
def svc(tmp_path):
    discogs = FakeDiscogs()
    rel = make_release()
    discogs.releases[RID] = rel
    discogs.searches["live at the other end"] = [search_result(rel)]
    root = tmp_path / "lib"
    root.mkdir()
    service.configure(data_dir=tmp_path / "data", config_dir=tmp_path / "config", discogs=discogs)
    service.settings.set(library_roots=[root])
    yield root, discogs
    service.reset()


def _choose(root):
    assert service.jobs.wait(service.library.scan().id).state is JobState.DONE
    rows = service.library.albums(service.AlbumFilter(), service.AlbumSort.FOLDER, 0, 100).items
    assert len(rows) == 1  # CD1/ and CD2/ are not albums
    album_id = rows[0].id
    assert service.jobs.wait(service.match.find([album_id]).id).state is JobState.DONE
    service.match.choose(album_id, RID)
    return album_id


def test_service_apply_then_reapply(svc):
    root, _discogs = svc
    make_flat(root.joinpath(*FOLDER))
    album_id = _choose(root)
    plan = service.plan.build([album_id])
    assert plan.albums[0].problems == []
    job = service.jobs.wait(service.plan.apply(plan.id).id)
    assert job.state is JobState.DONE, job.message
    target = root.joinpath(*TARGET)
    assert len(list(target.glob("*.flac"))) == 39 and swapped(root) == []
    assert sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file() and p.suffix != ".flac"
                  and p.suffix != ".m3u") == sorted(SIDECARS)
    service.match.choose(album_id, RID)
    again = service.plan.build([album_id])
    assert again.albums[0].problems == [] and again.tags_to_write == 0
    assert all(f.path_before == f.path_after for f in again.albums[0].files)


def test_service_holds_the_reviews_contradictory_example(svc):
    root, discogs = svc
    rel = make_release(REVIEW_RELEASE)
    discogs.releases[RID] = rel
    discogs.searches["live at the other end"] = [search_result(rel)]
    make_flat(root.joinpath(*FOLDER), REVIEW_RELEASE, REVIEW_NAMES)
    album_id = _choose(root)
    before = tree(root)
    plan = service.plan.build([album_id])
    [problem] = [p for p in plan.albums[0].problems if p.kind == "disc_ambiguous"]
    assert sorted(p.name for p in problem.paths) == sorted(f"{n}.flac" for n in REVIEW_NAMES)
    with pytest.raises(service.MMError):
        service.plan.apply(plan.id)
    assert tree(root) == before


# --- CLI -------------------------------------------------------------------------------------

class FakeCli:
    release_data = None

    def __init__(self, *_a, **_k):
        self.on_wait = None

    def search_releases(self, *_a, **_k):
        return [search_result(self.release_data)]

    def release(self, release_id):
        return copy.deepcopy(self.release_data)

    def master(self, master_id):
        return {}

    def image(self, url):
        raise cli.DiscogsError("no images")


def run_cli(monkeypatch, rel, *argv):
    monkeypatch.setattr(FakeCli, "release_data", rel)
    monkeypatch.setattr(cli, "Discogs", FakeCli)
    monkeypatch.setattr(cli, "load_token", lambda: ("token", None, None))
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.main([str(a) for a in argv])
    return code, out.getvalue()


@pytest.mark.parametrize("pick", [("--release-id", str(RID)), ("--auto",)], ids=["release-id", "auto"])
def test_cli_apply_then_reapply(tmp_path, monkeypatch, pick):
    src = make_flat(tmp_path.joinpath("in", *FOLDER))
    lib = tmp_path / "lib"
    lib.mkdir()
    code, out = run_cli(monkeypatch, make_release(), src, "--dest", lib, *pick, "--yes")
    assert code == 0 and "applied=1" in out, out
    target = lib.joinpath(*TARGET)
    assert len(list(target.glob("*.flac"))) == 39 and swapped(lib) == []
    assert (target / "CD1" / "Live At The Other End CD1.cue").is_file()
    code, out = run_cli(monkeypatch, make_release(), target, "--dest", lib, *pick, "--yes")
    assert code == 0, out
    assert swapped(lib) == [] and len(list(target.glob("*.flac"))) == 39


def test_cli_warns_and_skips_the_reviews_contradictory_example(tmp_path, monkeypatch):
    src = make_flat(tmp_path.joinpath("in", *FOLDER), REVIEW_RELEASE, REVIEW_NAMES)
    lib = tmp_path / "lib"
    lib.mkdir()
    before = tree(tmp_path / "in")
    _code, out = run_cli(monkeypatch, make_release(REVIEW_RELEASE), src, "--dest", lib, "--release-id", RID, "--yes")
    assert "warning: cannot tell which disc" in out and "skipped: nothing written" in out
    assert tree(tmp_path / "in") == before and not any(lib.rglob("*"))
