"""Check whether a newer marzban-node release exists on GitHub.

This module never updates anything — Docker deployments update by pulling
a new image (`docker compose pull && up -d`), which a container cannot
safely trigger for itself from the inside. It only answers "is there
something newer", so the controlling panel can surface that to an admin.
"""
import json
import os
import urllib.error
import urllib.request

from config import UPDATE_CHECK_REPO

_VERSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")


def _read_current_version() -> str:
    try:
        with open(_VERSION_FILE) as f:
            return f.read().strip()
    except OSError:
        return "0.0.0"


CURRENT_VERSION = _read_current_version()


def _parse_version(version: str) -> tuple:
    parts = []
    for piece in version.strip().lstrip("v").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _fetch_latest_release_tag(repo: str) -> str:
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return data.get("tag_name", "").lstrip("v")


def check_for_update() -> dict:
    latest = None
    error = None
    try:
        latest = _fetch_latest_release_tag(UPDATE_CHECK_REPO)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError) as exc:
        error = str(exc)

    update_available = bool(latest) and _parse_version(latest) > _parse_version(CURRENT_VERSION)
    return {
        "current_version": CURRENT_VERSION,
        "latest_version": latest,
        "update_available": update_available,
        "error": error,
    }
