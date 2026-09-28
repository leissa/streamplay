# CLAUDE.md

A Plasma 6 widget for self-hosted music libraries (Subsonic, Jellyfin, Emby,
Plex, Kodi, MPD, Lyrion) and UPnP renderers: a Python user service (`daemon/`) plus a pure-QML applet
(`plasmoid/`). The split is forced — MPRIS2 and audio playback cannot be driven
from QML — and it keeps music playing across a plasmashell restart.
`README.md` is the user-facing description.

## Commands

```sh
cd daemon
python3 tests/test_player.py      # queue, shuffle, repeat, output switching, real mpv
python3 tests/test_protocol.py    # control protocol, two services connected
python3 tests/test_mpd.py         # MPD library + output against tests/fake_mpd.py
python3 tests/test_kodi.py        # Kodi output against scripted notifications
python3 tests/test_jellyfin.py    # Jellyfin library against a scripted HTTP server
python3 tests/test_emby.py        # likewise Emby, test_plex.py, test_lyrion.py, test_upnp.py
python3 tests/test_lyrics.py      # LRCLIB and lyrics.ovh lookups against a scripted HTTP server

systemctl --user stop streamplay                  # before running by hand
PYTHONPATH=daemon python3 -m streamplay -vv       # also --port --host --no-mpris --config

./install.sh [uninstall]
./package.sh                      # build/streamplay-<version>.plasmoid for the KDE Store
./screenshots/shoot.sh [name...]  # build/screenshots: store shots over an invented library
kpackagetool6 --type Plasma/Applet --upgrade plasmoid/package   # applet only
```

Tests are plain scripts, not pytest, and stub every service, so no music server
is needed. To run a single check, `return` early. There is no build step, no
linter and no test runner.

## Verifying QML

`qmllint` silently skips the Plasma/Kirigami modules and reports nothing, even
for types that do not exist. Plasma logs QML errors to the journal, and
`console.log` never reaches the terminal. The working loop:

```sh
kpackagetool6 --type Plasma/Applet --upgrade plasmoid/package
STAMP=$(date '+%Y-%m-%d %H:%M:%S'); sleep 1
QT_QPA_PLATFORM=offscreen timeout 12 plasmoidviewer -a io.github.leissa.streamplay -f planar >/dev/null 2>&1
journalctl --user --since "$STAMP" --no-pager | grep streamplay
```

Empty output means clean. One bad type name kills the whole applet through a
cascade of "Type X unavailable", so run this after every QML change.

Config pages load only when the settings dialog opens, so that loop misses
them. Two things to check by hand:

- `ConfigCategory.source` in `contents/config/config.qml` resolves against
  `contents/ui/`, so our pages are `config/Foo.qml`. Getting it wrong gives
  categories with empty content and no error anywhere. Verify with
  `test -f contents/ui/$source`.
- Run `qml6` on the page and read the journal. Copy it to a scratch directory
  first and give the copy `i18n`/`i18nc`/`i18np` stubs: a failed `i18n` leaves
  properties undefined, so every reader reports `TypeError: Cannot read
  property 'x' of undefined` and looks like a real fault. The copy also lets a
  probe call the page's own functions and print with `console.warn`, which does
  reach the journal.

Plasmashell caches the package per applet instance, so after touching
`config/` the settings dialog has to be reopened, sometimes plasmashell
restarted.

`SearchField`, `Heading`, `DescriptiveLabel`, `PlaceholderMessage` and
`ListSectionHeader` are in `org.kde.plasma.extras`; `TabBar`, `ComboBox`,
`ScrollView`, `ItemDelegate` and friends in `org.kde.plasma.components`. Check
a module's `qmldir` before using a type not already used here.

## Architecture

### Libraries, outputs, one queue

- `backends/base.py: Backend` — a library you browse, yielding a `StreamTarget`,
  plus the outputs the service offers (`sinks()`).
- `backends/base.py: Sink` — somewhere audio comes out. Plays one target,
  reports eof, knows nothing about queues.
- `player.py: UnifiedPlayer` — owns the only queue, the play order, shuffle and
  repeat, and drives the selected sink.
- `hub.py: Hub` — holds every connected backend and sink and resolves for the
  player: `stream_target(track)`, `scrobble(track, submission)`,
  `unavailable(track, sink)`. The player never imports a backend.

Neither side owns the queue, which is what lets a Subsonic album and a Kodi
album share one and play through either destination. Do not move queue state
into a backend or a sink.

