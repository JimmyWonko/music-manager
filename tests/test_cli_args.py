"""discogs-tag CLI hygiene from the 2026-10-21 review: argument validation, `is not None` release ids,
--debug tracebacks, an AlbumResult enum and one function per mode. Album-tagging behaviour is unchanged."""
import shutil

import pytest
from conftest import SAMPLES

from music_manager.cli import discogs_tag as cli


@pytest.mark.parametrize("argv, needle", [
    (["--limit", "-1", "album"], "--limit"),
    (["--release-id", "0", "album"], "--release-id"),
    (["--release-id", "-5", "album"], "--release-id"),
    (["--self-test", "--save-token"], "not allowed with"),
    (["--normalize-folders", "--check-disc-swaps", "album"], "not allowed with"),
    (["--undo-journal", "j.jsonl", "--normalize-folders"], "not allowed with"),
    (["--report", "report.txt", "album"], "--report"),
])
def test_bad_arguments_are_clean_argparse_errors(argv, needle, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.parse_args(argv)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "usage: discogs-tag" in err and needle in err and "Traceback" not in err


def test_valid_arguments_still_parse():
    assert cli.parse_args(["--limit", "0", "a"]).limit == 0
    assert cli.parse_args(["--release-id", "1", "a"]).release_id == 1
    assert cli.parse_args(["--check-disc-swaps", "--report", "r.txt", "a"]).report.name == "r.txt"
    assert cli.parse_args(["--debug", "a"]).debug is True
    assert cli.parse_args(["a"]).debug is False


def test_release_id_checks_use_is_not_none():
    calls = []

    class Api:
        def release(self, rid):
            calls.append(rid)
            return {"id": rid}

    assert cli.choose_release(Api(), {}, [], auto=True, release_id=0) == {"id": 0}
    assert calls == [0]


def test_album_result_enum_keeps_the_old_strings():
    assert [r.value for r in cli.AlbumResult] == ["applied", "skipped", "skipped-tagged"]
    assert cli.AlbumResult.SKIPPED_TAGGED == "skipped-tagged"


def _one_album(tmp_path):
    album = tmp_path / "in" / "Testers - Side Story"
    album.mkdir(parents=True)
    shutil.copyfile(SAMPLES / "silent.flac", album / "01 a.flac")
    return album


@pytest.mark.parametrize("debug", [False, True])
def test_debug_shows_the_traceback_and_the_batch_still_finishes(tmp_path, monkeypatch, capsys, debug):
    album = _one_album(tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cli, "Discogs", lambda *a: object())
    monkeypatch.setattr(cli, "load_token", lambda: ("t", None, None))
    monkeypatch.setattr(cli, "process_album", boom)
    code = cli.main([str(album), "--dest", str(tmp_path / "lib"), *(["--debug"] if debug else [])])
    out, err = capsys.readouterr()
    assert code == 1 and "failed=1" in out and "error: " in err and "kaboom" in err
    assert ("Traceback (most recent call last)" in err) is debug


def test_process_album_returns_album_results(tmp_path, capsys):
    album = _one_album(tmp_path)
    from mutagen import File as MutagenFile

    from music_manager.fs import collect_albums

    audio = MutagenFile(album / "01 a.flac")
    audio.tags["DISCOGS_RELEASE_ID"] = ["5"]
    audio.save()
    [folder] = collect_albums(album)
    result = cli.process_album(None, folder, tmp_path / "lib", dry_run=True, auto=True, yes=False, skip_tagged=True,
                               release_id=None, clear_other=False, scan_root=album)
    assert result is cli.AlbumResult.SKIPPED_TAGGED


@pytest.mark.parametrize("argv, mode", [
    (["--save-token"], "run_save_token"),
    (["--undo-journal", "j.jsonl"], "run_undo"),
    (["--check-disc-swaps", "x"], "run_disc_swap_check"),
    (["--normalize-folders", "x"], "run_normalize"),
    (["x"], "run_tagging"),
])
def test_main_dispatches_one_function_per_mode(argv, mode, monkeypatch):
    names = ("run_save_token", "run_undo", "run_disc_swap_check", "run_normalize", "run_tagging")
    for name in names:
        monkeypatch.setattr(cli, name, lambda *a, _n=name: 40 + names.index(_n))
    assert cli.main(argv) == 40 + names.index(mode)
