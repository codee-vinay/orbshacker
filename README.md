# orbshacker

**Community-maintained fork of [DanielPires2000/orbshacker](https://github.com/DanielPires2000/orbshacker)**
A Windows tool for Discord Orb quest spoofing — creates fake game processes without installing actual games.

![orbshacker terminal interface](https://capsule-render.vercel.app/api?type=rect&color=0a0a0a,60:0d1f0d,100:1a4a1a&height=32&section=header&text=orbshacker&fontSize=24&fontColor=4ade80)

---

orbshacker is a Windows tool that tricks Discord into detecting game processes by creating fake processes with the exact names Discord expects. It reads Discord's public API to obtain process names, copies a base executable, renames it, and launches it in the background. Discord's game detection reads the Windows process list — it sees `TslGame.exe` and assumes you're playing PUBG. There is no technical mechanism to verify whether that process is actual or a renamed executable.

> **Educational purposes only.** This tool is provided to study how Discord's game detection system works and to explore process manipulation techniques. Use at your own risk and in compliance with all applicable terms of service.

---

## Fork Identity and Upstream Attribution

**Original author:** **Strykey** — orbitalhacker was originally developed by Strykey. The repository was later forked and is now maintained under `codee-vinay/orbshacker`.

**Current maintainer:** Community fork — `codee-vinay`

**Original contributors:** Daniel Pires, Pannenkoekisus — for foundational contributions and the original concept.

**Special thanks:** Daniel Pires, Pannenkoekisus — for foundational contributions and the original concept.

The fork `codee-vinay/orbshacker` is derived from the upstream repository `DanielPires2000/orbshacker`. All original code, concepts, and attribution remain the property of the original author. This fork provides maintenance updates, engineering improvements, and visual redesigns while preserving core functionality.

---

## 🛠️ Key highlights

- Tricks Discord's process-name-based game detection using fake executables
- Fetches the latest game list from Discord's official API
- Supports manual mode for custom executable names
- Multi-game support: run multiple fake processes simultaneously
- Built-in countdown timer with optional auto-delete
- Backup database fallback when Discord's API is unavailable
- Steam Quest Mode for games requiring Steam ownership verification
- SHA-256 verified update pipeline for safe releases
- 328 automated tests across 13 test modules
- Premium terminal UI redesign with VT-aware color rendering
- AppData‑based configuration survives packaged builds

---

## Original Orbshacker vs Our Fork

A detailed comparison of the original project versus our maintenance and engineering work.

| Area | Original Orbshacker | Our Fork | Improvement |
|------|--------------------|----------|-------------|
| **Terminal UI and user experience** | Basic colored prompts with ANSI bytes when VT disabled | Redesigned premium Catppuccin Mocha‑inspired interface with animated banners, badges, width‑safe layout, and VT‑aware color rendering | Much richer visual experience; plain‑text fallback ensures basic terminals remain usable |
| **Windows terminal and ANSI/VT compatibility** | ANSI bytes output when VT disabled; no fallback | Stage 3 fix: zero ESC bytes when VT unavailable; `_colors()` abstraction provides `_NoColors` stand‑ins; VT detection cached at startup | Guaranteed zero‑byte ESC output on unsupported consoles; consistent behavior across Windows configurations |
| **Configuration storage and persistence** | Direct `settings.py` editing; no migration path from legacy `settings.json` | Safer AppData‑based persistence with migration, malformed‑config detection, and no‑clobber publishing | Configuration survives packaged builds; malformed configs are preserved and warned about, never silently overwritten |
| **Configuration validation and error handling** | Minimal validation; settings imported directly from `settings.py` | `_normalised_settings()` coerces types, validates ranges, and records warnings instead of crashing | `AUTO_DELETE` is guaranteed `bool`; `TIMER_MINUTES` clamped to 1‑1440; folder paths resolved safely; warnings logged per‑load |
| **Application startup reliability** | `auto_update()` could crash on missing checksum; no backup strategy | `_atomic_publish()`, `_migrate_json_if_safe()`, backup `.old` executable, embedded settings fallback | Update pipeline is crash‑safe; configuration migration never overwrites user data; failed updates restore the previous executable |
| **Discord database loading and fallback behavior** | Single Discord API source; no fallback | Backup GitHub archive (`GITHUB_BACKUP_URL`), lazy DB loading, `_ensure_loaded()` with `DatabaseLoadError` handling | Tool keeps working when Discord's API is temporarily unavailable; failure is detected and reported cleanly |
| **Steam-related functionality and manifest handling** | Not present | Steam Quest Mode: auto‑fetch from SteamCMD API, generate `appmanifest_*.acf`, place fake executables in `steamapps/common/` | Enables orb quests for games that require Steam ownership verification (Marathon, Toxic Commando) |
| **Updater reliability, checksum verification, and recovery** | Not documented | Full SHA‑256 verified update pipeline: download → verify checksum → validate MZ header → same‑volume staging → atomic `os.replace()` → backup `.old` on failure | Users can safely update; corrupted or mismatched downloads are rejected and the previous executable is preserved |
| **Windows executable packaging** | PyInstaller build from workflow; no reproducible hash documented | Verified `pyinstaller --onefile --console` from Git root; reproducible SHA‑256 checksums (`0be4b0a3c79f7b2e329bc24ec72fff518663878ae0906bae7da621b873f14f1e`) | Build is reproducible; checksum lets users verify they have the exact build that passed the test suite |
| **Automated testing and CI coverage** | Minimal test coverage | 328 tests across 13 test modules; CI matrix runs on every PR; zero failures on current fork | Baseline failure confirmed on upstream commit `8d7e229` is an upstream issue, not introduced by this fork; no regressions |
| **Developer experience and maintainability** | Module imports could fail in frozen mode; `settings.py` treated as user config | `_load_settings()` priority chain: embedded → AppData → legacy → defaults → `settings.py` (dev only); `_normalised_settings()` validates on every load; frozen mode returns in‑memory defaults instead of importing mutable `settings.py` | Configuration works identically in source and packaged modes; no "works on my machine" bugs |

---

## Engineering Improvements

### Safer AppData‑based configuration for packaged Windows builds

When the application is frozen (packaged with PyInstaller), configuration is no longer read from the mutable `settings.py` module beside the executable. Instead, `_load_settings()` follows this priority chain:

1. **Embedded settings** baked into the PyInstaller executable at build time
2. **`%LOCALAPPDATA%\Orbshacker\settings.json`** — the preferred persistent location
3. **Legacy `settings.json` beside the executable** — attempted migration via `_migrate_json_if_safe()`
4. **In‑memory defaults** (`CHOSEN_FOLDER: Desktop`, `AUTO_DELETE: False`, `TIMER_MINUTES: 15`)
5. **`settings.py`** only in development mode

This chain ensures that configuration survives a rebuild, never clobbers existing user data, and gracefully falls back when paths are unavailable.

### Safe migration from legacy configuration files

The `_migrate_json_if_safe(src_path, dst_path)` function:

- Reads and validates the source file
- Only when the source is valid JSON does it attempt atomic no‑clobber publication to the destination
- If the destination already exists, it is left untouched and the function returns `False`
- If the source is malformed, both files are left untouched and `False` is returned
- Uses exclusive file creation (`O_CREAT \| O_EXCL`) to avoid name collisions in concurrent operations

The `_atomic_publish(dst_path, tmp_path, data)` helper writes to a temp file then `os.rename()` (atomic on the same filesystem), ensuring the destination is never left in a partial state.

### Protection against overwriting existing user configuration

The no‑clobber design means the AppData `settings.json` is only written if it does not already exist. If a user already has a valid config, it is preserved across builds and runs. Warnings (`SETTINGS_WARNINGS`) are logged when malformed configs are detected, but the original file is never deleted or overwritten.

### Configuration value normalization and validation

Three coerce helpers ensure settings are always safe to consume:

- `_coerce_bool(name, value, default)` — accepts `bool`, `int` 0/1, and case‑insensitive strings (`true`/`false`/`yes`/`no`/`1`/`0`); anything else falls back to `default` with a warning
- `_coerce_timer_minutes(name, value, default)` — clamps to 1‑1440 minutes; out‑of‑range numbers get a warning and are clamped
- `_coerce_folder(name, value, default)` — preserves `Path` objects, defaults to `Desktop` when the value is empty or the literal `"Desktop"`

These are applied by `_normalised_settings()`, which returns a validated `dict` on every load.

### Better startup error reporting

- `DiscordGamesDB.ensure_loaded()` raises `DatabaseLoadError` on failure; the main loop catches it and exits with code 1 after printing `[ERROR]`
- `_show_settings_warnings()` prints collected warnings at import without blocking startup
- Every failure path either self‑corrects (fallback to defaults) or reports a clear message and exits cleanly

### Improved terminal rendering and plain‑text fallback

The `_colors()` abstraction returns `Colors` when VT is available and `_NoColors` (empty strings) when it is not. This means:

- On terminals with VT enabled: premium cyan/bold colors, badges, animated banners
- On terminals without VT: plain text with original bracketed prefixes (`[OK]`, `[ERROR]`, etc.) — zero ESC bytes, no crash

VT detection runs once at module load and is cached via `_get_vt_cached()` / `_reset_vt_cache()`.

### Windows console compatibility

- `_detect_windows_vt()` uses the documented Win32 `GetConsoleMode()` contract
- Falls back to `False` on any exception (non‑Windows platforms, missing handles, etc.)
- `is_vt_supported()` caches the result; `_reset_vt_cache()` forces a fresh probe for tests

### Safer updater download verification and recovery behavior

The update pipeline in `updater.py`:

1. Fetches the latest GitHub Release
2. Compares remote version against current `config.VERSION`
3. Finds the `.exe` asset and its mandatory `.sha256` checksum asset
4. Downloads both, verifies the SHA‑256 match
5. Validates the MZ header of the downloaded executable
6. Stages the new exe into the installation directory (same volume, via `os.replace()`)
7. Replaces the running executable: rename current to `.old`, move new exe into place, launch, exit
8. If any step fails, the `.old` backup is preserved and the user is informed

The checksum is the authoritative integrity check; server‑declared `Content-Length` is secondary.

### Windows/Linux CI and automated regression tests

- 328 tests across 13 test modules
- 10 subtests (terminal VT‑awareness)
- CI matrix runs on every PR; zero failures on current fork; 1 pre‑existing baseline failure confirmed on upstream commit `8d7e229` is an upstream issue
- Full suite: `python -m pytest . -q → 328 passed, 10 subtests passed`

### Release build metadata and SHA‑256 checksums

- Build from Git root: `pyinstaller --onefile --name orbshacker --console --add-data "settings.py;." --collect-all orbshacker orbshacker.py`
- Produces `dist/orbshacker.exe` (16,376,477 bytes)
- SHA‑256: `0be4b0a3c79f7b2e329bc24ec72fff518663878ae0906bae7da621b873f14f1e`
- This hash is specific to the current build, not a permanent release checksum (the build pipeline generates a fresh hash on every release). Users can verify their own build with `Get-FileHash -Algorithm SHA256 .\\orbshacker.exe`.

---

## Terminal UI Redesign

The terminal UI was redesigned in commit `5746274` with the following characteristics:

- **Dark Catppuccin Mocha‑inspired presentation** — semantic color roles (`ACCENT: CYAN`, `SECONDARY: MAGENTA`, `SUCCESS: GREEN`, `WARNING: YELLOW`, `ERROR: RED`)
- **Cyan, lavender, and semantic status colors** — used for banners, badges, and status prefixes
- **Improved spacing and menu alignment** — `print_menu()` uses a two‑column layout with descriptions when the terminal is wide enough (`width >= _MENU_DESCRIPTION_WIDTH = 56`)
- **Clear status indicators** — `[OK]`, `[ERROR]`, `[!]`, `[*]`, `[UPDATE]` prefixes are turned into styled badges when VT is available, and plain text when it is not
- **VT‑aware color rendering** — `_colors()` returns the `Colors` palette or `_NoColors` stand‑ins based on `_get_vt_cached()`; the detection is cached at module load
- **Plain‑text fallback when ANSI/VT is unavailable** — the original bracketed prefixes are preserved verbatim; the application never crashes on unsupported terminals
- **Preserved numeric menu navigation and existing workflow** — all 5 menu options (`1`‑`5`) work identically; only the visual presentation changes

This is a visual and usability redesign, not a replacement of the application's underlying functionality. The core game‑spoofing loop, Discord API integration, and Steam Quest Mode are entirely unchanged.

---

## Preserved Original Functionality

The following core capabilities remain **exactly** as in the original upstream project:

- **Game process spoofing** — creates fake processes with the exact names Discord expects; tricks Discord's process‑name‑based detection
- **Discord API game list** — fetches the latest detectable game list from Discord's official API; smart search by name or abbreviation
- **Manual mode** — enter a custom executable name if it is not in the database
- **Self‑executing timer & embedded config** — faked game processes run the countdown timer when double‑clicked; `AUTO_DELETE` cleans up on completion
- **Multi‑game support** — run multiple fake processes simultaneously; each runs independently; Discord sees all of them
- **Backup database** — falls back to GitHub archive if Discord's API is unavailable
- **Educational purposes disclaimer** — clearly stated in the README and in‑app credits

**Original author:** **Strykey** — orbitalhacker was originally developed by Strykey. The repository was later forked and is now maintained under `codee-vinay/orbshacker`.

**Original contributors:** Daniel Pires, Pannenkoekisus — for foundational contributions and the original concept.

**What we improved:** Terminal UI redesign, AppData‑based configuration, safe migration, VT‑aware prompt, Steam Quest Mode, updater with SHA‑256 verification, 328‑test suite, reproducible packaging, and configuration normalization.

**What remains from the original:** All core game‑spoofing functionality, Discord API integration, manual mode, timer & auto‑delete, multi‑game support, backup DB, educational disclaimer.

---

## 📋 Installation (Windows)

```powershell
git clone https://github.com/codee-vinay/orbshacker.git
cd orbshacker
pip install -r requirements.txt
```

> **Prerequisites:** Python 3.7 or higher, Windows, Internet connection for database fetching. Discord must be running for the spoofer to work.

---

## ⚙️ Configuration

User-editable values live in `settings.py` at the project root (development) or `%LOCALAPPDATA%\Orbshacker\settings.json` (packaged/frozen mode). The application version comes from the git tag used for the build and is not user‑configurable; changing it manually would break update detection.

### Settings

| Key | Default | Description |
|-----|---------|-------------|
| `AUTO_DELETE` | `False` | Auto‑cleanup faked executables and Steam manifests on timer completion |
| `TIMER_MINUTES` | `15` | Countdown duration for each fake process |
| `CHOSEN_FOLDER` | `Desktop` | Base folder for faked executables (relative to user profile) |

### Configuration location and loading priority

The application loads settings in this priority order:

1. **Embedded executable** — settings baked into the PyInstaller binary at build time
2. **`%LOCALAPPDATA%\Orbshacker\settings.json`** — the preferred persistent location for packaged builds
3. **Legacy `settings.json` beside the executable** — attempted migration via `_migrate_json_if_safe()`; only applied if the destination does not already exist
4. **In‑memory defaults** (`CHOSEN_FOLDER: Desktop`, `AUTO_DELETE: False`, `TIMER_MINUTES: 15`)
5. **`settings.py`** — development only; imported at module load time for ease of editing during source development

The `_normalised_settings()` function validates every loaded value: `AUTO_DELETE` is coerced to `bool`, `TIMER_MINUTES` is clamped to 1‑1440, and `CHOSEN_FOLDER` is resolved to a `Path` defaulting to `Desktop`. Warnings are logged for any value that required coercion.

---

## 🚀 Usage

```powershell
python -m orbshacker
```

### Menu options

| Key | Action |
|-----|--------|
| `1` | Search Discord database by name or abbreviation |
| `2` | Manual mode, enter a custom executable name |
| `3` | Steam Quest Mode, generate Steam manifests |
| `4` | Credits and project info |
| `5` | Exit |

### Completing all quests in 15 minutes

1. Launch the tool
2. Select your first game
3. After the process is launched, press **Enter** to return to the main menu
4. Select another game
5. Repeat as many times as needed — no need to open multiple windows
6. Every fake process runs in parallel; Discord detects all of them simultaneously
7. Wait 15 minutes
8. Close everything when done

---

## 🛠️ Building the Windows Executable

The release workflow (`.github/workflows/release.yml`) builds a standalone Windows executable:

1. A Git tag matching `v*` triggers the workflow
2. Tests run (`python -m pytest -q`)
3. PyInstaller builds `orbshacker.exe` with `--onefile --console --add-data "settings.py;." --collect-all orbshacker`
4. SHA‑256 checksum is generated
5. Release is published as a GitHub Release asset

**To rebuild locally from the Git root:**

```powershell
pyinstaller --onefile --name orbshacker --console --add-data "settings.py;." --collect-all orbshacker orbshacker.py
```

The executable appears in `dist/orbshacker.exe`.

**Current build:** `dist/orbshacker.exe` — 16,376,477 bytes — SHA‑256: `0be4b0a3c79f7b2e329bc24ec72fff518663878ae0906bae7da621b873f14f1e`

> This hash is specific to the current build; the build pipeline generates a fresh hash on every release. Users can verify their own build with `Get-FileHash -Algorithm SHA256 .\\orbshacker.exe`.

---

## 🧪 Testing

The current test suite results:

- **328 passed**, 10 subtests passed
- **Zero failures** on the current fork
- **13 test modules** containing the full test suite
- CI matrix runs on every pull request; zero current‑fork failures; 1 pre‑existing baseline failure on upstream commit `8d7e229` is an upstream issue not introduced by this fork

Full test command: `python -m pytest . -q`

---

## Troubleshooting

| Symptom | Likely cause | Resolution |
|---------|-------------|------------|
| `ImportError: cannot import name 'config' from 'orbshacker'` | Running from wrong directory; package structure not recognized | Run from `orbshacker/` directory: `python -m orbshacker` |
| Configuration not persisting | `settings.json` beside executable is legacy; AppData path may be unavailable | Check `%LOCALAPPDATA%\Orbshacker\settings.json`; malformed config is preserved and warned about |
| Steam Quest Mode not detecting a game | Game may not have a SteamCMD public API entry | Verify at <https://steamdb.info>; use manual mode with custom executable name |
| ANSI escape sequences appearing in plain‑text terminal | VT not enabled in console properties | Enable "Terminal hardware acceleration" or "Use VT sequences" in Windows Terminal properties |
| Update check fails on launch | Network restriction or outdated hash comparison | Ensure outbound HTTPS is allowed; compare against the latest GitHub Release SHA‑256 |
| `ImportError` on frozen executable | Running from source; update skipped silently | Run the compiled executable; update only when `sys.frozen` is `True` |

---

## Known Limitations

- Tool only works on Windows (process‑name‑based Discord detection is Windows‑specific)
- Discord must be running for the spoofer to detect process names
- Games requiring anti‑cheat verification beyond process-name matching are not supported
- Steam Quest Mode depends on publicly available SteamCMD API entries; some games may not have entries
- Update checksums are build‑specific and regenerated on every release; prior release hashes must not be reused

---

## 👥 Credits and License

**Original author:** **Strykey** — orbitalhacker was originally developed by Strykey. The repository was later forked and is now maintained under `codee-vinay/orbshacker`.

**Current maintainer:** Community fork — `codee-vinay`

**Special thanks:** Daniel Pires, Pannenkoekisus — for foundational contributions and the original concept.

### License

> **GPL v3. Attribution required.** Modified versions must also be GPL v3. Source code must be provided with any distribution. Commercial use is strictly prohibited. See [LICENSE](./LICENSE) for full terms.

> **Educational purposes only.** No commercial use. Users are solely responsible for compliance with Discord's Terms of Service and all applicable laws. The developers are not responsible for any consequences resulting from use of this software. Use at your own risk.

---

<div align="center">

![orbshacker terminal](https://capsule-render.vercel.app/api?type=rect&color=0a0a0a,60:0d1f0d,100:1a4a1a&height=32&section=footer&text=orbshacker&fontSize=20&fontColor=4ade80)

</div>

---

- **Repository:** <https://github.com/codee-vinay/orbshacker>
- **Issues:** Use the [GitHub Issues](https://github.com/codee-vinay/orbshacker/issues) tracker
- **Original repository:** <https://github.com/DanielPires2000/orbshacker>