`Sink.plays(track)` says whether an output can play a track from that service
at all; Kodi and MPD accept only their own library. A sink with
`web_streams_only` (Lyrion, UPnP) refuses another service's tracks unless its
backend has `web_streams`, which MPD's `file://` URLs are not. `Hub.unavailable(track,
sink)` folds that together with "the service is not connected" into the one
reason the player skips the entry over (`_step_over`) and `queue()` hands the
applet as `unavailable`. `UnifiedPlayer._watch_start` is the net underneath: an
output that takes a track and then reports nothing playing would otherwise park
the queue on it for ever.

A service's outputs come from `Backend.sinks()`: one for Kodi and MPD, one
per player for Lyrion, one per renderer for UPnP. A backend whose set changes
calls `sinks_changed()`, and `Hub._sync_sinks` diffs by id *and identity*, so
`sinks()` must return the same object for the same device. Every sink is handed
one track at a time. To add a service: `BACKEND_TYPES` in
`backends/__init__.py`, `sinks()`, `Sink.plays` and `Sink.source` — that last one is how `Hub._drop_source` tears the outputs down
with the service without knowing any type names. UPnP sets `has_library =
False`, which keeps it out of `Hub.libraries()`, and the applet's
`Client.libraries`, via `hasLibrary` in `sources`.

### Gapless handover

`UnifiedPlayer._preload` hands the sink `Sink.preload(next)` whenever what comes
next changes. `MpvSink` appends it to mpv's playlist, so mpv moves on by itself
with `--prefetch-playlist`, and the player's following `play` of that queue uid
is adopted rather than reloaded (`_handed_over`). Match on `uid`, not URL:
Subsonic salts every stream URL. The remote outputs ignore the hint.

### Telling eof from a user's stop

No remote service reports this well.

- Kodi: `Player.OnStop` with `end: true` is eof; `KodiSink._expect_stop` marks
  our own stops.
- MPD, Lyrion and UPnP report a bare stop either way, so their sinks derive
  from `PolledSink`. `_near_end()` judges by position, carried forward from the
  last reading by wall clock (`_note_position`), because a short track can
  start *and* end between two polls. `_transition()` covers the inverse case:
  replacing the track takes the service through stop.
- Lyrion counts a stop while `waitingToPlay` as still playing; UPnP treats a
  `STOPPED` before the first `PLAYING`, within `START_GRACE` of a play, as
  buffering.

### Jellyfin specifics

- `connect()` logs in with `AuthenticateByName` and `close()` logs out.
  Jellyfin revokes any earlier token of the same device id, so each
  `JellyfinBackend` makes its own; otherwise *Test Connection* would log the
  live connection out.
- Scrobbling is `Sessions/Playing` and `Sessions/Playing/Stopped` at the full
  duration, which is what makes Jellyfin count a play and scrobbler plugins
  submit it.

### Emby, Plex, Lyrion, UPnP specifics

- `EmbyBackend` is `JellyfinBackend` with Emby's auth headers
  (`_auth_headers`) and the `/emby` path prefix.
- Plex keeps its token in `password`, the only secret field. Genres filter by a
  tag id that differs per section (`_genre_ids`), and `_paged` pages across
  several music sections as one list.
- Lyrion has no play history, favourites or descending year sort, so those
  album sorts fall back to alphabetical.
- UPnP re-discovers every 60 s and drops a renderer only after
  `MISSES_BEFORE_DROP` missed rounds, so one lost packet does not pull an
  output mid-track. Its DIDL-Lite is escaped twice on purpose, as XML and again
  as a SOAP argument; Sonos and others refuse a URI without it.

### MPD specifics

- It serves no audio, so `MpdBackend._file_url()` builds a `file://` URL from
  the profile's `musicDirectory`. Without it `stream_target` returns `native`
  but no `url`, and MPD tracks play only on MPD.
- No ids: a tag value is the id. Track = path, artist = name, album =
  `albumartist + "\x1f" + album`.
- `list ... group date` splits an album whose tracks disagree about the date;
  `_album_list` folds them back together.
- `list`/`find` match case-sensitively and `search` does not, so
  `MpdBackend.search` filters artists and albums in Python.

### Sources and ids

Every `Track`, `Album`, `Artist` and playlist carries `source`, the profile id
it came from. Library ids are unique only within one service, so any call
taking an id needs a source too. `library.*` without a `source` fans out across
the connected services and merges; with one, it targets that service.

### Threads and events

- One asyncio loop for everything except MPRIS.
- MPRIS has its own GLib thread (`mpris.py`) because dbus-python needs a GLib
  main loop. In: `MprisService.dispatch()` → `loop.call_soon_threadsafe`. Out:
  `push_state`/`push_seeked` → `GLib.idle_add`. Never touch the D-Bus object
  from asyncio or the player from GLib.
- mpv events go on an `asyncio.Queue` for a separate task and are never handled
  inside the socket reader: an eof handler issues a new command and would wait
  for a reply only the blocked reader could deliver. Real bug; keep the queue.
- `Mpv._drop_ipc()` writes the connection off the moment the socket closes.
  Otherwise a command goes into a dead socket and waits out its ten-second
  timeout, and systemd kills mpv and the daemon together so shutdown always
  hits this — it cost a SIGKILL on every restart. `tests/test_player.py` guards
  it.
- mpv reports position many times a second. `UnifiedPlayer._on_sink_changed`
  emits a rate-limited `position` event for progress and a full `state` push
  otherwise; `_changed()` is for genuine transitions.

### Applet ↔ daemon

One WebSocket on `127.0.0.1:8760`: `{"id", "method", "params"}` →
`{"id", "ok", "result"|"error"}`, plus pushed `state`, `position`, `queue`,
`sources`, `profiles` and `seeked`. The same port serves cover art over plain
HTTP at `/cover?src=…&id=…&size=…` through `websockets`' `process_request`
hook, so QML's `Image` can load artwork and the applet holds no credentials.

