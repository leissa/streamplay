"""Connection profiles and daemon settings, in one hand-editable JSON file.

Passwords live in the Secret Service instead; the hub merges them in.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

from . import secretstore

log = logging.getLogger(__name__)

CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "streamplay"
CONFIG_FILE = CONFIG_DIR / "config.json"

CACHE_DIR = Path(
    os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
) / "streamplay"

DEFAULT_PORT = 8760

#: Fields that are secret and therefore never leave the daemon in clear text.
SECRET_FIELDS = ("password",)

#: Profiles every config has, which can be switched off but not removed.
BUILTIN_PROFILES = {
    "youtube": {"name": "YouTube Music", "enabled": False},
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or uuid.uuid4().hex[:8]


class Profile(dict):
    """One backend configuration.

    Common: ``id``, ``name``, ``type`` (a key of ``backends.BACKEND_TYPES``).
    Subsonic: ``url``, ``username``, ``password``, ``legacyAuth``, ``verifyTls``.
    Jellyfin, Emby: ``url``, ``username``, ``password``, ``verifyTls``.
    Plex: ``url``, ``password`` (the X-Plex-Token), ``verifyTls``.
    Kodi: ``host``, ``port``, ``username``, ``password``, ``wsPort``, ``useTls``.
    MPD: ``host``, ``port``, ``password``, ``musicDirectory``, ``socket``.
    ``socket`` replaces host/port and lets MPD reveal ``musicDirectory`` itself.
    Lyrion: ``host``, ``port``, ``username``, ``password``.
    UPnP: ``renderers``, device-description URLs for players discovery misses.
    YouTube: nothing; it is built in.
    """

    @property
    def id(self) -> str:
        return str(self.get("id", ""))

    @property
    def name(self) -> str:
        return str(self.get("name") or self.id)

    @property
    def type(self) -> str:
        return str(self.get("type", "subsonic"))

    @property
    def builtin(self) -> bool:
        return self.id in BUILTIN_PROFILES

    def redacted(self) -> dict[str, Any]:
        """A copy safe for the applet, with each secret reduced to a ``has<Field>`` flag.
        """
        out = {k: v for k, v in self.items() if k not in SECRET_FIELDS}
        for f in SECRET_FIELDS:
            out["has" + f[0].upper() + f[1:]] = bool(self.get(f))
        return out


class Config:
    def __init__(self, path: Path = CONFIG_FILE) -> None:
        self.path = path
        self.profiles: dict[str, Profile] = {}
        self.settings: dict[str, Any] = {}
        self.load()
        for pid, defaults in BUILTIN_PROFILES.items():
            self.profiles[pid] = Profile({**defaults, **self.profiles.get(pid, {}),
                                          "id": pid, "type": pid})


    def load(self) -> None:
        if not self.path.exists():
            self.settings = self._default_settings()
            return
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            log.error("cannot read %s: %s -- starting with empty config", self.path, exc)
            self.settings = self._default_settings()
            return

        self.profiles = {}
        for pid, data in (raw.get("profiles") or {}).items():
            prof = Profile(data)
            prof["id"] = pid
            for field in SECRET_FIELDS:
                prof.pop(field, None)
            self.profiles[pid] = prof

        self.settings = self._default_settings()
        self.settings.update(raw.get("settings") or {})

    def merge_secrets(self, secrets: dict[tuple[str, str], str]) -> None:
        for (pid, field), value in secrets.items():
            if pid in self.profiles and field in SECRET_FIELDS:
                self.profiles[pid][field] = value

    @staticmethod
    def _default_settings() -> dict[str, Any]:
        return {
            "port": DEFAULT_PORT,
            "volume": 0.7,
            "repeat": "none",
            "shuffle": False,
            "autoConnect": True,
            # Subsonic streaming: 0 = let the server decide (usually raw).
            "maxBitrate": 0,
            "streamFormat": "raw",
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "profiles": {pid: {k: v for k, v in p.items() if k not in SECRET_FIELDS}
                         for pid, p in self.profiles.items()},
            "settings": self.settings,
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        os.chmod(self.path, 0o600)


    def upsert(self, data: dict[str, Any]) -> Profile:
        """Create or update a profile; no password means keep the stored one.
        """
        pid = str(data.get("id") or "").strip()
        if not pid:
            pid = slugify(str(data.get("name") or data.get("type") or "profile"))
            base, n = pid, 2
            while pid in self.profiles:
                pid, n = f"{base}-{n}", n + 1

        existing = self.profiles.get(pid, Profile())
        merged = Profile(existing)
        for key, value in data.items():
            if key in SECRET_FIELDS and value in (None, ""):
                continue  # keep the stored secret
            if key.startswith("has"):
                continue  # redaction artefact echoed back by the UI
            merged[key] = value
        merged["id"] = pid

        for field in SECRET_FIELDS:
            if data.get(field) and data[field] != existing.get(field):
                secretstore.store(pid, field, str(data[field]),
                                  f"Streamplay: {merged.name}")

        self.profiles[pid] = merged
        self.save()
        return merged

    def delete(self, pid: str) -> bool:
        if pid not in self.profiles or self.profiles[pid].builtin:
            return False
        del self.profiles[pid]
        self.save()
        try:
            secretstore.forget(pid)
        except secretstore.SecretStoreError as exc:
            log.error("cannot remove the password of %s: %s", pid, exc)
        return True

    def set_setting(self, key: str, value: Any) -> None:
        self.settings[key] = value
        self.save()

    def listed(self) -> list[Profile]:
        """Every profile in the order shown, built-in ones last."""
        return sorted(self.profiles.values(), key=lambda p: p.builtin)

    def redacted_profiles(self) -> list[dict[str, Any]]:
        return [p.redacted() for p in self.listed()]
