"""Download and install a specific Xray-core release, verifying its
official checksum, then hot-restart the running core with the last
config that was applied on this connection.

No new dependency: uses urllib from the standard library instead of
requests, consistent with the rest of this minimal-footprint project.
"""
import hashlib
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile

from config import XRAY_EXECUTABLE_PATH
from logger import logger
from xray import wait_until_ready

_RELEASES_BASE = "https://github.com/XTLS/Xray-core/releases/download"
_ARCH_MAP = {
    "x86_64": "64",
    "amd64": "64",
    "aarch64": "arm64-v8a",
    "arm64": "arm64-v8a",
    "armv7l": "arm32-v7a",
    "armv6l": "arm32-v6",
    "i386": "32",
    "i686": "32",
}


class XrayUpdateError(Exception):
    pass


def _xray_asset_name() -> str:
    machine = platform.machine().lower()
    arch = _ARCH_MAP.get(machine)
    if not arch:
        raise XrayUpdateError(f"Unsupported architecture for remote update: {machine}")
    return f"Xray-linux-{arch}.zip"


def _validate_version(version: str) -> str:
    version = (version or "").strip().lstrip("v")
    if not version or not all(part.isdigit() for part in version.split(".")):
        raise XrayUpdateError(f"Invalid version string: {version!r}")
    return version


