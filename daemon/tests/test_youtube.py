"""Drives the YouTube library against stand-ins for ytmusicapi and yt-dlp, so neither nor the network is needed.
Run with ``python3 tests/test_youtube.py`` from ``daemon``.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from streamplay.backends import BackendError, create_backend, youtube
from streamplay.models import Track

FAILURES: list[str] = []

ART = "https://yt3.googleusercontent.com/abc=w120-h120-l90-rj"
RADIOHEAD = [{"name": "Radiohead", "id": "UCr"}]


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        FAILURES.append(label)


class FakeYTMusic:
    calls: list[tuple] = []

    def get_search_suggestions(self, query):
        return []

    def search(self, query, filter=None, limit=20):
        FakeYTMusic.calls.append(("search", query, filter))
        if filter == "songs":
            return [{"videoId": "v1", "title": "Myxomatosis", "artists": RADIOHEAD,
                     "album": {"name": "Hail to the Thief", "id": "MPREb_htt"},
                     "duration_seconds": 233, "thumbnails": [{"url": ART}]},
                    {"title": "no video id"}]
        if filter == "albums":
            return [{"browseId": "MPREb_htt", "title": "Hail to the Thief",
                     "artists": RADIOHEAD, "year": "2003", "thumbnails": [{"url": ART}]}]
        return [{"browseId": "UCr", "artist": "Radiohead", "thumbnails": [{"url": ART}]}]

    def get_artist(self, channel_id):
        return {"name": "Radiohead",
                "albums": {"browseId": "MPADUCr", "params": "p", "results": []},
                "singles": {"results": [{"browseId": "MPREb_single", "title": "Single"}]}}

    def get_artist_albums(self, browse_id, params, limit=100):
        FakeYTMusic.calls.append(("get_artist_albums", browse_id, params, limit))
        return [{"browseId": "MPREb_htt", "title": "Hail to the Thief", "year": "2003"}]

    def get_album(self, browse_id):
        return {"title": "Hail to the Thief", "year": "2003", "artists": RADIOHEAD,
                "thumbnails": [{"url": ART}],
                "tracks": [{"videoId": "v0", "title": "2 + 2 = 5", "album": "Hail to the Thief",
                            "trackNumber": 1, "duration_seconds": 200, "thumbnails": None},
                           {"videoId": "gone", "title": "Gone", "isAvailable": False}]}


class FakeYoutubeDL:
    resolved: list[str] = []

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    def extract_info(self, url, download=True):
        FakeYoutubeDL.resolved.append(url)
        if url.endswith("broken"):
            raise RuntimeError("Video unavailable")
        expire = int(time.time()) + 6 * 3600
        return {"url": f"https://rr1.googlevideo.com/videoplayback?expire={expire}&id={url[-2:]}"}


async def main() -> None:
    youtube.YTMusic, youtube.yt_dlp = None, None
    yt = create_backend({"id": "yt", "type": "youtube", "name": "YouTube"})
    try:
        await yt.connect()
        check("missing modules are reported", False)
    except BackendError as exc:
        check("missing modules are reported", "ytmusicapi" in str(exc) and "yt-dlp" in str(exc))

    youtube.YTMusic = FakeYTMusic
    youtube.yt_dlp = types.SimpleNamespace(YoutubeDL=FakeYoutubeDL)
    await yt.connect()
    check("nothing to browse", await yt.albums() == [] and await yt.artists() == [])

    found = await yt.search("radiohead")
    check("search asks for each kind",
          {c[2] for c in FakeYTMusic.calls if c[0] == "search"} == {"songs", "albums", "artists"})
    song = found["tracks"][0]
    check("results without a video are dropped", len(found["tracks"]) == 1)
    check("track fields", (song.id, song.title, song.artist, song.artist_id, song.album,
                           song.album_id, song.duration, song.source, song.backend)
          == ("v1", "Myxomatosis", "Radiohead", "UCr", "Hail to the Thief", "MPREb_htt",
              233.0, "yt", "youtube"))
    album = found["albums"][0]
    check("album fields", (album.id, album.name, album.artist, album.year, album.cover_id)
          == ("MPREb_htt", "Hail to the Thief", "Radiohead", 2003, ART))
    check("artist fields", (found["artists"][0].id, found["artists"][0].name) == ("UCr", "Radiohead"))

    albums = await yt.artist_albums("UCr")
    check("artist albums are fetched in full, singles as listed",
          [a.id for a in albums] == ["MPREb_htt", "MPREb_single"]
          and ("get_artist_albums", "MPADUCr", "p", None) in FakeYTMusic.calls)
    check("artist albums know their artist", albums[1].artist == "Radiohead"
          and albums[1].artist_id == "UCr")

    tracks = await yt.album_tracks("MPREb_htt")
    check("unavailable album tracks are dropped", [t.id for t in tracks] == ["v0"])
    check("album tracks take the album's details",
          (tracks[0].artist, tracks[0].album_id, tracks[0].track_no, tracks[0].year,
           tracks[0].cover_id) == ("Radiohead", "MPREb_htt", 1, 2003, ART))

    target = await yt.stream_target(song)
    check("stream resolves through yt-dlp",
          target.url.startswith("https://rr1.googlevideo.com/") and target.source == "yt"
          and FakeYoutubeDL.resolved == [youtube.WATCH_URL + "v1"])
    await yt.stream_target(song)
    check("a resolved stream is reused until it nears expiry", len(FakeYoutubeDL.resolved) == 1)
    await asyncio.gather(*(yt.stream_target(Track(id="v2", title="x", source="yt"))
                           for _ in range(2)))
    check("concurrent requests for one video share a resolve",
          FakeYoutubeDL.resolved.count(youtube.WATCH_URL + "v2") == 1)
    try:
        await yt.stream_target(Track(id="broken", title="x", source="yt"))
        check("a failed resolve is a BackendError", False)
    except BackendError:
        check("a failed resolve is a BackendError", True)

    url, params, headers = yt.cover_request(ART, 256)
    check("cover is resized by its URL",
          url == "https://yt3.googleusercontent.com/abc=w256-h256-l90-rj" and not params)
    check("a video thumbnail is fetched as is",
          yt.cover_request("https://i.ytimg.com/vi/x/hq.jpg", 256)[0] == "https://i.ytimg.com/vi/x/hq.jpg")
    check("covers come only from YouTube's image hosts",
          yt.cover_request("http://127.0.0.1:8080/x=y", 256) is None
          and yt.cover_request("https://evilgoogleusercontent.com/a=b", 256) is None)


if __name__ == "__main__":
    asyncio.run(main())
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        sys.exit(1)
    print("\nall checks passed")
