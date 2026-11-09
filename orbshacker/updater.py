"""
updater.py – Auto-update from GitHub Releases.

Only runs when the app is a compiled executable (sys.frozen).
Compares the current VERSION against the latest GitHub Release tag.
If a newer version exists, downloads the .exe asset together with its
published SHA-256 checksum, verifies both, and only then replaces the
running executable.
"""
# pyright: reportUnusedFunction=false

import os
import re
import sys
import tempfile
from pathlib import Path
from typing import TypedDict

import requests
from packaging.version import InvalidVersion, Version

from . import config, ui


class ReleaseAsset(TypedDict):
    name: str
    browser_download_url: str


class GitHubRelease(TypedDict, total=False):
    tag_name: str
    assets: list[ReleaseAsset]


class UpdateError(Exception):
    """Raised when an update cannot be downloaded, verified or applied."""


_CHECKSUM_RE = re.compile(r"[0-9a-fA-F]{64}")
_MIN_EXECUTABLE_BYTES = 1024


def _delete_quietly(path: Path) -> None:
    """Remove a temporary file, ignoring cleanup failures."""
    try:
        path.unlink()
    except OSError:
        pass


def _cleanup_old_exe(old_exe: Path) -> None:
    """Delete a leftover backup executable if it exists."""
    if old_exe.exists():
        try:
            old_exe.unlink()
        except OSError:
            pass


def is_frozen() -> bool:
    """Return True if running as a compiled executable (e.g. PyInstaller)."""
    return getattr(sys, 'frozen', False)


def _parse_version(tag: str) -> Version:
    """Parse a version tag like 'v2.1.0' or '2.1.0' into a Version object."""
    return Version(tag.lstrip('v'))


