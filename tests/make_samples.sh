#!/bin/sh
# Regenerate the tiny silent samples used by test_tags.py (needs ffmpeg).
set -e
cd "$(dirname "$0")/samples"
gen() { ext=$1; shift; ffmpeg -hide_banner -loglevel error -y -f lavfi -i "anullsrc=r=8000:cl=mono" -t 0.05 \
    -map_metadata -1 -fflags +bitexact -flags:a +bitexact "$@" "silent.$ext"; }
gen mp3 -c:a libmp3lame -b:a 32k
gen flac -c:a flac
gen m4a -c:a aac -b:a 32k
gen wav -c:a pcm_s16le
gen aiff -c:a pcm_s16be
gen wma -c:a wmav2 -b:a 32k
gen ogg -c:a libvorbis
gen opus -c:a libopus -b:a 16k
gen wv -c:a wavpack
