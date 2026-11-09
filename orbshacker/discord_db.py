"""
discord_db.py – DiscordGamesDB class and database search mode.
"""

import logging
from collections.abc import Mapping
from typing import TypedDict
import time

from . import config
from .path_utils import sanitize_relative_path
from .faker import GameFaker
from .ui import (
    Colors, print_color, print_boxed_title,
    loading_animation, ask_confirm,
)
from .net import fetch_json
from .errors import NetworkError, DatabaseLoadError


class ExecutableEntry(TypedDict, total=False):
    os: str
    name: str


class GameRecord(TypedDict, total=False):
    id: str
    name: str
    aliases: list[str]
    executables: list[ExecutableEntry]


logger = logging.getLogger(__name__)


def _normalise_game_id(value: object) -> str | None:
    """Return a usable id: a non-empty string, or an int converted to string.

    Booleans are rejected explicitly because ``bool`` is an ``int`` subclass.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def _normalise_aliases(value: object) -> list[str]:
    """Return only non-empty strings from *value*, or ``[]`` if malformed."""
    if not isinstance(value, list):
        return []
    aliases: list[str] = []
    for alias in value:
        if isinstance(alias, str):
            cleaned = alias.strip()
            if cleaned:
                aliases.append(cleaned)
    return aliases


def _normalise_executables(value: object) -> list[ExecutableEntry]:
    """Return only usable executable entries, or ``[]`` if malformed."""
    if not isinstance(value, list):
        return []
    executables: list[ExecutableEntry] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            continue
        os_name = entry.get("os")
        exe_name = entry.get("name")
        if not isinstance(os_name, str) or not isinstance(exe_name, str):
            continue
        os_name, exe_name = os_name.strip(), exe_name.strip()
        if os_name and exe_name:
            executables.append({"os": os_name, "name": exe_name})
    return executables


def normalise_game(raw: object) -> GameRecord | None:
    """Return a validated copy of *raw*, or ``None`` if it is unusable.

    ``id`` and ``name`` are essential; aliases and executables are optional
    and degrade to empty lists when missing or malformed. The input payload
    is never mutated.
    """
    if not isinstance(raw, Mapping):
        return None
    game_id = _normalise_game_id(raw.get("id"))
    raw_name = raw.get("name")
    if game_id is None or not isinstance(raw_name, str):
        return None
    name = raw_name.strip()
    if not name:
        return None
    return {
        "id": game_id,
        "name": name,
        "aliases": _normalise_aliases(raw.get("aliases")),
        "executables": _normalise_executables(raw.get("executables")),
    }


def validate_catalogue(payload: object, *, source_label: str) -> list[GameRecord]:
    """Validate a full payload before it may replace the live catalogue.

    Raises ``DatabaseLoadError`` when the payload is not a list or contains
    no usable records, so the caller can fall back to another source.
    """
    if not isinstance(payload, list):
        raise DatabaseLoadError(f"{source_label} payload is not a list of games")

    games: list[GameRecord] = []
    rejected = 0
    for raw in payload:
        game = normalise_game(raw)
        if game is None:
            rejected += 1
            continue
        games.append(game)

    if rejected:
        logger.debug("%s: rejected %d malformed game record(s)", source_label, rejected)
    if not games:
        raise DatabaseLoadError(f"{source_label} contained no usable games")
    return games


class DiscordGamesDB:
    """Loads and searches the Discord-detectable games database."""

    def __init__(self):
        self.games: list[GameRecord] = []
        self.source: str | None = None
        self.last_error: str | None = None
        self._loaded = False

    def ensure_loaded(self) -> None:
        """Load the catalogue on first use.

        A successful load is idempotent; a failed load raises
        ``DatabaseLoadError`` and leaves the instance retryable, with any
        previously loaded catalogue untouched.
        """
        if self._loaded:
            return
        self._load()
        self.last_error = None
        self._loaded = True

    def _load(self) -> None:
        """Load games from the Discord API or the GitHub backup.

        The catalogue is replaced only after a source validates completely,
        so a failed reload keeps the last known-good games.
        """
        print_color("\n[*] Loading games database...", Colors.YELLOW)

        failures: list[str] = []

        try:
            games = self._load_from_discord_api()
        except (NetworkError, DatabaseLoadError) as exc:
            failures.append(f"Discord Official API: {exc}")
            print_color(f"[!] Discord API error: {exc}", Colors.YELLOW)
            print_color("[!] Discord API unavailable, using GitHub backup...", Colors.YELLOW)
        else:
            self.games = games
            self.source = "Discord Official API"
            return

        try:
            games = self._load_from_github()
        except (NetworkError, DatabaseLoadError) as exc:
            failures.append(f"GitHub Backup: {exc}")
            print_color(f"[ERROR] GitHub backup failed: {exc}", Colors.RED)
        else:
            self.games = games
            self.source = "GitHub Backup"
            return

        self.last_error = "; ".join(failures)
        raise DatabaseLoadError(
            f"Could not load games database from any source. {self.last_error}"
        )

    def _load_from_discord_api(self) -> list[GameRecord]:
        """Fetch and validate the live catalogue from Discord."""
        loading_animation("Connecting to Discord API", 1.0)
        payload = fetch_json(config.DISCORD_API_URL, headers=config.DISCORD_HEADERS)
        games = validate_catalogue(payload, source_label="Discord Official API")
        print_color(f"[OK] Loaded {len(games)} games from Discord API", Colors.GREEN, bold=True)
        print_color("[*] Using LIVE database (fresh from Discord's servers)", Colors.CYAN)
        return games

    def _load_from_github(self) -> list[GameRecord]:
        """Fetch and validate the GitHub backup catalogue."""
        loading_animation("Fetching GitHub backup", 1.0)
        payload = fetch_json(config.GITHUB_BACKUP_URL, timeout=config.REQUEST_TIMEOUT_LONG)
        games = validate_catalogue(payload, source_label="GitHub Backup")
        print_color(f"[OK] Loaded {len(games)} games from GitHub", Colors.GREEN, bold=True)
        return games

    def search_games(self, query: str) -> list[GameRecord]:
        """Search for games by name or alias, returning up to MAX_SEARCH_RESULTS matches.

        Reads the loaded catalogue only; call ensure_loaded() first.
        """
        query_lower = query.lower()
        exact: dict[str, GameRecord] = {}
        partial: dict[str, GameRecord] = {}
        for game in self.games:
            game_id = game.get('id', '')
            name = game.get('name', '').lower()
            aliases = [a.lower() for a in game.get('aliases', [])]
            if query_lower == name or query_lower in aliases:
                if game_id:
                    exact.setdefault(game_id, game)
            elif query_lower in name or any(query_lower in a for a in aliases):
                if game_id:
                    partial.setdefault(game_id, game)
        merged = list(exact.values()) + [g for g in partial.values() if g.get('id') not in exact]
        return merged[:config.MAX_SEARCH_RESULTS]

    # ── executable helpers ────────────────────────────────────────────────────
    _SKIP_EXE_PATTERNS = [
        '_be.exe', '_eac.exe', 'launcher', 'unins',
        'crash', 'report', 'update', 'setup', 'install',
    ]

    def _filter_win32_exes(self, game: GameRecord, skip_patterns: bool = True) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for exe in game.get('executables', []):
            if not isinstance(exe, Mapping) or exe.get('os') != 'win32':
                continue
            name = exe.get('name')
            if not isinstance(name, str):
                continue
            if name.startswith('>'):
                name = name[1:]
            name = sanitize_relative_path(name)
            if not name or name in seen:
                continue
            if skip_patterns and any(p in name.lower() for p in self._SKIP_EXE_PATTERNS):
                continue
            seen.add(name)
            result.append(name)
        return result

    def _valid_win32_executables(self, game: GameRecord) -> list[tuple[str, bool]]:
        """Return sanitised ``(name, marked)`` pairs in payload order.

        Entries that are not usable Windows executables, whose name is blank
        before sanitisation, that duplicate an earlier name, or that match a
        skip pattern are excluded.
        """
        candidates: list[tuple[str, bool]] = []
        seen: set[str] = set()
        for exe in game.get('executables', []):
            if not isinstance(exe, Mapping) or exe.get('os') != 'win32':
                continue
            raw_name = exe.get('name')
            if not isinstance(raw_name, str):
                continue
            marked = raw_name.startswith('>')
            raw_name = raw_name[1:] if marked else raw_name
            if not raw_name.strip():
                continue
            name = sanitize_relative_path(raw_name)
            if not name or name in seen:
                continue
            if any(p in name.lower() for p in self._SKIP_EXE_PATTERNS):
                continue
            seen.add(name)
            candidates.append((name, marked))
        return candidates

    def get_win32_executable(self, game: GameRecord) -> str | None:
        """Prefer Discord's '>'-marked executable, else the first candidate."""
        candidates = self._valid_win32_executables(game)
        for name, marked in candidates:
            if marked:
                return name
        return candidates[0][0] if candidates else None

    def get_all_executables(self, game: GameRecord) -> list[str]:
        return self._filter_win32_exes(game, skip_patterns=False)


