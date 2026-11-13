"""
ui.py – Terminal UI helpers: colors, banners, animations, menus, credits.
"""

import sys
import os
import re
import time
import ctypes
import textwrap
from ctypes import wintypes

from . import config


class Colors:
    RESET   = '\033[0m'
    BOLD    = '\033[1m'
    RED     = '\033[91m'
    GREEN   = '\033[92m'
    YELLOW  = '\033[93m'
    BLUE    = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN    = '\033[96m'
    WHITE   = '\033[97m'
    GRAY    = '\033[90m'


class _NoColors:
    """Empty stand-ins used when the terminal cannot render ANSI sequences."""

    RESET = BOLD = RED = GREEN = YELLOW = BLUE = MAGENTA = CYAN = WHITE = GRAY = ""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible_len(text: str) -> int:
    """Length of *text* as seen on screen (ANSI escape sequences excluded)."""
    return len(_ANSI_RE.sub("", text))


def _colors():
    """Return the ANSI palette, or empty stand-ins when VT is unavailable."""
    return Colors if _get_vt_cached() else _NoColors


# ── Windows VT100 support detection ──────────────────────────────────────────

STD_OUTPUT_HANDLE = -11
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

MAX_BOX_WIDTH = 80

# Narrowest width at which a metadata label still reads as one word.
_BANNER_MIN_INFO_WIDTH = 12

_VT_SUPPORT = None


def _reset_vt_cache() -> None:
    """Clear the cached VT detection result (used by tests)."""
    global _VT_SUPPORT
    _VT_SUPPORT = None


def _get_kernel32():
    """Indirection over ``ctypes.windll.kernel32`` so tests can inject a fake."""
    return ctypes.windll.kernel32