def _get_latest_release() -> GitHubRelease | None:
    """Fetch the latest release info from the GitHub API.
    Returns dict with 'tag_name' and 'assets' or None on failure.
    """
    url = f"https://api.github.com/repos/{config.GITHUB_REPO_OWNER}/{config.GITHUB_REPO_NAME}/releases/latest"
    try:
        resp = requests.get(url, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _find_exe_asset(assets: list[ReleaseAsset]) -> ReleaseAsset | None:
    """Find the .exe download asset from a release."""
    for asset in assets:
        name = asset.get('name', '')
        if name.lower().endswith('.exe'):
            return asset
    return None


def _find_checksum_asset(
    assets: list[ReleaseAsset],
    exe_asset: ReleaseAsset,
) -> ReleaseAsset | None:
    """Return the ``<exe name>.sha256`` asset published next to the executable."""
    exe_name = exe_asset.get('name', '').strip()
    if not exe_name:
        return None
    expected = f"{exe_name}.sha256".lower()
    for asset in assets:
        if asset.get('name', '').strip().lower() == expected:
            return asset
    return None


def _parse_checksum(text: str) -> str:
    """Extract a SHA-256 hex digest from a checksum file.

    Accepts both a bare digest and the ``<digest>  <filename>`` format written
    by ``sha256sum``. Raises :class:`UpdateError` for anything else.
    """
    if not isinstance(text, str):
        raise UpdateError("checksum file did not contain text")

    cleaned = text.lstrip("\ufeff").strip()
    if not cleaned:
        raise UpdateError("checksum file is empty")

    first_line = cleaned.splitlines()[0].strip()
    parts = first_line.split()
    if not parts or not _CHECKSUM_RE.fullmatch(parts[0]):
        raise UpdateError("checksum file is malformed")

    return parts[0].lower()


def _download_file(url: str, dest: Path) -> None:
    """Download a file from url to dest path.

    When the server declares a usable ``Content-Length`` the number of bytes
    written must match it. Responses without (or with an ignored) length are
    accepted; the checksum is the authoritative integrity check.
    """
    resp = requests.get(url, stream=True, timeout=config.REQUEST_TIMEOUT_LONG)
    resp.raise_for_status()

    expected_length: int | None = None
    declared_length = resp.headers.get("Content-Length")
    content_encoding = (resp.headers.get("Content-Encoding") or "identity").lower()
    if declared_length and content_encoding in ("", "identity"):
        try:
            expected_length = int(declared_length)
        except (TypeError, ValueError):
            expected_length = None

    written = 0
    with open(dest, 'wb') as f:
        for chunk in resp.iter_content(8192):
            if chunk:
                f.write(chunk)
                written += len(chunk)

    if expected_length is not None and written != expected_length:
        raise UpdateError(
            f"incomplete download: server declared {expected_length} bytes, received {written}"
        )


def _download_checksum(url: str) -> str:
    """Download the published checksum file."""
    resp = requests.get(url, timeout=config.REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _validate_executable(path: Path) -> None:
    """Reject files that cannot plausibly be the Windows executable."""
    if not path.is_file():
        raise UpdateError(f"downloaded file is missing: {path}")

    size = path.stat().st_size
    if size < _MIN_EXECUTABLE_BYTES:
        raise UpdateError(f"downloaded file is too small to be an executable ({size} bytes)")

    with open(path, 'rb') as fh:
        if fh.read(2) != b"MZ":
            raise UpdateError("downloaded file is not a Windows executable (missing MZ header)")


def _download_verified(url: str, checksum_url: str, dest: Path) -> None:
    """Download an executable and verify it against its published checksum.

    Raises :class:`UpdateError` when the checksum cannot be fetched, parsed or
    matched. The temporary download is removed on every failure path.
    """
    try:
        _download_file(url, dest)
        expected = _parse_checksum(_download_checksum(checksum_url))
        actual = _sha256(dest)
        if actual != expected:
            raise UpdateError(f"checksum mismatch: expected {expected}, got {actual}")
        _validate_executable(dest)
    except Exception:
        _delete_quietly(dest)
        raise


def _restore_backup(old_exe: Path, current_exe: Path, cause: BaseException) -> None:
    """Put the previous executable back, preserving it if restoration fails."""
    if not old_exe.exists():
        return

    if current_exe.exists():
        try:
            current_exe.unlink()
        except OSError as exc:
            raise UpdateError(
                f"could not remove the partially installed executable ({exc}); "
                f"the previous executable is preserved at {old_exe}"
            ) from cause

    try:
        os.replace(old_exe, current_exe)
    except OSError as exc:
        raise UpdateError(
            f"could not restore the previous executable ({exc}); "
            f"it is preserved at {old_exe}"
        ) from cause


def _replace_exe(new_exe: Path) -> None:
    """Replace the currently running .exe with the new one.

    On Windows we can't overwrite a running file, so we:
    1. Rename current exe to .old
    2. Move the verified new exe into place
    3. Launch the new exe
    4. Exit the current process

    The backup is kept after a successful launch and only removed by
    ``_cleanup_old_exe`` once the replacement reaches the updater again, so a
    binary that dies during startup can still be replaced by the .old file.
    Any failure while installing or launching restores the backup immediately.
    """
    _validate_executable(new_exe)

    current_exe = Path(sys.executable)
    old_exe = current_exe.with_suffix('.old')

    # Clean up a stale backup before moving the current executable aside.
    if old_exe.exists():
        try:
            old_exe.unlink()
        except OSError as exc:
            raise UpdateError(f"cannot remove stale backup {old_exe}: {exc}") from exc

    try:
        os.replace(current_exe, old_exe)
    except OSError as exc:
        raise UpdateError(f"cannot move the current executable aside: {exc}") from exc

    try:
        os.replace(new_exe, current_exe)
    except OSError as exc:
        _restore_backup(old_exe, current_exe, exc)
        raise UpdateError(f"cannot install the downloaded executable: {exc}") from exc

    ui.print_color("[UPDATE] Update downloaded and verified.", ui.Colors.GREEN, bold=True)
    ui.print_color("[UPDATE] Restarting...", ui.Colors.CYAN)

    try:
        os.startfile(str(current_exe))  # type: ignore[attr-defined]
    except Exception as exc:
        _restore_backup(old_exe, current_exe, exc)
        raise UpdateError(f"the new executable could not be started: {exc}") from exc

    sys.exit(0)


def auto_update() -> None:
    """Check for updates and auto-update if a new release is available.

    Only runs when the app is a compiled executable.
    Skips silently when running from source.
    """
    if not is_frozen():
        return  # Running from source – skip update entirely

    _cleanup_old_exe(Path(sys.executable).with_suffix('.old'))

    try:
        ui.loading_animation("Checking for updates", 1.2)
    except Exception:
        pass

    # 1. Fetch latest release
    release = _get_latest_release()
    if not release:
        return

    # 2. Compare versions
    tag = release.get('tag_name', '')
    try:
        remote_version = _parse_version(tag)
        local_version = _parse_version(config.VERSION)
    except InvalidVersion:
        return

    if remote_version <= local_version:
        ui.print_color(f"[UPDATE] You're up to date (v{config.VERSION})", ui.Colors.CYAN)
        return

    assets = release.get('assets', [])

    # 3. Find exe download
    asset = _find_exe_asset(assets)
    if not asset:
        ui.print_color(f"[UPDATE] v{tag} available but no .exe found in release", ui.Colors.YELLOW)
        ui.print_color(f"[UPDATE] Download manually: {config.REPO_URL}/releases/latest", ui.Colors.GRAY)
        return

    # 4. The checksum asset is mandatory – without it there is nothing to verify.
    checksum_asset = _find_checksum_asset(assets, asset)
    if not checksum_asset:
        ui.print_color(
            f"[UPDATE] v{tag} available but {asset['name']}.sha256 is missing from the release",
            ui.Colors.RED,
        )
        ui.print_color(f"[UPDATE] Update aborted. Download manually: {config.REPO_URL}/releases/latest", ui.Colors.GRAY)
        return

    # 5. Download, verify + replace
    ui.print_color(f"[UPDATE] New version available: {config.VERSION} → {tag}", ui.Colors.GREEN, bold=True)
    tmp_path: Path | None = None
    try:
        ui.loading_animation(f"Downloading {asset['name']}", 2.0)
        with tempfile.NamedTemporaryFile(delete=False, suffix='.exe') as tmp_file:
            tmp_path = Path(tmp_file.name)

        _download_verified(
            asset['browser_download_url'],
            checksum_asset['browser_download_url'],
            tmp_path,
        )
        _replace_exe(tmp_path)
    except UpdateError as e:
        ui.print_color(f"[UPDATE] Update aborted: {e}", ui.Colors.RED)
        ui.print_color(f"[UPDATE] Download manually: {config.REPO_URL}/releases/latest", ui.Colors.GRAY)
    except Exception as e:
        ui.print_color(f"[UPDATE] Download failed: {e}", ui.Colors.YELLOW)
        ui.print_color(f"[UPDATE] Download manually: {config.REPO_URL}/releases/latest", ui.Colors.GRAY)
    finally:
        if tmp_path is not None:
            _delete_quietly(tmp_path)


# ── Kept for tests (pure functions) ───────────────────────────────────────────

def _sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of a file."""
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()