# ── Interactive UI ────────────────────────────────────────────────────────────

def _pick_discord_game(db: DiscordGamesDB, query: str) -> GameRecord | None:
    """Search the Discord DB and let the user choose a game."""
    loading_animation(f"Searching for '{query}'", 0.8)
    matches = db.search_games(query)
    if not matches:
        print_color(f"\n[ERROR] No games found for '{query}'", Colors.RED)
        print_color("[!] Try a different search term or abbreviation", Colors.YELLOW)
        time.sleep(config.SLEEP_LONG)
        return None

    print(f"\n{Colors.BOLD}{Colors.GREEN}Found {len(matches)} game(s):{Colors.RESET}\n")
    print(f"{Colors.GRAY}{'─' * 75}{Colors.RESET}")
    for idx, game in enumerate(matches, 1):
        name    = game.get('name', 'Unknown')
        game_id = game.get('id', 'N/A')
        aliases = game.get('aliases', [])
        print(f"{Colors.BOLD}{Colors.CYAN}{idx:2d}.{Colors.RESET} {Colors.WHITE}{name}{Colors.RESET}")
        if aliases:
            alias_str = ', '.join(aliases[:3])
            if len(aliases) > 3:
                alias_str += f" (+{len(aliases) - 3} more)"
            print(f"    {Colors.GRAY}Aliases: {alias_str}{Colors.RESET}")
        print(f"    {Colors.GRAY}ID: {game_id}{Colors.RESET}")
        if idx < len(matches):
            print(f"{Colors.GRAY}{'─' * 75}{Colors.RESET}")
    print()

    raw = input(f"{Colors.BOLD}Select [1-{len(matches)}]{Colors.RESET} (or 'back'): ").strip()
    if raw.lower() in ('back', 'b', ''):
        return None
    try:
        idx = int(raw)
        if not 1 <= idx <= len(matches):
            raise ValueError
    except ValueError:
        print_color("\n[ERROR] Enter a valid number", Colors.RED)
        time.sleep(config.SLEEP_SHORT)
        return None
    return matches[idx - 1]