def _fetch(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise XrayUpdateError(f"Not found: {url} (does this Xray version/asset exist?)")
        raise XrayUpdateError(f"Failed to fetch {url}: HTTP {exc.code}")
    except urllib.error.URLError as exc:
        raise XrayUpdateError(f"Failed to fetch {url}: {exc.reason}")


def _verify_checksum(asset_bytes: bytes, dgst_text: str, asset_name: str) -> None:
    # Xray-core publishes "<name>.dgst" files with one hash per line, e.g.
    # "SHA2-256= <hex>". Accept SHA256/SHA2-256 spellings, case-insensitive.
    expected = None
    for line in dgst_text.splitlines():
        key, _, value = line.partition("=")
        if key.strip().upper().replace("-", "") in ("SHA256", "SHA2256"):
            expected = value.strip().split()[0].lower()
            break
    if not expected:
        raise XrayUpdateError("Could not find a SHA256 entry in the release .dgst file")

    actual = hashlib.sha256(asset_bytes).hexdigest()
    if actual != expected:
        raise XrayUpdateError(
            f"Checksum mismatch for {asset_name}: expected {expected}, got {actual}"
        )


class PreparedUpdate:
    """A downloaded, checksum-verified, sanity-checked Xray binary staged
    in a temp directory, ready for apply() to swap in. Holding one keeps
    its temp directory alive; if it's never passed to apply() (e.g. the
    caller decides not to proceed after fetch_and_verify() succeeds),
    call .cleanup() explicitly to remove the temp directory."""

    def __init__(self, version: str, tmp_dir: tempfile.TemporaryDirectory, binary_path: str):
        self.version = version
        self._tmp_dir = tmp_dir
        self.binary_path = binary_path

    def cleanup(self) -> None:
        self._tmp_dir.cleanup()


def fetch_and_verify(version: str) -> PreparedUpdate:
    """Download Xray `version`, verify its checksum, and confirm the
    extracted binary actually runs — entirely independent of any running
    core. Deliberately separate from apply(): this step is pure network
    I/O plus local verification, so it must run WITHOUT the caller's
    core_lock — holding that lock for as long as GitHub takes to respond
    would block every other core-mutating request (connect, disconnect,
    start, stop, restart) for the same duration, even though none of them
    touch anything this step reads or writes.

    Raises XrayUpdateError on any failure. Returns a PreparedUpdate to
    pass to apply().
    """
    version = _validate_version(version)
    asset_name = _xray_asset_name()
    base_url = f"{_RELEASES_BASE}/v{version}"

    logger.info(f"Downloading Xray {version} ({asset_name})...")
    asset_bytes = _fetch(f"{base_url}/{asset_name}")
    dgst_text = _fetch(f"{base_url}/{asset_name}.dgst").decode()
    _verify_checksum(asset_bytes, dgst_text, asset_name)

    tmp_dir = tempfile.TemporaryDirectory()
    try:
        zip_path = os.path.join(tmp_dir.name, asset_name)
        with open(zip_path, "wb") as f:
            f.write(asset_bytes)

        with zipfile.ZipFile(zip_path) as zf:
            zf.extract("xray", tmp_dir.name)

        binary_path = os.path.join(tmp_dir.name, "xray")
        os.chmod(binary_path, os.stat(binary_path).st_mode | stat.S_IEXEC)

        # Sanity-check the new binary actually runs before touching the live one.
        try:
            output = subprocess.check_output(
                [binary_path, "version"], stderr=subprocess.STDOUT, timeout=10
            ).decode()
        except Exception as exc:
            raise XrayUpdateError(f"Downloaded binary failed to run: {exc}")
        if version not in output:
            raise XrayUpdateError(
                f"Downloaded binary reports unexpected version (wanted {version}): {output.strip()}"
            )
    except Exception:
        tmp_dir.cleanup()
        raise

    return PreparedUpdate(version, tmp_dir, binary_path)


def apply(prepared: PreparedUpdate, core) -> dict:
    """Swap in a PreparedUpdate's binary, then restart `core` with its own
    last-applied config if it was running, rolling back to the previous
    binary on any failure.

    `core` is the XRayCore instance the caller is managing; it must expose
    .version, .started, .config (last config passed to start/restart),
    .get_version(), .stop() and .start(config). Everything here touches
    shared state (the installed binary, `core`), so the caller MUST hold
    its core_lock across this call.

    Returns {"previous_version", "new_version"}. Raises XrayUpdateError on
    any failure — including a downloaded binary that won't start — after
    rolling back to the previously-installed binary. Always consumes
    (cleans up) `prepared`, success or failure.
    """
    version = prepared.version
    previous_version = core.version
    was_started = core.started
    config = getattr(core, "config", None)
    backup_path = f"{XRAY_EXECUTABLE_PATH}.bak"

    try:
        # Back up the currently-installed binary *before* stopping the core
        # or touching anything live. If this fails (full disk, permissions,
        # a stale file occupying backup_path, ...), abort here: the running
        # core is never even stopped, rather than swapping in an unverified
        # binary with no way back if it turns out to be broken.
        try:
            shutil.copy2(XRAY_EXECUTABLE_PATH, backup_path)
        except OSError as exc:
            raise XrayUpdateError(f"Refusing to update: could not back up current binary: {exc}")

        if was_started:
            core.stop()

        try:
            shutil.move(prepared.binary_path, XRAY_EXECUTABLE_PATH)
        except OSError as exc:
            shutil.move(backup_path, XRAY_EXECUTABLE_PATH)
            if was_started:
                core.start(config)
            raise XrayUpdateError(f"Failed to install new binary: {exc}")
    finally:
        prepared.cleanup()

    core.version = core.get_version()

    if was_started:
        if config is None:
            logger.warning("Xray binary updated but no known config to restart with; core left stopped")
        else:
            started_ok = False
            try:
                core.start(config)
                started_ok = wait_until_ready(core)
            except Exception as exc:
                logger.error(f"New Xray {version} failed to start: {exc}")

            if not started_ok:
                logger.error(
                    f"New Xray {version} failed to start or didn't stay running; "
                    f"rolling back to {previous_version}"
                )
                shutil.move(backup_path, XRAY_EXECUTABLE_PATH)
                core.version = core.get_version()
                core.start(config)
                raise XrayUpdateError(
                    f"New version failed to start or stay running, rolled back to {previous_version}"
                )

    if os.path.exists(backup_path):
        os.remove(backup_path)

    logger.warning(f"Xray updated: {previous_version} -> {core.version}")
    return {"previous_version": previous_version, "new_version": core.version}
