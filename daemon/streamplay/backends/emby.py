"""Emby client, sharing Jellyfin's code since Jellyfin forked Emby's API."""

from __future__ import annotations

from typing import Any

from .jellyfin import JellyfinBackend


class EmbyBackend(JellyfinBackend):
    kind = "emby"

    def __init__(self, profile: dict[str, Any]) -> None:
        super().__init__(profile)
        # Emby's own clients append /emby unless the address already has it.
        if not self.base.lower().endswith(("/emby", "/mediabrowser")):
            self.base += "/emby"

    def _auth_headers(self) -> dict[str, str]:
        headers = {"X-Emby-Authorization": self._authorization()}
        if self.token:
            headers["X-Emby-Token"] = self.token
        return headers
