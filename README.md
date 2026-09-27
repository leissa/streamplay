# <img src="plasmoid/package/contents/icons/streamplay.svg" width="48" align="top"> Streamplay

[![tests](https://img.shields.io/github/actions/workflow/status/leissa/streamplay/tests.yml?branch=master&style=flat-square&logo=github&label=tests)](https://github.com/leissa/streamplay/actions/workflows/tests.yml)

A Plasma 6 widget for self-hosted music libraries. It connects to
**Subsonic**-compatible servers, **Jellyfin**, **Emby**, **Plex**, **Kodi**,
**MPD** and **Lyrion Music Server** — several of them at the same time — and
puts everything into a single shared queue that can be played on this
computer, on Kodi, MPD or a Squeezebox player, or on any UPnP/DLNA renderer on
the network. It registers itself with KDE as an MPRIS2 player, so Now Playing,
the media keys and the lock screen all control it.

## What it does

- **Several services at once.** Each server has its own on/off switch; browsing
  merges the connected ones into one library, with a badge on every row saying
  where it came from. A filter narrows it back to a single service.
- **One queue for all of them.** A Subsonic album and a Kodi album can sit next
  to each other in the same queue and play one after the other.
- **Pick where it plays.** The queue can go to this computer's speakers (via
  mpv), to Kodi, to MPD, to any player of a Lyrion Music Server, or to a UPnP /
  DLNA renderer such as a Sonos speaker, a TV or an AV receiver. Switching
  mid-track carries the position over.
- **The usual transport.** Play, pause, stop, next, previous, seek, rewind by
  dragging the progress bar, volume, shuffle and three repeat modes.
- **Queue editing.** Enqueue, play next, replace, remove, drag to reorder,
  clear, and jump to any entry.
- **Browsing.** By album, artist, genre or server-side playlist, plus search
  across every connected service at once.
- **KDE integration.** MPRIS2 means media keys, Now Playing in the system tray,
  and the volume OSD all work without any extra setup.

## Keyboard

With the popup open:

| Key | Action |
| --- | --- |
| <kbd>Ctrl</kbd>+<kbd>1</kbd> / <kbd>2</kbd> / <kbd>3</kbd> | Playing / Queue / Library tab |
| <kbd>Ctrl</kbd>+<kbd>H</kbd> / <kbd>Ctrl</kbd>+<kbd>L</kbd> | previous / next tab, also while typing in the search field |
| <kbd>Ctrl</kbd>+<kbd>F</kbd> | search the library, from any tab |
| <kbd>↓</kbd> / <kbd>↑</kbd>, <kbd>Ctrl</kbd>+<kbd>N</kbd> / <kbd>P</kbd>, <kbd>Ctrl</kbd>+<kbd>J</kbd> / <kbd>K</kbd> | move through the library; from the search field into the results and back |
| <kbd>Enter</kbd> | open the artist, album, genre or playlist; play a track |
| <kbd>Ctrl</kbd>+<kbd>Enter</kbd> | play now, replacing the queue |
| <kbd>Shift</kbd>+<kbd>Enter</kbd> | add to the queue |
| <kbd>Backspace</kbd> | back |

## How it is put together

MPRIS and audio playback cannot be done from QML alone, so the work is split in
two. A small user service owns everything stateful; the widget is a view onto
it. That also means music keeps playing if plasmashell is restarted.

```
┌─────────────────────────────┐
│  Plasma applet (pure QML)   │   panel icon, popup, settings pages
└──────────────┬──────────────┘
               │  WebSocket + HTTP on 127.0.0.1:8760
┌──────────────▼──────────────┐
│  streamplay (Python)        │
│  ┌───────────────────────┐  │
│  │ one queue             │  │   order, shuffle, repeat, current track
│  ├───────────┬───────────┤  │
│  │ libraries │  outputs  │  │
│  │ Subsonic  │  mpv      │  │   any library can play on any output
│  │ Jellyfin  │  Kodi     │  │
│  │ Emby      │  MPD      │  │
│  │ Plex      │  Lyrion   │  │   one output per player
│  │ Kodi      │  UPnP     │  │   one output per renderer
│  │ MPD       │           │  │
│  │ Lyrion    │           │  │
│  └───────────┴───────────┘  │
│  MPRIS2 ──────> D-Bus       │
└─────────────────────────────┘
```

A *library* is something you browse; an *output* is somewhere audio comes out.
Kodi, MPD and Lyrion are both; UPnP renderers are only outputs. Because neither
side owns the queue, tracks from one service can play through another: Kodi
songs are streamed locally over Kodi's own HTTP server, and every other library
hands out stream URLs that any output can open.

MPD is the one asymmetric case. It serves no audio over its control port, so
playing its music anywhere other than on MPD itself needs the files to be
readable from this computer as well — see the music folder setting below.

## Requirements

Everything below is packaged on Arch and most other distributions:

| Needed for | Package |
| --- | --- |
| the service | `python`, `python-requests`, `python-websockets`, `python-secretstorage` |
| local playback | `mpv` |
| MPRIS (Now Playing, media keys) | `python-dbus`, `python-gobject` |
| the widget | `plasma-workspace` (Plasma 6) |

## Install

```sh
./install.sh
```

This copies the service to `~/.local/share/streamplay`, adds a
`~/.local/bin/streamplayd` launcher, enables the `streamplay` systemd user
service, and installs the widget. Nothing is written outside `$HOME`.

Then add the **Streamplay** widget to a panel or the desktop, open its
settings and add your servers.

To remove everything again (your servers and settings are kept):

```sh
./install.sh uninstall
```

### From the KDE Store

`./package.sh` builds `build/streamplay-<version>.plasmoid`, which is the
widget with the service bundled inside. After installing it through *Get New
Widgets*, the widget offers **Start Service**. That runs the bundled service
as the transient systemd user unit `streamplay-applet`, and from then on the
widget starts it whenever it finds it not running. The requirements above
still have to come from the distribution; the widget lists any that are
missing. After a store update the widget restarts the service if it is still
running the old version.

## Adding servers

Open the widget's settings → **Music Servers** → *Add Music Server…* and pick
the kind of server.

- **Subsonic** covers every server speaking the Subsonic API:
  [Airsonic-Advanced](https://github.com/airsonic-advanced/airsonic-advanced),
  [Ampache](https://ampache.org), [Funkwhale](https://funkwhale.audio),
  [Gonic](https://github.com/sentriz/gonic),
  [LMS (Lightweight Music Server)](https://github.com/epoupon/lms),
  [Navidrome](https://www.navidrome.org) and Subsonic itself. It needs the base
  URL (`https://music.example.org`, not the `/rest` path), a username and a
  password. The password is never sent in the clear: each request carries a
  salted MD5 token instead. Very old servers that do not understand this can be
  switched to the legacy format.
- **Jellyfin** needs the base URL (`https://jellyfin.example.org`, including any
  base path such as `/jellyfin`), a username and a password. The widget logs in
  as its own device and shows up under *Dashboard → Devices*.
- **Emby** needs the server address (`https://emby.example.org` or
  `http://host:8096`; the `/emby` API path is added automatically), a username
  and a password. Like Jellyfin, the widget logs in as its own device.
- **Plex** needs the server address (`http://192.168.1.20:32400`; the port
  defaults to 32400) and an access token: in Plex Web, open any item, choose
  *Get Info → View XML* and copy the `X-Plex-Token` value from the address bar.
  Every music library on the server is merged into one. Streams are the
  original files; Plex's transcoder is not used.
- **Kodi** needs the host, the web interface port (8080 by default) and the
  event port (9090). In Kodi, turn on *Settings → Services → Control → Allow
  remote control via HTTP* and *Allow remote control from applications on other
  systems*. A username and password are optional but recommended.
- **MPD** needs the host and port (6600 by default), and a password only if
  `password` is set in `mpd.conf`. MPD 0.21 or newer is required.

  The **music folder** is optional but worth filling in. MPD hands out no audio
  itself, so without it MPD's music can only be played on MPD; with it — the
  same path as `music_directory` in `mpd.conf`, as this computer sees it — its
  tracks play on the local speakers and on Kodi as well. For an MPD on another
  machine that means the library has to be mounted here, at whatever path you
  enter.

  If MPD listens on a unix socket, put its path under `socket` in
  `config.json`; the settings dialog has no field for it. That is worth doing
  for a local MPD, because MPD tells a socket client where its music lives and
  the music folder then fills itself in.
- **Lyrion Music Server** (formerly Logitech Media Server / Squeezebox Server)
  needs the host and web port (9000 by default), plus a username and password
  only if *Settings → Advanced → Security* has password protection on. Its
  library can be browsed, and every connected player (Squeezebox, piCorePlayer,
  Squeezelite) appears as its own output. Players that connect later show up
  within about ten seconds.
- **UPnP / DLNA players** need nothing: speakers, TVs and receivers that accept
  streams (Sonos, gmrender, upmpdcli, many AV receivers) are found on the local
  network and appear as outputs, and new ones show up within a minute. They
  play anything streamed over HTTP, but not MPD's local files. A player that
  multicast cannot reach can be added by its device-description URL.

**Test Connection** checks the settings without touching the live connection.
**Save and Connect** applies them immediately. Each server's switch controls
whether it is connected, and several can be on at once.

## Where things are kept

| What | Where |
| --- | --- |
| servers and settings | `~/.config/streamplay/config.json` (mode 0600) |
| cover art cache | `~/.cache/streamplay/covers/` |
| the service | `~/.local/share/streamplay/` |
| the widget | `~/.local/share/plasma/plasmoids/io.github.leissa.streamplay/` |

Passwords are not in `config.json` but in the desktop's keyring, through the
freedesktop Secret Service. On Plasma that is KDE Wallet with *Use KWallet for
the Secret Service interface* turned on in System Settings; GNOME Keyring and
KeePassXC work as well. `config.json` can be edited by hand while the service is
stopped.

## Running the service by hand

Useful when something is not working:

```sh
systemctl --user stop streamplay
~/.local/bin/streamplayd -vv          # -v for info, -vv for debug
```

Other options: `--port` to move it off 8760, `--host` to bind elsewhere,
`--no-mpris` to skip the D-Bus registration, `--config` for a different
configuration file.

## Troubleshooting

**The widget says the service is not running.** Check
`systemctl --user status streamplay` and `journalctl --user -u streamplay -n 50`.
If you changed the port, change it in the widget's settings too.

**A server shows as failed.** The settings page prints the reason underneath its
name. For Kodi that is usually remote control not being enabled; for Subsonic,
Jellyfin, Emby and Plex, a wrong URL, password or token; for MPD, `bind_to_address` in `mpd.conf` not covering
the address you gave.

**MPD tracks will not play on the local speakers.** That is the music folder
setting: without it the daemon has no path to the files, and MPD offers none.
The widget will still play them on MPD itself.

**Media keys do nothing.** MPRIS needs `python-dbus` and `python-gobject`. Check
with `busctl --user list | grep mpris` — `org.mpris.MediaPlayer2.streamplay`
should be listed while the service runs.

**Nothing comes out of the speakers.** Local playback goes through mpv, so
`mpv some-file.flac` failing points at the audio setup rather than at this
widget.

**QML errors after editing the widget.** Plasma logs them to the journal rather
than the terminal:

```sh
journalctl --user --since "2 minutes ago" | grep streamplay
```

## Tests

```sh
cd daemon
python3 tests/test_player.py     # queue, shuffle, repeat, output switching, with real mpv
python3 tests/test_protocol.py   # the control protocol, with two services connected
python3 tests/test_mpd.py        # the MPD library and output, against a stub MPD
python3 tests/test_kodi.py       # the Kodi output, against scripted notifications
python3 tests/test_jellyfin.py   # the Jellyfin library, against a stub server
python3 tests/test_emby.py       # the Emby library, against a stub server
python3 tests/test_plex.py       # the Plex library, against a stub server
python3 tests/test_lyrion.py     # the Lyrion library and players, against a stub server
python3 tests/test_upnp.py       # UPnP renderers, against a stub renderer
```

They are self-contained: they generate their own audio and use stub services,
so no music server is needed.

## Control protocol

The applet talks to the service over one WebSocket on `127.0.0.1:8760`. Requests
are `{"id": 1, "method": "queue.add", "params": {…}}` and replies are
`{"id": 1, "ok": true, "result": {…}}`. The service also pushes `state`,
`position`, `queue`, `sources`, `profiles` and `seeked` events. Cover art is
served over plain HTTP from the same port at `/cover?src=…&id=…&size=…`, so the
widget never needs any credentials.

Anything that speaks WebSocket can drive it; `library.*`, `queue.*`, `player.*`,
`sources.*`, `outputs.*` and `profiles.*` are the method groups.

## Disclaimer

This plugin was mostly created with the help of AI.

## Licence

GPL-3.0-or-later; see [`LICENSE`](LICENSE).

The server icons in `plasmoid/package/contents/icons/` come from the upstream
projects: `emby.svg` from [Emby.Resources](https://github.com/MediaBrowser/Emby.Resources),
`jellyfin.svg` from [jellyfin-ux](https://github.com/jellyfin/jellyfin-ux)
(CC BY-SA 4.0), `kodi.svg` from the [Kodi](https://github.com/xbmc/xbmc)
repository, `lyrion.png` from [lyrion.org](https://lyrion.org), `mpd.svg` from
the [MPD](https://github.com/MusicPlayerDaemon/MPD) repository and
`subsonic.png` from [subsonic.org](https://www.subsonic.org). `plex.svg` is the
Plex chevron, redrawn. Kodi is a trademark of the XBMC Foundation, Plex of
Plex, Inc., and all logos remain the property of their respective owners.
