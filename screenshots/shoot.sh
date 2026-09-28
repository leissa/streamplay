#!/usr/bin/env bash
#
# Render KDE Store screenshots into build/screenshots: the applet in a real panel of a
# throwaway Plasma session in a headless nested KWin, talking to the real daemon over
# an invented library.
#
#   ./screenshots/shoot.sh [name...]

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
OUT="$ROOT/build/screenshots"
PORT=18760
WIDTH=1600
HEIGHT=1000
# The popup plus a strip of panel, for the close-up variants.
CROP=600x660+1000+340

die() { printf '\033[31m==>\033[0m %s\n' "$*" >&2; exit 1; }

for tool in kwin_wayland plasmashell spectacle qdbus6 dbus-run-session magick; do
    command -v "$tool" >/dev/null || die "Missing $tool"
done
python3 -c 'import numpy, PIL' 2>/dev/null || die "Missing python-numpy or python-pillow"

# name  tab  output  colorscheme  desktoptheme  library stack
SHOTS=(
    'playing|0|local|BreezeDark|breeze-dark|null'
    'queue|1|local|BreezeDark|breeze-dark|null'
    'library|2|local|BreezeDark|breeze-dark|null'
    'lyrics|3|local|BreezeDark|breeze-dark|null'
    'album|2|kodi:kodi|BreezeDark|breeze-dark|[{ mode: "albums", title: "" }, { mode: "albumTracks", id: "copper-sky", source: "kodi", title: "Copper Sky" }]'
    'playing-light|0|kodi:kodi|BreezeLight|default|null'
)

stage=$(mktemp -d)
daemon=
# D-Bus activated services outlive the nested session; they all inherit STAGE.
reap() {
    local environ pid
    for environ in /proc/[0-9]*/environ; do
        grep -qsz "^STAGE=$stage\$" "$environ" || continue
        pid=${environ#/proc/}
        kill "${pid%/environ}" 2>/dev/null || true
    done
}
cleanup() {
    [ -n "$daemon" ] && kill "$daemon" 2>/dev/null
    reap
    sleep 0.5
    rm -rf "$stage"
}
trap cleanup EXIT

python3 "$HERE/covers.py" "$stage/art" >/dev/null

pkg="$stage/data/plasma/plasmoids/io.github.leissa.streamplay"
mkdir -p "$(dirname "$pkg")"
cp -r "$ROOT/plasmoid/package" "$pkg"

# Hooks for the shot: which tab and library page to show, and open the popup by itself.
python3 - "$pkg/contents" "$PORT" <<'EOF'
import pathlib, sys
root, port = pathlib.Path(sys.argv[1]), sys.argv[2]
ui = root / "ui"
edits = [
    (root / "config/main.xml", "<default>8760</default>", f"<default>{port}</default>"),
    (ui / "main.qml", 'import "Formatting.js" as Fmt\n',
     'import "Formatting.js" as Fmt\nimport "Shot.js" as Shot\n'),
    (ui / "main.qml", "    compactRepresentation: CompactRepresentation {}",
     "    Timer { interval: 3000; running: true; onTriggered: root.expanded = true }\n"
     "    compactRepresentation: CompactRepresentation {}"),
    (ui / "FullRepresentation.qml", "import org.kde.kirigami as Kirigami\n",
     'import org.kde.kirigami as Kirigami\nimport "Shot.js" as Shot\n'),
    (ui / "FullRepresentation.qml", "    property string currentKey: tabKeys[0]",
     "    property string currentKey: tabKeys[Shot.tab]"),
    (ui / "LibraryPane.qml", "import org.kde.kirigami as Kirigami\n",
     'import org.kde.kirigami as Kirigami\nimport "Shot.js" as Shot\n'),
    (ui / "LibraryPane.qml", '    property var stack: [{ mode: "albums", title: "" }]',
     '    property var stack: Shot.stack || [{ mode: "albums", title: "" }]'),
]
for path, old, new in edits:
    text = path.read_text()
    if old not in text:
        sys.exit(f"screenshot hook no longer applies to {path.name}: {old.strip()!r}")
    path.write_text(text.replace(old, new, 1))
EOF

mkdir -p "$OUT"
for spec in "${SHOTS[@]}"; do
    IFS='|' read -r name tab output scheme theme stack <<<"$spec"
    if [ $# -gt 0 ] && [[ " $* " != *" $name "* ]]; then
        continue
    fi
    printf '\033[1m==>\033[0m %s\n' "$name"

    printf '.pragma library\nvar tab = %s;\nvar stack = %s;\n' "$tab" "$stack" \
        >"$pkg/contents/ui/Shot.js"
    rm -rf "$stage/config" "$stage/cache" "$stage/state"
    mkdir -p "$stage/config/autostart"
    # A fresh profile would otherwise greet us with plasma-welcome.
    printf '[Module-plasma_welcome]\nautoload=false\n' >"$stage/config/kded6rc"
    printf '[Desktop Entry]\nHidden=true\n' >"$stage/config/autostart/org.kde.kdeconnect.daemon.desktop"

    python3 "$HERE/demo.py" "$PORT" "$stage/art" "$output" >"$stage/daemon.log" 2>&1 &
    daemon=$!
    for _ in $(seq 50); do
        grep -q ready "$stage/daemon.log" && break
        kill -0 "$daemon" 2>/dev/null || die "Demo daemon failed: $(cat "$stage/daemon.log")"
        sleep 0.2
    done

    rm -f "$OUT/$name.png"
    STAGE="$stage" XDG_CONFIG_HOME="$stage/config" XDG_DATA_HOME="$stage/data" \
    XDG_CACHE_HOME="$stage/cache" XDG_STATE_HOME="$stage/state" \
        timeout 90 dbus-run-session -- kwin_wayland --virtual --no-lockscreen \
            --width "$WIDTH" --height "$HEIGHT" --socket "streamplay-shot-$$" \
            --exit-with-session "$HERE/session.sh $OUT/$name.png $scheme $theme" \
            >"$stage/kwin.log" 2>&1 || true
    reap

    kill "$daemon" 2>/dev/null || die "Demo daemon died: $(tail -5 "$stage/daemon.log")"
    wait "$daemon" 2>/dev/null || true
    daemon=
    [ -s "$OUT/$name.png" ] || die "No screenshot for $name: $(tail -5 "$stage/spectacle.log" 2>/dev/null)"
    magick "$OUT/$name.png" -crop "$CROP" +repage "$OUT/$name-popup.png"
done

grep -i "streamplay.*\(error\|warning\|unavailable\)" "$stage/plasmashell.log" \
    && printf '\033[33m==>\033[0m QML reported problems, see above\n' >&2
printf '\033[1m==>\033[0m %s\n' "$OUT"
