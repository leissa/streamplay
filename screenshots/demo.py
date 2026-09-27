"""The real daemon over an invented library, for store screenshots.

python3 demo.py PORT ART_DIR [OUTPUT]
"""

import asyncio
import io
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "daemon"))

from PIL import Image

from streamplay import backends, secretstore
from streamplay.backends.base import Backend, Sink, StreamTarget
from streamplay.config import Config
from streamplay.covers import CoverCache
from streamplay.hub import Hub
from streamplay.models import Album, Artist, Track
from streamplay.server import ControlServer

ART = pathlib.Path(sys.argv[2])


def dur(s):
    m, sec = s.split(":")
    return int(m) * 60 + int(sec)


# source, slug, album, artist, year, genre, tracks
LIBRARY = [
    ("subsonic", "night-drive-atlas", "Night Drive Atlas", "Neon Cartography", 2020, "Synthwave", [
        ("Ignition Sequence", "3:41"), ("Chrome Boulevard", "4:26"), ("Coastline at 2 AM", "5:12"),
        ("Tail Lights", "3:58"), ("Midnight Interchange", "4:47"), ("Radio Static Hearts", "4:05"),
        ("Overpass", "3:22"), ("Last Exit, First Light", "6:31")]),
    ("subsonic", "low-tide-radio", "Low Tide Radio", "Aurora Vale", 2021, "Dream Pop", [
        ("Seaglass", "4:12"), ("Paper Lanterns", "3:49"), ("Low Tide Radio", "5:03"),
        ("Undertow", "4:38"), ("Salt in Your Hair", "3:27"), ("Lighthouse Keeper", "5:55")]),
    ("subsonic", "quiet-machines", "Quiet Machines", "Mira Kessler", 2023, "Electronic", [
        ("Boot", "1:48"), ("Soft Circuits", "5:21"), ("Idle Loop", "4:44"),
        ("Cold Storage", "6:02"), ("Handshake", "4:10"), ("Sleep Mode", "7:15")]),
    ("subsonic", "afterglow", "Afterglow", "Velvet Static", 2022, "Shoegaze", [
        ("Bloomfield", "5:40"), ("Afterglow", "6:14"), ("Hazel", "4:02"),
        ("Tape Hiss Lullaby", "5:33"), ("Everything Is Loud", "7:08")]),
    ("subsonic", "concrete-bloom", "Concrete Bloom", "Lumen District", 2024, "Electronic", [
        ("Brutalist Spring", "4:31"), ("Rooftop Garden", "5:09"), ("Tram Lines", "3:56"),
        ("Neon Moss", "4:48"), ("Concrete Bloom", "6:20")]),
    ("subsonic", "signals-from-the-attic", "Signals from the Attic", "The Paper Satellites", 2018, "Indie Rock", [
        ("Antenna", "3:14"), ("Morse Code Kids", "3:42"), ("Dust on the Receiver", "4:19"),
        ("Attic Window", "3:51"), ("Broadcast", "4:36"), ("Static Summer", "3:28")]),
    ("kodi", "ember-and-ash", "Ember & Ash", "Hollow Pines", 2016, "Folk", [
        ("Kindling", "3:33"), ("Ember & Ash", "4:21"), ("The River Knows", "5:02"),
        ("Pine Needle Bed", "3:47"), ("Smoke Signals", "4:15"), ("Carry Me Home", "5:28")]),
    ("kodi", "blue-hour-sessions", "Blue Hour Sessions", "Juniper Oake Quartet", 2014, "Jazz", [
        ("Blue Hour", "7:12"), ("Streetlamp Serenade", "6:05"), ("Half Past Nothing", "8:31"),
        ("Rain on Lexington", "5:44"), ("Nightcap", "6:50")]),
    ("kodi", "copper-sky", "Copper Sky", "Kestrel & Stone", 2012, "Americana", [
        ("Dust Devil", "3:39"), ("Copper Sky", "4:27"), ("Mile Marker 49", "3:58"),
        ("Rattlesnake Waltz", "4:44"), ("Long Way to Marfa", "5:16")]),
    ("kodi", "songs-for-the-long-winter", "Songs for the Long Winter", "Orla Brennan", 2017, "Singer-Songwriter", [
        ("First Frost", "3:22"), ("Woodsmoke", "4:08"), ("Letters I Never Sent", "4:51"),
        ("Snowblind", "3:37"), ("Thaw", "5:09")]),
    ("mpd", "tidelines", "Tidelines", "Saltwater Choir", 2019, "Ambient", [
        ("Ebb", "8:12"), ("Littoral", "9:40"), ("Tidelines", "11:05"), ("Flood", "7:33")]),
    ("mpd", "orbit-songs", "Orbit Songs", "The Minor Planets", 2015, "Indie Pop", [
        ("Perihelion", "3:18"), ("Tiny Moons", "2:57"), ("Escape Velocity", "3:44"),
        ("Gravity Assist", "4:02"), ("Aphelion", "4:26")]),
]