def _resolve_discord_exe(db: DiscordGamesDB, game: GameRecord) -> str | None:
    """Return the primary exe, prompting the user if none found."""
    exe_name = db.get_win32_executable(game)
    if exe_name:
        return exe_name
    print_color("\n[ERROR] No Windows executable found for this game", Colors.RED)
    print_color("[!] This game might not have a Windows version or executable data", Colors.YELLOW)
    if ask_confirm("Enter executable name manually?"):
        exe_name = input(f"{Colors.BOLD}Executable name:{Colors.RESET} ").strip()
        if not exe_name:
            print_color("\n[!] Operation cancelled", Colors.YELLOW)
            time.sleep(config.SLEEP_SHORT)
            return None
        return sanitize_relative_path(exe_name)
    return None


def database_mode(db: DiscordGamesDB, faker: GameFaker) -> None:
    """Database search mode."""
    print_boxed_title("DATABASE SEARCH", width=50, color=Colors.CYAN)
    print_color(f"[*] Database: {db.source}", Colors.CYAN)
    print_color(f"[*] Total games available: {len(db.games)}", Colors.GRAY)
    print_color("\n[*] Search by name or abbreviation", Colors.CYAN)
    print_color("[*] Examples: PUBG, Fortnite, League, Valorant, Minecraft", Colors.GRAY)
    print()

    query = input(f"{Colors.BOLD}Search{Colors.RESET} (or 'back'): ").strip()
    if query.lower() in ('back', 'b', ''):
        return

    selected = _pick_discord_game(db, query)
    if not selected:
        return

    loading_animation("Analysing game data", 0.8)
    exe_name = _resolve_discord_exe(db, selected)
    if not exe_name:
        return

    all_exes = db.get_all_executables(selected)

    print(f"\n{Colors.BOLD}Game Information:{Colors.RESET}")
    print(f"  Name:               {Colors.CYAN}{selected.get('name')}{Colors.RESET}")
    print(f"  ID:                 {Colors.GRAY}{selected.get('id')}{Colors.RESET}")
    print(f"  Primary Executable: {Colors.GREEN}{exe_name}{Colors.RESET}")
    if len(all_exes) > 1:
        print(f"  {Colors.GRAY}Other executables: {', '.join(all_exes[1:3])}{Colors.RESET}")
        if len(all_exes) > 3:
            print(f"  {Colors.GRAY}(+{len(all_exes) - 3} more executables available){Colors.RESET}")
    print(f"  Path: {Colors.GRAY}{faker.chosen_path / config.FAKE_EXE_DIR / exe_name}{Colors.RESET}")

    if not ask_confirm():
        print_color("\n[!] Operation cancelled", Colors.YELLOW)
        time.sleep(config.SLEEP_SHORT)
        return

    result = faker.create_fake_game(exe_name)
    if result:
        print()
        faker.launch_executable(result)
        print_color("\n[OK] Setup complete! Discord should detect the game.", Colors.GREEN, bold=True)
        print_color("[!] IMPORTANT: Discord MUST be running for the spoofing to work", Colors.YELLOW)
        print_color("[*] Keep the process running until quest is complete", Colors.CYAN)
        print_color("[*] TIP: Run this tool again to emulate another game simultaneously!", Colors.MAGENTA)

    input(f"\n{Colors.GRAY}Press Enter to continue...{Colors.RESET}")