def _detect_windows_vt() -> bool:
    """Return True when the Windows console has VT processing enabled.

    Implements the documented Win32 contract: ``GetConsoleMode`` takes the
    console handle plus a pointer to a DWORD that receives the mode, and its
    BOOL return value reports whether the call succeeded.
    """
    if sys.platform != "win32":
        return False

    try:
        kernel32 = _get_kernel32()
    except Exception:
        return False

    try:
        kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.GetConsoleMode.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetConsoleMode.restype = wintypes.BOOL

        handle = kernel32.GetStdHandle(wintypes.DWORD(STD_OUTPUT_HANDLE & 0xFFFFFFFF))
        if (
            handle is None
            or handle == 0
            or handle == -1
            or handle == INVALID_HANDLE_VALUE
        ):
            return False

        mode = wintypes.DWORD(0)
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False

        return bool(mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except Exception:
        return False


def is_vt_supported() -> bool:
    """Return True when the terminal can render ANSI/VT sequences.

    Detection runs once and is cached (``_reset_vt_cache()`` forces a fresh
    probe). Windows uses the Win32 console-mode contract; other platforms
    probe ``isatty()``. Every failure mode resolves to ``False``.
    """
    global _VT_SUPPORT
    if _VT_SUPPORT is None:
        if sys.platform == "win32":
            _VT_SUPPORT = _detect_windows_vt()
        else:
            try:
                _VT_SUPPORT = os.isatty(sys.stdout.fileno())
            except Exception:
                _VT_SUPPORT = False
    return bool(_VT_SUPPORT)


def _get_vt_cached() -> bool:
    """Internal alias for :func:`is_vt_supported` (single shared cache)."""
    return is_vt_supported()


# ── Output helpers ───────────────────────────────────────────────────────────

def print_color(text: str, color: str = Colors.WHITE, bold: bool = False) -> None:
    """Print colored text."""
    if not _get_vt_cached():
        print(text)
        return
    style = Colors.BOLD if bold else ''
    print(f"{style}{color}{text}{Colors.RESET}")


def get_console_width() -> int:
    try:
        size = os.get_terminal_size(sys.stdout.fileno())
        return size.columns
    except Exception:
        try:
            env_width = os.environ.get("COLUMNS")
            if env_width and env_width.isdigit():
                return int(env_width)
        except Exception:
            pass
        return 80


# ── Boxed titles ─────────────────────────────────────────────────────────────

def print_boxed_title(title: str, width: int | None = 50, color: str = Colors.CYAN) -> None:
    """Print a boxed title that never exceeds the real terminal width.

    ``width`` defaults to 50 columns, preserving the original public default.
    Passing ``None`` explicitly asks for the terminal width instead. The
    effective width never exceeds the requested width (when supplied), the
    detected terminal width, or ``MAX_BOX_WIDTH``, so a caller asking for 65
    columns on a 40-column terminal gets a 40-column box, and asking for 5
    columns really yields a 5-column box.
    """
    term_width = get_console_width()
    if term_width <= 0:
        return

    requested = term_width if width is None else width
    try:
        requested = int(requested)
    except (TypeError, ValueError):
        requested = term_width

    # Never wider than what the caller asked for, the terminal, or the maximum.
    effective = min(requested, MAX_BOX_WIDTH, term_width)
    if effective < 4:
        # Too narrow for a border; emit the title as plain text.
        print(title[:max(effective, 1)])
        return
    text_width = effective - 2

    bold = Colors.BOLD if _get_vt_cached() else ""
    reset = Colors.RESET if _get_vt_cached() else ""
    col = color if _get_vt_cached() else ""

    border = f"{bold}{col}+{'-' * (effective - 2)}+{reset}"

    if len(title) <= text_width:
        chunks = [title]
    else:
        chunks = textwrap.wrap(title, width=text_width) or [title[:text_width]]

    lines = []
    for chunk in chunks:
        chunk = chunk[:text_width]
        padding = text_width - len(chunk)
        left = padding // 2
        right = padding - left
        lines.append(
            f"{bold}{col}|{reset}"
            f"{' ' * left}{bold}{chunk}{reset}{' ' * right}"
            f"{bold}{col}|{reset}"
        )

    print(f"\n{border}")
    for line in lines:
        print(line)
    print(f"{border}\n")


# ── Banner ───────────────────────────────────────────────────────────────────

_BANNER_ART = [
    "_____ _____ _____ _____    _____ _____ _____ _____ _____ ",
    "|     | __  | __  |   __|  |  |  |  _  |     |  |  |   __| __  |",
    "|  |  |    -| __ -|__   |  |     |     |   --|    -|   __|    -|",
    "|_____|__|__|_____|_____|  |__|__|__|__|_____|__|__|_____|__|__",
]

_DATABASE_SOURCE = "Discord Official API + GitHub Archive"


def _banner_fits_width(banner_lines: list, width: int) -> bool:
    """True when every visible line fits inside *width* columns."""
    return all(_visible_len(line) <= width for line in banner_lines)


def _compact_banner(width: int) -> list:
    """Condensed, still-recognisable banner used on narrow terminals."""
    inner = max(width - 4, 0)
    label = "ORBSHACKER"[:inner]

    padding = inner - len(label)
    left = padding // 2
    right = padding - left

    # "|" + space + left pad + label + right pad + space + "|" spans exactly
    # `width` columns, so the wordmark row is closed like the rules around it.
    body = "|" + " " + " " * left + label + " " * right + " " + "|"
    body = body[:width]

    rule = ("+" + "-" * max(width - 2, 0) + "+")[:width]
    return [rule, body, rule]


def _banner_info_lines(width: int, vt: bool) -> list:
    """Developer / version / database lines, wrapped instead of dropped."""
    fields = [
        ("Developer", config.DEVELOPER, Colors.CYAN),
        ("Version", config.VERSION, Colors.WHITE),
        ("Database", _DATABASE_SOURCE, Colors.GREEN),
    ]

    lines = []
    # Below twelve columns a label such as "Developer:" would be chopped mid
    # word, so readability wins over exact fitting at those pathological sizes.
    wrap_width = max(width, _BANNER_MIN_INFO_WIDTH)
    for label, value, value_color in fields:
        plain = f"  {label}: {value}"
        if len(plain) <= wrap_width:
            if vt:
                lines.append(
                    f"  {Colors.GRAY}{label}: {Colors.RESET}"
                    f"{value_color}{value}{Colors.RESET}"
                )
            else:
                lines.append(plain)
            continue

        for chunk in textwrap.wrap(plain, width=wrap_width) or [plain[:wrap_width]]:
            lines.append(f"{Colors.GRAY}{chunk}{Colors.RESET}" if vt else chunk)
    return lines


def print_banner() -> None:
    """Display the ASCII banner plus developer, version and database details."""
    width = get_console_width()
    if width <= 0:
        # A terminal that reports no usable width must not silence the banner.
        width = 80

    vt = _get_vt_cached()

    if _banner_fits_width(_BANNER_ART, width):
        art = list(_BANNER_ART)
    elif width >= 4:
        art = _compact_banner(width)
    else:
        art = []

    for line in art + _banner_info_lines(width, vt):
        print(line)


# ── Animations, prompts, menus, credits ──────────────────────────────────────

def loading_animation(text: str, duration: float = 1.5) -> None:
    """Display loading animation."""
    frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    ascii_frames = ["|", "/", "-", "\\"]
    end_time = time.time() + duration
    i = 0
    use_ascii = not _get_vt_cached()
    while time.time() < end_time:
        if use_ascii:
            frame = ascii_frames[i % len(ascii_frames)]
        else:
            frame = f"{Colors.CYAN}{frames[i % len(frames)]}{Colors.RESET}"
        try:
            sys.stdout.write(f"\r{frame} {text}")
            sys.stdout.flush()
        except UnicodeEncodeError:
            # The console code page cannot render the braille frames. Switch to
            # ASCII for this and every later frame instead of freezing silently.
            use_ascii = True
            try:
                sys.stdout.write(f"\r{ascii_frames[i % len(ascii_frames)]} {text}")
                sys.stdout.flush()
            except Exception:
                pass
        except Exception:
            pass
        time.sleep(0.1)
        i += 1
    try:
        if _get_vt_cached():
            sys.stdout.write(f"\r{Colors.CYAN}{' ' * (len(text) + 5)}\r")
        else:
            sys.stdout.write(f"\r{' ' * (len(text) + 5)}\r")
        sys.stdout.flush()
    except Exception:
        pass


def ask_confirm(prompt: str = "Create and launch?") -> bool:
    """Ask a Y/n confirmation question. Returns True if user confirms."""
    if not _get_vt_cached():
        answer = input("\n" + prompt + " [Y/n]: ").strip().lower()
        return answer in ('', 'y', 'yes')
    answer = input(f"\n{Colors.BOLD}{prompt}{Colors.RESET} [Y/n]: ").strip().lower()
    return answer in ('', 'y', 'yes')


def print_menu() -> None:
    """Display main menu."""
    print_boxed_title("MAIN MENU", width=50, color=Colors.CYAN)
    C = _colors()
    print(f"  {C.BOLD}{C.GREEN}1.{C.RESET} Search Discord database (Official API)")
    print(f"  {C.BOLD}{C.GREEN}2.{C.RESET} Manual mode (custom executable)")
    print(f"  {C.BOLD}{C.YELLOW}3.{C.RESET} Steam Quest Mode  {C.YELLOW}[NEW - for Marathon, Toxic Commando…]{C.RESET}")
    print(f"  {C.BOLD}{C.GREEN}4.{C.RESET} Credits & Info")
    print(f"  {C.BOLD}{C.RED}5.{C.RESET} Exit\n")


def show_credits() -> None:
    """Display credits."""
    print_boxed_title("CREDITS", width=65, color=Colors.CYAN)
    C = _colors()
    credits_text = f"""
    {C.BOLD}Developer:{C.RESET} {C.CYAN}{config.DEVELOPER}{C.RESET}
    {C.BOLD}Version:{C.RESET}   {C.WHITE}{config.VERSION}{C.RESET}

    {C.BOLD}Description:{C.RESET}
    This tool works as a game process spoofer. It tricks Discord into
    thinking you're running a game by creating fake processes with the
    exact names Discord expects.
    
    {C.BOLD}IMPORTANT:{C.RESET} {C.RED}Discord MUST be running for this to work!{C.RESET}
    
    {C.BOLD}How it works (Game Spoofing):{C.RESET}
    1. Connects to Discord's official API to get the latest game list
    2. Finds the exact process name Discord expects for each game
    3. Copies exe.exe to Desktop/Win64/ and renames it to match
    4. Launches the fake process in background
    5. Discord scans running processes and detects the fake process name
    6. Discord thinks you're playing the game (process name match)
    7. The fake process must stay running for Discord to keep detecting it
    
    {C.BOLD}Steam Quest Mode (NEW):{C.RESET}
    Some games (Marathon, Toxic Commando…) require Discord to verify
    that Steam has at least partially downloaded them.
    Steam Quest Mode bypasses this by:
    1. Fetching app info automatically from SteamCMD public API
    2. Generating a fake appmanifest_<appid>.acf in your steamapps/ folder
    3. Placing the fake exe directly in steamapps/common/<installdir>/
    Discord then sees a valid Steam manifest + a running process = quest detected.
    
    {C.BOLD}Database Sources:{C.RESET}
    • Primary: Discord Official API
    • Backup:  GitHub Archive by Cynosphere
    
    {C.BOLD}Pro Tips:{C.RESET}
    • Use Steam Quest Mode for games not detected by modes 1 or 2
    • Find AppIDs at https://steamdb.info
    • The fake process must stay running for Discord to detect it
    
    {C.BOLD}{C.GREEN}Multi-Game Emulation:{C.RESET}
    • Run this tool multiple times to emulate multiple games at once
    • Complete ALL orb quests simultaneously in just 15 minutes
    
    {C.BOLD}{C.RED}WARNING - EDUCATIONAL PURPOSES ONLY{C.RESET}
    • Users are SOLELY responsible for compliance with Discord ToS
    • The developers are NOT responsible for any consequences
    • Use at your own risk
    
    {C.GRAY}Made by {config.DEVELOPER}{C.RESET}
    {C.GRAY}Press Enter to return to menu...{C.RESET}
"""
    print(credits_text)
    input()