`lyrics.get` takes title, artist, album and duration rather than a track id,
because the lookup services know nothing of our ids. `LyricsCache` stores hits
and misses on disk and asks again about a miss after `MISS_TTL`; a failed
request is not stored. `LyricsPane` asks only while it is visible, so the
services hear only about tracks the user wanted lyrics for.

Cover art is normally `Backend.cover_request()`, an HTTP URL `CoverCache`
fetches in a thread. MPD sends images down the control connection instead, so
it implements `Backend.cover_bytes()`, which the cache tries first and runs on
the loop.

`Client.qml` is the whole transport.

- `_adopt()` compares before assigning `sources`/`outputs`. The daemon repeats
  them in every state push, and assigning to a `property var` fires a change
  signal every time, which made `LibraryPane` reload several times a second.
  Keep the comparison.
- `displayPosition` interpolates between the daemon's ~1 Hz updates;
  `scrubbing` suppresses it while the seek bar is dragged.

The panes reference `root.client` and `root.track` across files because QML
resolves ids through the *creation context* and every pane is instantiated from
`main.qml`. Config pages are a separate context, so `ConfigServers.qml` creates
its own `Client` (`import ".." as Sp`) and reads the daemon host and port from
`cfg_*` properties Plasma fills in.

### Store package

`package.sh` copies `daemon/streamplay` into `contents/code/`, next to
`service.sh`. `Service.qml` runs that script through the `executable` data
engine, and the script starts the daemon with `systemd-run` as the transient
unit `streamplay-applet`, so it outlives plasmashell. The script exits 3
without a bundled daemon (a dev install) and 2 with dependencies missing,
printing them from `streamplay.check`. The daemon reports `version` and `home`
in `hello`. `Service.qml` restarts a daemon that runs from its own `code`
directory at another version, which is why `package.sh` refuses mismatched
versions.

### Icons

`metadata.json` sets `"Icon": "/icons/streamplay.svg"`, a path relative to
`plasmoid/package/contents/`, not an icon-theme name. Plasma's widget picker
resolves an `Icon` starting with `/` inside the package, so the bundled logo
also shows for a store install, which never touches the icon theme. Keep the
leading slash; a bare name or `./foo.svg` falls back to a theme lookup and
yields the generic plasmoid icon. `plasmoidviewer`'s own *About* page does not
resolve this form, which is a plasmoidviewer artifact, not a packaging bug.

## Configuration and secrets

`~/.config/streamplay/config.json`, mode 0600, holds everything but the
passwords, which `secretstore.py` keeps in the freedesktop Secret Service
(`org.freedesktop.secrets`, not the KWallet API) under the attributes
`application=streamplay`, `profile`, `field`.

- `Hub.start()` merges them into the in-memory profiles, so backends still
  read `profile["password"]`; `Config.save()` never writes a secret field.
- A provider like KeePassXC is not D-Bus activatable and often starts after the
  daemon, so `Hub._retry_secrets` polls until it appears, then connects.
- Reading runs in a thread because an unlock prompt blocks until answered.
- `Config.upsert()` treats a missing or empty `password` as "keep the stored
  one", so the applet can save an edited profile without ever holding the
  secret.
- `Profile.redacted()` strips `password` for a `hasPassword` boolean. Never
  send a raw profile to the applet.
- `Hub._schedule_settings_flush` delays writing volume, shuffle and repeat so a
  volume drag does not thrash the file.

## Comments

Comments are scarce. The default is **no comment**.

Comment only when the code itself cannot reasonably express the information.

- Comment **why**, not what the code does.
- Prefer a better name, structure, or API over a comment.
- Keep comments to **one short sentence**, normally one line.
- When a comment spans multiple lines, use **one complete sentence per line**. Do not wrap a single sentence across multiple lines merely to fit a line-length limit.
- A comment should convey one fact only: an invariant, non-obvious constraint, algorithmic reason, or important external reference.
- Do not explain the implementation, summarize a function, or provide a narrative of its control flow.
- Match the comment density and brevity of the surrounding code. **Never increase comment density.**
- Do not add documentation-style prose, introductions, conclusions, or motivational/explanatory language.
- Do not use rhetorical contrasts such as `"X" -> "Y"`, `"instead of X"`, or `"from X to Y"` to explain an optimization.
- Do not add comments describing the change itself ("now handles X", "renamed from Y"); that belongs in the commit message.
- Do not add banner or section-header comments.
- Do not add a comment if deleting it would leave the code equally correct and understandable.
- A comment that restates the code is worse than no comment:
  ```cpp
  vec.push_back(x); // BAD: "put x into the vector"
  ```

**Hard limit:** Do not write multi-line comments unless the user explicitly asks for documentation or the comment is required to document a non-obvious invariant that cannot be stated briefly.

Before adding a comment, ask:
1. Is this information necessary?
2. Is it already apparent from the code or names?
3. Can it be expressed in one short sentence?
If the answer to 1 or 3 is no, do not add the comment.
