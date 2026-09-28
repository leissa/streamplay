# Changelog

All notable changes to Streamplay. Each version is tagged and
[released on GitHub](https://github.com/leissa/streamplay/releases) with a
`.plasmoid` package attached.

## [1.4.0](https://github.com/leissa/streamplay/releases/tag/v1.4) — 2026-09-29

### Changed

- The library shows a spinner and dims the list while a search or listing takes
  longer than a moment. A fast reply no longer flashes it.

### Fixed

- Switching the output while playing starts the track on the new output where
  it left off, without first playing a moment from the beginning.
- A slow library reply can no longer overwrite the results of a newer search.

## [1.3.0](https://github.com/leissa/streamplay/releases/tag/v1.3) — 2026-09-27

### Added

- The widget can be driven from the keyboard. <kbd>Ctrl</kbd>+<kbd>1</kbd>/<kbd>2</kbd>/<kbd>3</kbd>
  switch tabs and <kbd>Ctrl</kbd>+<kbd>H</kbd>/<kbd>L</kbd> step between them.
  <kbd>Ctrl</kbd>+<kbd>F</kbd> jumps to the library search from any tab.
  <kbd>↓</kbd>/<kbd>↑</kbd>, <kbd>Ctrl</kbd>+<kbd>N</kbd>/<kbd>P</kbd> and
  <kbd>Ctrl</kbd>+<kbd>J</kbd>/<kbd>K</kbd> move from the search field into the
  results and through the library. <kbd>Enter</kbd> opens or plays,
  <kbd>Ctrl</kbd>+<kbd>Enter</kbd> plays now, <kbd>Shift</kbd>+<kbd>Enter</kbd>
  queues and <kbd>Backspace</kbd> goes back. The README lists them all.
- Tooltips on the tabs, the *Back* button and the row actions name their shortcuts.
- A *Back to the start* button once you are more than one level deep in the library.

### Changed

- A new search replaces the previous one and everything opened from it, rather
  than piling up on the back stack. Clearing the field returns to the start.

## [1.2.0](https://github.com/leissa/streamplay/releases/tag/v1.2) — 2026-09-27

### Added

- **Jellyfin** as a library: base URL, username and password. The widget logs in
  as its own device and appears under *Dashboard → Devices*, and playback is
  reported to `Sessions/Playing`, so Jellyfin counts plays and scrobbler plugins
  submit them.
- **Emby** as a library, with Emby's auth headers and the `/emby` path prefix.
- **Plex** as a library: server address and an `X-Plex-Token`. Every music
  library on the server is merged into one, and streams are the original files —
  the transcoder is not used.
- **Lyrion Music Server** (formerly Logitech Media Server / Squeezebox Server)
  as a library, and every one of its players — Squeezebox, piCorePlayer,
  Squeezelite — as its own output. Players that connect later show up within
  about ten seconds.
- **UPnP / DLNA renderers** as outputs, with no configuration at all: Sonos
  speakers, TVs, AV receivers, gmrender and upmpdcli are discovered on the local
  network. Discovery repeats every minute, and a renderer is dropped only after
  several missed rounds so a lost packet cannot pull an output mid-track. One
  that multicast cannot reach can be added by its device-description URL. They
  play anything streamed over HTTP, but not MPD's local files.
- A test per service, run against a scripted server (`test_jellyfin.py`,
  `test_emby.py`, `test_plex.py`, `test_lyrion.py`, `test_upnp.py`), and GitHub
  Actions running the whole suite on every push.

### Changed

- The Subsonic backend is named for the API rather than for one server, since it
  covers Airsonic-Advanced, Ampache, Funkwhale, Gonic, LMS, Navidrome and
  Subsonic itself.
- The settings dialog has a single *Add Music Server…* entry that asks which
  kind of server, instead of one button per service.

### Fixed

- The widget picker showed a generic plasmoid icon for store installs.
  `metadata.json` now points at the logo bundled in the package, so neither
  `install.sh` nor the applet's service script has to write into the icon theme.
- Lyrion has no play history, favourites or descending year sort, so those album
  sorts fall back to alphabetical instead of failing.

## [1.1.0](https://github.com/leissa/streamplay/releases/tag/v1.1) — 2026-09-26

### Added

- `Sink.plays(track)` says whether an output can play a track from a given
  service at all, and `Hub.unavailable(track, sink)` folds that together with
  "the service is not connected" into a single reason. The queue pushed to the
  applet marks such entries with it, which the widget shows as a tooltip, and
  the player steps over them rather than failing, giving up only once the whole
  queue has been tried.
- A watchdog under the remote outputs: a track that has not started after ten
  seconds is treated as an error, so an output that accepts a track and then
  reports nothing playing can no longer park the queue for ever.

### Fixed

- Kodi hung between tracks. `KodiSink` marks its own stops so they are not
  mistaken for end-of-file, but opening a file on an idle Kodi sends no
  `Player.OnStop` to consume that mark, and the stale mark then swallowed the
  next real end-of-file. The flag is cleared when playback starts.
- The queue is re-pushed when a service connects or disconnects and when the
  output changes, since both change what is playable.

## [1.0.0](https://github.com/leissa/streamplay/releases/tag/v1.0) — 2026-09-25

First release: a Plasma 6 widget for self-hosted music libraries, split into a
Python user service that owns the queue, the playback and the MPRIS2
registration, and a pure-QML applet that is a view onto it. Because the service
is separate, music keeps playing across a plasmashell restart.

- Libraries: Navidrome / Subsonic-compatible servers, authenticated with a
  salted MD5 token rather than a plaintext password; Kodi, over the HTTP
  interface and the event port; MPD, over its control port. Several can be
  connected at once, browsing merges them into one library with a badge saying
  where each row came from, and a filter narrows it back to a single service.
- Outputs: this computer's speakers via mpv, with gapless handover between
  tracks; Kodi; MPD. Switching output mid-track carries the position over.
- One shared queue that neither the library nor the output owns, so a Subsonic
  album and a Kodi album can sit next to each other and play one after the
  other, on whichever output is selected.
- Play, pause, stop, next, previous, seek, volume, shuffle and three repeat
  modes; enqueue, play next, replace, remove, drag to reorder, clear and jump to
  an entry; browsing by album, artist, genre or server-side playlist, and search
  across every connected service at once.
- MPRIS2 over D-Bus, so the media keys, Now Playing in the system tray and the
  volume OSD work with no extra setup.
- Passwords in the freedesktop Secret Service, never in the config file and
  never in the applet.
