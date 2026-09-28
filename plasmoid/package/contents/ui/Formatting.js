.pragma library

/* Shared display helpers. */

function pad(n) {
    return n < 10 ? "0" + n : "" + n;
}

function duration(seconds) {
    if (!seconds || seconds < 0 || !isFinite(seconds)) {
        return "0:00";
    }
    const total = Math.floor(seconds);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    return hours > 0 ? hours + ":" + pad(minutes) + ":" + pad(secs)
                     : minutes + ":" + pad(secs);
}

function elide(text, limit) {
    if (!text) {
        return "";
    }
    return text.length > limit ? text.substring(0, limit - 1) + "…" : text;
}

function subtitle(track) {
    if (!track) {
        return "";
    }
    const parts = [];
    if (track.artist) {
        parts.push(track.artist);
    }
    if (track.album) {
        parts.push(track.album);
    }
    return parts.join(" — ");
}

function numbered(track, show) {
    if (!track) {
        return "";
    }
    const title = track.title || "";
    if (!show || !track.trackNo) {
        return title;
    }
    const no = track.discNo ? track.discNo + "-" + pad(track.trackNo) : track.trackNo;
    return no + ". " + title;
}

/* The icon standing for a kind of music server, used wherever one is listed. */
function serverIcon(type) {
    switch (type) {
    case "emby":     return Qt.resolvedUrl("../icons/emby.svg");
    case "jellyfin": return Qt.resolvedUrl("../icons/jellyfin.svg");
    case "kodi":     return Qt.resolvedUrl("../icons/kodi.svg");
    case "lyrion":   return Qt.resolvedUrl("../icons/lyrion.png");
    case "mpd":      return Qt.resolvedUrl("../icons/mpd.svg");
    case "plex":     return Qt.resolvedUrl("../icons/plex.svg");
    case "upnp":     return "network-wireless";
    default:         return Qt.resolvedUrl("../icons/subsonic.png");
    }
}
