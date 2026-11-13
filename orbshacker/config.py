"""
orbshacker – central configuration.

Reads user-editable values from the root-level ``settings.py``.
If a value is missing there, the default defined below is used.
Internal-only constants (API URLs, headers, timeouts) live here
and are NOT exposed in settings.py.
"""

import importlib.util
import json
import shutil
import sys
from pathlib import Path
import subprocess
from typing import TypeVar, cast
import os
import tempfile

# Default desktop path, computed at module load time when environment is normal
# This ensures frozen mode defaults work reliably even if environment is later cleared
try:
    _DEFAULT_DESKTOP = str((Path.home() / "Desktop").as_posix())
except RuntimeError:
    _DEFAULT_DESKTOP = str((Path.cwd() / "Desktop").as_posix())

from . import _version as _build_version
from .path_utils import sanitize_relative_path

T = TypeVar("T")

def _get_default_json_content() -> str:
    desktop_path = Path.home() / "Desktop"
    desktop_str = str(desktop_path).replace("\\", "/")
    content = {
        "CHOSEN_FOLDER": desktop_str,
        "AUTO_DELETE": False,
        "TIMER_MINUTES": 15
    }
    return json.dumps(content, indent=2)

def _is_faked_game() -> bool:
    """Check if the currently running executable/script is a faked game copy."""
    if getattr(sys, "frozen", False):
        name = Path(sys.executable).name.lower()
        return name != "orbshacker.exe"
    else:
        name = Path(sys.argv[0]).name.lower()
        return name not in ("orbshacker.py", "__main__.py") and "pytest" not in name

def _load_embedded_settings() -> dict | None:
    if not getattr(sys, "frozen", False):
        return None
    try:
        exe_path = Path(sys.executable)
        if not exe_path.exists():
            return None
        with open(exe_path, "rb") as f:
            # Seek to end and read up to 65536 bytes
            f.seek(0, 2)
            file_size = f.tell()
            read_size = min(file_size, 65536)
            f.seek(file_size - read_size)
            chunk = f.read(read_size)
        
        marker = b"__ORBSHACKER_BAKED_CONFIG__"
        if marker in chunk:
            parts = chunk.split(marker)
            if len(parts) >= 3:
                json_bytes = parts[-2]
                return json.loads(json_bytes.decode("utf-8"))
    except Exception:
        pass
    return None

def _get_appdata_settings_path() -> Path | None:
    """Return the path to the Orbshacker settings.json in LocalAppData,
    or ``None`` if ``LOCALAPPDATA`` is unavailable.
    """
    local_appdata = os.getenv("LOCALAPPDATA")
    if not local_appdata:
        return None
    return Path(local_appdata) / "Orbshacker" / "settings.json"

def _is_valid_json_file(path: Path) -> bool:
    """Check if *path* exists and contains valid JSON.

    Never modifies or side-effects the file.
    Returns ``True`` only when the file can be parsed as JSON.
    """
    if not path.exists():
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)
        return True
    except Exception:
        return False

SETTINGS_WARNINGS: list[str] = []


def _warn(message: str) -> None:
    """Record a user-facing configuration warning (deduplicated per load)."""
    if message not in SETTINGS_WARNINGS:
        SETTINGS_WARNINGS.append(message)