PLAYLISTS = {
    "subsonic": [("pl-drive", "Late Night Drive", ["night-drive-atlas", "quiet-machines"]),
                  ("pl-focus", "Deep Focus", ["quiet-machines", "concrete-bloom"]),
                  ("pl-sunday", "Sunday Morning", ["low-tide-radio", "afterglow"])],
    "kodi": [("pl-dinner", "Dinner Party", ["blue-hour-sessions", "copper-sky"])],
    "mpd": [],
}


class DemoBackend(Backend):
    kind = ""

    async def connect(self):
        pass

    def _albums(self):
        return [a for a in LIBRARY if a[0] == self.source]

    def _album(self, row):
        _, slug, name, artist, year, genre, tracks = row
        return self.tag(Album(id=slug, name=name, artist=artist, artist_id=artist, year=year,
                              genre=genre, track_count=len(tracks), cover_id=slug,
                              duration=sum(dur(d) for _, d in tracks)))

    def _tracks(self, row):
        _, slug, name, artist, year, genre, tracks = row
        return [self.tag(Track(id=f"{slug}/{n}", title=title, artist=artist, album=name,
                               duration=dur(d), backend=self.kind, artist_id=artist,
                               album_id=slug, track_no=n, year=year, genre=genre,
                               cover_id=slug))
                for n, (title, d) in enumerate(tracks, 1)]

    async def artists(self):
        names = sorted({a[3] for a in self._albums()})
        return [self.tag(Artist(id=n, name=n, cover_id=next(a[1] for a in LIBRARY if a[3] == n),
                                album_count=sum(1 for a in self._albums() if a[3] == n)))
                for n in names]

    async def artist_albums(self, artist_id):
        return [self._album(a) for a in self._albums() if a[3] == artist_id]

    async def albums(self, sort="alphabetical", offset=0, limit=100):
        return [self._album(a) for a in self._albums()][offset:offset + limit]

    async def album_tracks(self, album_id):
        return next((self._tracks(a) for a in self._albums() if a[1] == album_id), [])

    async def genres(self):
        return sorted({a[5] for a in self._albums()})

    async def genre_albums(self, genre, offset=0, limit=100):
        return [self._album(a) for a in self._albums() if a[5] == genre]

    async def playlists(self):
        out = []
        for pid, name, slugs in PLAYLISTS[self.source]:
            tracks = await self.playlist_tracks(pid)
            out.append({"id": pid, "name": name, "source": self.source,
                        "trackCount": len(tracks), "duration": sum(t.duration for t in tracks),
                        "coverId": slugs[0]})
        return out

    async def playlist_tracks(self, playlist_id):
        for pid, _, slugs in PLAYLISTS[self.source]:
            if pid == playlist_id:
                return [t for s in slugs for t in (await self.album_tracks(s))[:3]]
        return []

    async def search(self, query, limit=40):
        q = query.lower()
        tracks = [t for a in self._albums() for t in self._tracks(a)
                  if q in t.title.lower() or q in t.artist.lower() or q in t.album.lower()]
        return {"artists": [a for a in await self.artists() if q in a.name.lower()],
                "albums": [self._album(a) for a in self._albums()
                           if q in a[2].lower() or q in a[3].lower()],
                "tracks": tracks[:limit]}

    async def stream_target(self, track):
        return StreamTarget(url="file:///dev/null", source=self.source)

    async def cover_bytes(self, cover_id, size):
        path = ART / f"{cover_id}.jpg"
        if not path.exists():
            return None
        im = Image.open(path)
        if size:
            im = im.resize((size, size), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92)
        return buf.getvalue()


