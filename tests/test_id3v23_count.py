"""status_counts().id3v23: library-wide count of tracks written as ID3v2.3 under the current settings."""
import pytest

from music_manager import service
from music_manager.service import AlbumFilter, MatchStatus


@pytest.mark.xfail(strict=True, reason="status_counts().id3v23 not built yet")
def test_status_counts_carries_a_library_wide_id3v23_count(tmp_path):
    from service_fixtures import Library, make_album

    lib = Library(tmp_path)
    make_album(lib.root / "WAV Artist - Waves", ["01 A", "02 B"], ext="wav")
    make_album(lib.root / "AIFF Artist - Aifs", ["01 A"], ext="aiff")
    make_album(lib.root / "MP3 Artist - Mps", ["01 A", "02 B"], ext="mp3")
    lib.start(tmp_path)
    try:
        counts = service.library.status_counts()
        assert set(counts) == set(MatchStatus)  # still a plain status -> count mapping
        assert counts.id3v23 == 3  # WAV/AIFF always
        assert service.library.status_counts(AlbumFilter(text="Side Story")).id3v23 == 3  # library-wide
        service.settings.set(id3v23_mp3=True)
        assert service.library.status_counts().id3v23 == 5
    finally:
        service.reset()