def _load_settings():
    # Each load reports only the problems found during that load.
    SETTINGS_WARNINGS.clear()
    # 1. Try loading embedded settings from the executable
    embedded = _load_embedded_settings()
    if embedded is not None:
        return embedded

    # 2. For frozen (packaged) mode: AppData first, then legacy exe-dir settings.json
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        appdata_path = _get_appdata_settings_path()

        # 2a. Try loading from %LOCALAPPDATA%Orbshacker/settings.json
        if appdata_path is not None:
            if _is_valid_json_file(appdata_path):
                # Valid AppData config -- load it
                settings = _read_json_safely(appdata_path)
                if settings is not None:
                    return settings
            else:
                # Malformed AppData -- preserve it, warn the user, use defaults
                _warn(
                    "[!] Malformed configuration found at "
                    + str(appdata_path)
                    + ". Using in-memory defaults."
                )
                # Fall through to below (legacy or defaults)

        # 2b. AppData either None, invalid, or we fell through -- inspect legacy exe-dir settings.json
        legacy_json_path = exe_dir / "settings.json"

        if legacy_json_path.exists():
            if _is_valid_json_file(legacy_json_path):
                # Valid legacy config -- migrate to AppData using safe no-clobber publication
                if _migrate_json_if_safe(legacy_json_path, appdata_path) if appdata_path else False:
                    # Migration succeeded -- return the migrated settings
                    settings = _read_json_safely(appdata_path)
                    if settings is not None:
                        return settings
                # Migration failed (dst existed or error) -- preserve legacy file
                # _migrate_json_if_safe handles its own temp file cleanup
            else:
                # Malformed legacy config -- preserve it, warn the user, use defaults
                _warn(
                    "[!] Malformed legacy configuration found at "
                    + str(legacy_json_path)
                    + ". Using in-memory defaults."
                )
                # Fall through to defaults below
        else:
            # No legacy json beside exe -- create defaults in AppData only (not beside exe)
            if appdata_path is not None:
                try:
                    tmp_path = exe_dir / f".migrate_{os.getpid()}.json"
                    default_content = _get_default_json_content()
                    fd = os.open(str(tmp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(default_content)
                        f.flush()
                        os.fsync(f.fileno())
                    _atomic_publish(appdata_path, tmp_path, default_content)
                except FileExistsError:
                    # Another process already created the temp file; clean up and fall through
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass
                except Exception:
                    # AppData unavailable or unwritable -- warn, fall through to defaults
                    _warn("[!] AppData is unavailable or unwritable. Using in-memory defaults.")
            # Fall through to defaults below
        # In frozen mode, fall through to dev-mode settings.json check below
        # (this path is only reached if somehow not frozen, as a fallback)


    # 3. Try loading settings.json (only in development mode)
    #    In frozen mode, skip this - AppData/legacy path was already attempted
    if not getattr(sys, "frozen", False):
        dev_dir = Path(__file__).resolve().parents[1]
        dev_settings_path = dev_dir / "settings.json"
        if dev_settings_path.exists():
            try:
                with open(dev_settings_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

    # 4. Fallback to settings.py (backward compatibility)
    # In frozen mode, return in-memory defaults instead of importing settings.py
    # to avoid writing mutable settings beside the executable and
    # to prevent the settings.py module from being treated as the user config
    if getattr(sys, "frozen", False):
        # Use a safe desktop path that doesn't depend on Path.home() at call time.
        # The _DEFAULT_DESKTOP was computed at module load time with error handling.
        # Return "Desktop" as a plain string for consistent comparison.
        return {
            "CHOSEN_FOLDER": "Desktop",
            "AUTO_DELETE": False,
            "TIMER_MINUTES": 15
        }
    # In development mode, check for legacy config and validate it
    legacy_json_path = Path(__file__).resolve().parents[1] / "settings.json"
    if legacy_json_path.exists():
        if _is_valid_json_file(legacy_json_path):
            # Valid legacy config - load it
            try:
                with open(legacy_json_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        else:
            # Malformed legacy config - preserve it, use in-memory defaults
            # Don't import settings module - return in-memory defaults instead
            _warn(
                "[!] Malformed legacy configuration found at "
                + str(legacy_json_path)
                + ". Using in-memory defaults."
            )
            # Return in-memory defaults
            return {
                "CHOSEN_FOLDER": _DEFAULT_DESKTOP,
                "AUTO_DELETE": False,
                "TIMER_MINUTES": 15
            }
    # No valid legacy config - fall back to settings.py
    try:
        import settings as _user  # root-level settings.py
        return _user
    except ImportError:
        return None  # no user settings file -- use all defaults


def _read_json_safely(path: Path) -> dict | None:
    """Read and parse JSON from *path*.

    Returns the parsed ``dict`` when the file is valid JSON,
    ``None`` when the file is missing, empty, or malformed.
    Never modifies or side-effects the file.
    """
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return None
    except Exception:
        return None



_user = _load_settings()



def _atomic_publish(dst_path: Path, tmp_path: Path, data: dict | None = None) -> bool:
    """Atomically publish configuration to *dst_path* using a temp file.

    Writes *data* (or an empty dict if None) to *tmp_path* (in the same directory
    as *dst_path*), flushes and fsyncs, then ``os.rename()`` (atomic on same
    filesystem).

    If *dst_path* already exists (valid or invalid JSON), the destination is
    left untouched and the function returns ``False`` (no-clobber).

    On success the destination contains the new JSON and ``True`` is returned.
    On failure the temp file is cleaned up and ``False`` is returned.
    """
    try:
        write_data = data if data is not None else {}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(write_data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.rename(str(tmp_path), str(dst_path))
        return True
    except FileExistsError:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return False
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return False


def _migrate_json_if_safe(src_path: Path, dst_path: Path) -> bool:
    """Safely migrate JSON from *src_path* to *dst_path*.

    Reads and validates the source file.  Only when the source is valid JSON
    does it attempt the atomic no-clobber publication to *dst_path*.

    If the destination already exists it is left untouched and the function
    returns ``False``.
    If the source is malformed both files are left untouched and ``False`` is
    returned.
    """
    if not src_path.exists():
        return False
    source_data = _read_json_safely(src_path)
    if source_data is None:
        return False
    try:
        # Use exclusive creation to avoid name collisions in concurrent operations
        # os.open with O_CREAT | O_EXCL ensures the file does not already exist
        tmp_path = dst_path.parent / f".migrate_{os.getpid()}.json"
        fd = os.open(str(tmp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        # Wrap the file descriptor using os.fdopen
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(source_data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
    except FileExistsError:
        # Another process already created the temp file; clean up and return False
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return False
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return False

    # Verify we can read back what we wrote before publishing
    try:
        with open(tmp_path, "r", encoding="utf-8") as f:
            verify_data = json.load(f)
    except Exception:
        # Corrupted temp file -- clean up and fail
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return False

    result = _atomic_publish(dst_path, tmp_path, source_data)

    # Clean up temp file regardless of publish result -- it was already renamed
    try:
        os.unlink(tmp_path)
    except Exception:
        pass

    return result

def _get(name: str, default: T) -> T:
    """Return a value from the user settings, falling back to *default*."""
    if _user is None:
        return default
    if isinstance(_user, dict):
        return cast(T, _user.get(name, default))
    return cast(T, getattr(_user, name, default))


# ── Settings normalisation ─────────────────────────────────────────────────────
# Settings come from JSON or settings.py, so their types are not guaranteed by
# the loader. These helpers make each value safe to consume and record a warning
# instead of silently misbehaving when a value is unusable.

MIN_TIMER_MINUTES = 1
MAX_TIMER_MINUTES = 1440


def _describe(value: object) -> str:
    """Return a short, safe representation of a rejected setting value."""
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _coerce_bool(name: str, value: object, default: bool = False) -> bool:
    """Coerce a setting to a real ``bool``.

    Accepts booleans, integer 0/1, and the case-insensitive strings
    ``true``/``false``/``yes``/``no``/``1``/``0``. Anything else falls back to
    *default* and records one warning naming the setting.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in ("true", "yes", "1"):
            return True
        if token in ("false", "no", "0"):
            return False
    _warn(f"[!] Invalid {name} value {_describe(value)}; using {default}.")
    return default


def _coerce_timer_minutes(name: str, value: object, default: int = 15) -> int:
    """Coerce a setting to an ``int`` in ``1..1440`` minutes.

    Integers and optionally signed digit strings (``"30"``, ``"+30"``,
    ``"-5"``) are accepted; out-of-range numbers are clamped with a warning.
    Booleans, decimals, non-numeric strings and other types fall back to
    *default* with a warning.
    """
    if isinstance(value, bool):
        minutes = None
    elif isinstance(value, int):
        minutes = value
    elif isinstance(value, str):
        token = value.strip()
        digits = token[1:] if token[:1] in ("+", "-") else token
        minutes = None
        # ``isdigit()`` accepts characters ``int()`` cannot parse (e.g. "\u00b2");
        # ``isdecimal()`` matches what ``int()`` accepts, and the try guards the
        # remaining conversion limits.
        if digits.isdecimal():
            try:
                minutes = int(token)
            except ValueError:
                minutes = None
    else:
        minutes = None

    if minutes is None:
        _warn(f"[!] Invalid {name} value {_describe(value)}; using {default} minutes.")
        return default

    clamped = min(max(minutes, MIN_TIMER_MINUTES), MAX_TIMER_MINUTES)
    if clamped != minutes:
        _warn(
            f"[!] {name} value {minutes} is outside "
            f"{MIN_TIMER_MINUTES}-{MAX_TIMER_MINUTES}; using {clamped} minutes."
        )
    return clamped


def _coerce_folder(name: str, value: object, default: Path) -> Path:
    """Coerce a setting to a ``Path``, preserving the existing resolution rules."""
    if isinstance(value, Path):
        return value
    if isinstance(value, str):
        if not value.strip() or value.strip() == "Desktop":
            return Path.home() / "Desktop"
        return Path(value)
    _warn(f"[!] Invalid {name} value {_describe(value)}; using {default}.")
    return default


def _normalised_settings() -> dict:
    """Return validated values for the user-editable settings."""
    default_folder = Path.home() / "Desktop"
    return {
        "AUTO_DELETE": _coerce_bool("AUTO_DELETE", _get("AUTO_DELETE", False)),
        "TIMER_MINUTES": _coerce_timer_minutes("TIMER_MINUTES", _get("TIMER_MINUTES", 15)),
        "CHOSEN_FOLDER": _coerce_folder(
            "CHOSEN_FOLDER",
            _get("CHOSEN_FOLDER", str(default_folder)),
            default_folder,
        ),
    }


def _git_version() -> str | None:
    """Return the current git tag as a version string when available."""
    commands = [
        ["git", "describe", "--tags", "--exact-match", "HEAD"],
        ["git", "describe", "--tags", "--abbrev=0", "--match", "v*"],
    ]
    for command in commands:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True,
                cwd=Path(__file__).resolve().parents[1],
            )
            tag = result.stdout.strip()
            if tag:
                return tag.lstrip("v")
        except Exception:
            continue
    return None


def _resolve_version() -> str:
    """Resolve the app version from build metadata or git tags."""
    built_version = getattr(_build_version, "VERSION", None)
    if built_version:
        return str(built_version)

    git_version = _git_version()
    if git_version:
        return git_version

    return "0.0.0"


# ── App identity ──────────────────────────────────────────────────────────────
VERSION   = _resolve_version()
DEVELOPER = "Strykey / Daniel Pires / Pannenkoekisus"

# ── GitHub repo ───────────────────────────────────────────────────────────────
# Release builds write ``_version.GITHUB_REPOSITORY`` with the repository that
# published the executable; source checkouts fall back to this fork.
_DEFAULT_REPO_OWNER = "codee-vinay"
_DEFAULT_REPO_NAME  = "orbshacker"


def _resolve_repository() -> tuple[str, str]:
    """Return the repository the updater should query.

    ``_version.GITHUB_REPOSITORY`` is written by the release workflow as
    ``owner/name``. Missing or malformed values keep the development default.
    """
    injected = getattr(_build_version, "GITHUB_REPOSITORY", None)
    if isinstance(injected, str):
        owner, separator, name = injected.strip().partition("/")
        owner = owner.strip()
        name = name.strip()
        if separator and owner and name and "/" not in name:
            return owner, name
    return _DEFAULT_REPO_OWNER, _DEFAULT_REPO_NAME


GITHUB_REPO_OWNER, GITHUB_REPO_NAME = _resolve_repository()
REPO_URL = f"https://github.com/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}"

# ── Network endpoints (internal – not in settings.py) ─────────────────────────
DISCORD_API_URL        = "https://discord.com/api/v9/applications/detectable"
GITHUB_BACKUP_URL      = (
    "https://gist.githubusercontent.com/Cynosphere/"
    "c1e77f77f0e565ddaac2822977961e76/raw/gameslist.json"
)
STEAMCMD_API_URL       = "https://api.steamcmd.net/v1/info"
STEAM_STORE_SEARCH_URL = "https://store.steampowered.com/api/storesearch"

# ── HTTP settings (internal) ─────────────────────────────────────────────────
REQUEST_TIMEOUT      = 10
REQUEST_TIMEOUT_LONG = 20

DISCORD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://discord.com/",
    "Origin":          "https://discord.com",
}

# ── UI / UX (user-editable via settings.py or settings.json) ──────────────────
SLEEP_SHORT        = 1.0
SLEEP_LONG         = 2.0
FAKE_EXE_DIR       = sanitize_relative_path(_get("FAKE_EXE_DIR", "Win64"))
MAX_SEARCH_RESULTS = 20
_effective_settings = _normalised_settings()

AUTO_DELETE        = _effective_settings["AUTO_DELETE"]
TIMER_MINUTES      = _effective_settings["TIMER_MINUTES"]
STEAM_MANIFEST_PATH = _get("STEAM_MANIFEST_PATH", None)

# CHOSEN_FOLDER is always a Path object
CHOSEN_FOLDER = _effective_settings["CHOSEN_FOLDER"]