class Subsonic(DemoBackend):
    kind = "subsonic"


class Kodi(DemoBackend):
    kind = "kodi"


class Mpd(DemoBackend):
    kind = "mpd"


class DemoSink(Sink):
    def __init__(self, sink_id, name, source=""):
        super().__init__()
        self.id, self.name, self.source = sink_id, name, source
        self.state.volume = 0.72

    async def play(self, target, track):
        self.state.status = "playing"
        self.state.duration = track.duration
        self.state.position = 0.0
        self._changed()

    async def resume(self):
        self.state.status = "playing"
        self._changed()

    async def pause(self):
        self.state.status = "paused"
        self._changed()

    async def stop(self):
        self.state.status = "stopped"
        self._changed()

    async def seek(self, position):
        self.state.position = position
        self._changed()

    async def set_volume(self, volume):
        self.state.volume = volume
        self._changed()


SINK_NAMES = {"kodi": "Living Room (Kodi)", "mpd": "Study (MPD)"}


def demo_sinks(backend):
    if backend.kind not in SINK_NAMES:
        return []
    if not hasattr(backend, "demo_sink"):
        backend.demo_sink = DemoSink(f"{backend.kind}:{backend.source}",
                                     SINK_NAMES[backend.kind], backend.source)
    return [backend.demo_sink]


async def main(port, output):
    backends.BACKEND_TYPES.update(subsonic=Subsonic, kodi=Kodi, mpd=Mpd)
    DemoBackend.sinks = demo_sinks
    secretstore.load_all = lambda: {}

    async def local_sink(self):
        self.sinks["local"] = DemoSink("local", "This computer")
    Hub._ensure_local_sink = local_sink

    tmp = ART.parent / "daemon"
    config = Config(tmp / "config.json")
    config.upsert({"id": "subsonic", "name": "Subsonic", "type": "subsonic", "enabled": True,
                   "url": "https://music.example.org", "username": "demo"})
    config.upsert({"id": "kodi", "name": "Living Room", "type": "kodi", "enabled": True,
                   "host": "livingroom.local", "port": 8080})
    config.upsert({"id": "mpd", "name": "Study", "type": "mpd", "enabled": True,
                   "host": "study.local", "port": 6600})
    config.settings["port"] = port
    config.settings["volume"] = 0.72

    hub = Hub(config, enable_mpris=False)
    hub.covers = CoverCache(tmp / "covers")
    server = ControlServer(hub, port=port)
    serving = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(0.3)
    await hub.start()

    player = hub.player
    await hub.set_output(output)
    nav, kodi = hub.sources["subsonic"], hub.sources["kodi"]
    await player.enqueue((await nav.album_tracks("night-drive-atlas"))[:5], mode="replace")
    await player.enqueue((await kodi.album_tracks("ember-and-ash"))[:3])
    await player.enqueue((await hub.sources["mpd"].album_tracks("orbit-songs"))[:2])
    await player.play_index(2)
    await player.seek(134.0)
    await player.set_repeat("all")
    print("ready", flush=True)
    await serving


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), sys.argv[3] if len(sys.argv) > 3 else "local"))
