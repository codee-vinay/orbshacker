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
    """ANSI palette approximating Catppuccin Mocha with the basic 16 colours.

    The original names stay for existing call sites; the semantic aliases below
    describe the roles the premium theme assigns to them.
    """

    RESET   = '\033[0m'
    BOLD    = '\033[1m'
    DIM     = '\033[2m'
    RED     = '\033[91m'
    GREEN   = '\033[92m'
    YELLOW  = '\033[93m'
    BLUE    = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN    = '\033[96m'
    WHITE   = '\033[97m'
    GRAY    = '\033[90m'

    # Semantic roles (teal/sky accent, lavender secondary).
    ACCENT    = CYAN
    SECONDARY = MAGENTA
    SUCCESS   = GREEN
    WARNING   = YELLOW
    ERROR     = RED
    INFO      = CYAN
    MUTED     = GRAY
    TEXT      = WHITE
    BORDER    = GRAY


class _NoColors:
    """Empty stand-ins used when the terminal cannot render ANSI sequences."""

    RESET = BOLD = DIM = RED = GREEN = YELLOW = BLUE = MAGENTA = CYAN = ""
    WHITE = GRAY = ""
    ACCENT = SECONDARY = SUCCESS = WARNING = ERROR = ""
    INFO = MUTED = TEXT = BORDER = ""


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

# Status prefixes used across the application, mapped to semantic badges.
_BADGE_RE = re.compile(r"^\[(OK|ERROR|!|\*|UPDATE)\]\s?")
_BADGE_LABELS = {
    "OK": "OK",
    "ERROR": "ERR",
    "!": "WARN",
    "*": "INFO",
    "UPDATE": "UPDATE",
}
_BADGE_COLORS = {
    "OK": Colors.GREEN,
    "ERROR": Colors.RED,
    "!": Colors.YELLOW,
    "*": Colors.CYAN,
    "UPDATE": Colors.MAGENTA,
}


def print_color(text: str, color: str = Colors.WHITE, bold: bool = False) -> None:
    """Print colored text, turning a known ``[TAG]`` prefix into a status badge.

    Plain-text mode prints *text* verbatim so legacy terminals keep the original
    bracketed prefixes.
    """
    if not _get_vt_cached():
        print(text)
        return

    # Leading newlines are layout, not message content: keep them exactly and
    # still recognise a status prefix that follows them.
    lead = text[: len(text) - len(text.lstrip("\n"))]
    core = text[len(lead):]
    style = Colors.BOLD if bold else ''

    match = _BADGE_RE.match(core)
    if match:
        token = match.group(1)
        label = _BADGE_LABELS[token]
        badge_color = _BADGE_COLORS[token]
        rest = core[match.end():]
        print(
            f"{lead}{Colors.BOLD}{badge_color}{label}{Colors.RESET} "
            f"{style}{color}{rest}{Colors.RESET}"
        )
        return

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
    frame = Colors.GRAY if _get_vt_cached() else ""

    border = f"{frame}+{'-' * (effective - 2)}+{reset}"

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
            f"{frame}|{reset}"
            f"{' ' * left}{bold}{col}{chunk}{reset}{' ' * right}"
            f"{frame}|{reset}"
        )

    print(f"\n{border}")
    for line in lines:
        print(line)
    print(f"{border}\n")


# ── Banner ───────────────────────────────────────────────────────────────────

# Width at which the full premium header replaces the compact one.
_BANNER_ADVANCED_WIDTH = 64
_BANNER_TAGLINE = "DESKTOP EXPERIENCE"
_BANNER_STATUS = "o READY"

_DATABASE_SOURCE = "Discord Official API + GitHub Archive"


def _banner_header(width: int, vt: bool) -> list[str]:
    """Premium header: spaced wordmark, tagline, version and a ready chip.

    Geometry is computed from plain text first, so colour escapes never change
    the visible width of a row.
    """
    box_width = min(width, _BANNER_ADVANCED_WIDTH)
    if box_width < 4:
        return []
    inner = box_width - 2
    border = "+" + "-" * inner + "+"

    brand = "O R B S H A C K E R"
    status = _BANNER_STATUS
    prefix = " " + brand
    pad = inner - len(prefix) - len(status) - 1

    if pad >= 1:
        if vt:
            row_one = (
                f"{Colors.GRAY}|{Colors.RESET}"
                f" {Colors.BOLD}{Colors.CYAN}{brand}{Colors.RESET}"
                f"{' ' * pad}"
                f"{Colors.BOLD}{Colors.GREEN}{status}{Colors.RESET} "
                f"{Colors.GRAY}|{Colors.RESET}"
            )
        else:
            row_one = "|" + prefix + " " * pad + status + " |"
    else:
        clipped = ("|" + prefix)[: box_width - 1].ljust(box_width - 1) + "|"
        row_one = f"{Colors.GRAY}{clipped}{Colors.RESET}" if vt else clipped

    meta_text = f" {_BANNER_TAGLINE}  .  v{config.VERSION}"[:inner].ljust(inner)
    row_two = (
        f"{Colors.GRAY}|{Colors.RESET}"
        f"{Colors.GRAY}{meta_text}{Colors.RESET}"
        f"{Colors.GRAY}|{Colors.RESET}"
        if vt
        else "|" + meta_text + "|"
    )

    border_line = f"{Colors.GRAY}{border}{Colors.RESET}" if vt else border
    return [border_line, row_one, row_two, border_line]


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
        ("Developer:", config.DEVELOPER, Colors.CYAN),
        ("Version:", config.VERSION, Colors.WHITE),
        ("Database:", _DATABASE_SOURCE, Colors.GREEN),
    ]

    key_width = max(len(label) for label, _, _ in fields) + 2
    lines = []
    # Below twelve columns a label such as "Developer:" would be chopped mid
    # word, so readability wins over exact fitting at those pathological sizes.
    wrap_width = max(width, _BANNER_MIN_INFO_WIDTH)
    for label, value, value_color in fields:
        plain = f"  {label:<{key_width}}{value}"
        if len(plain) <= wrap_width:
            if vt:
                lines.append(
                    f"  {Colors.GRAY}{label:<{key_width}}{Colors.RESET}"
                    f"{value_color}{value}{Colors.RESET}"
                )
            else:
                lines.append(plain)
            continue

        # Keep wrapped values aligned under the value column when there is room;
        # on pathological widths readability of the label wins.
        if wrap_width > key_width + 10:
            chunks = textwrap.wrap(
                plain,
                width=wrap_width,
                subsequent_indent=" " * (2 + key_width),
            )
        else:
            chunks = textwrap.wrap(plain, width=wrap_width)

        for chunk in chunks or [plain[:wrap_width]]:
            lines.append(f"{Colors.GRAY}{chunk}{Colors.RESET}" if vt else chunk)
    return lines


def print_banner() -> None:
    """Display the Orbshacker header plus developer, version and database details."""
    width = get_console_width()
    if width <= 0:
        # A terminal that reports no usable width must not silence the banner.
        width = 80

    vt = _get_vt_cached()

    if width >= _BANNER_ADVANCED_WIDTH:
        header = _banner_header(width, vt)
    elif width >= 4:
        header = _compact_banner(width)
    else:
        header = []

    for line in header + _banner_info_lines(width, vt):
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
    answer = input(
        f"\n{Colors.BOLD}{Colors.ACCENT}{prompt}{Colors.RESET}"
        f" {Colors.MUTED}[Y/n]{Colors.RESET}: "
    ).strip().lower()
    return answer in ('', 'y', 'yes')


MENU_ITEMS = (
    ("1", "Search Discord database", "Official API catalogue"),
    ("2", "Manual mode", "Custom executable"),
    ("3", "Steam Quest Mode", "AppID manifests"),
    ("4", "Credits & Info", "About this build"),
    ("5", "Exit", "Close Orbshacker"),
)

# Two-column menu rows only appear when there is room for the descriptions.
_MENU_DESCRIPTION_WIDTH = 56


def _fit_label(text: str, room: int) -> str:
    """Trim a menu label to *room* columns, preferring a word boundary."""
    if room <= 0:
        return ""
    if len(text) <= room:
        return text
    clipped = text[:room]
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped


def _section_heading(title: str, width: int) -> None:
    """Print a section label followed by a muted rule."""
    C = _colors()
    rule = "-" * max(min(width - 4, 56), 0)
    print(f"\n  {C.BOLD}{C.ACCENT}{title}{C.RESET}")
    print(f"  {C.BORDER}{rule}{C.RESET}")


def print_menu(selected: int | None = None) -> None:
    """Display the main menu, highlighting *selected* when supplied."""
    C = _colors()
    width = get_console_width()
    show_descriptions = width >= _MENU_DESCRIPTION_WIDTH
    label_width = max(len(label) for _, label, _ in MENU_ITEMS) + 2
    rule = "-" * max(min(width - 4, 56), 0)

    _section_heading("MAIN MENU", width)
    label_room = max(width - 8, 0)
    for index, (number, label, description) in enumerate(MENU_ITEMS, start=1):
        active = selected == index
        marker = ">" if active else " "
        number_text = f"{number:>2}"
        if show_descriptions:
            label_text = f"{label:<{label_width}}"
        else:
            label_text = _fit_label(label, label_room)

        if active:
            line = f"  {C.BOLD}{C.ACCENT}{marker} {number_text}  {label_text}{C.RESET}"
        else:
            line = (
                f"  {C.MUTED}{marker}{C.RESET} {C.TEXT}{number_text}{C.RESET}"
                f"  {label_text}"
            )
        if show_descriptions:
            line += f"{C.ACCENT if active else C.MUTED}{description}{C.RESET}"
        print(line)
    print(f"  {C.BORDER}{rule}{C.RESET}")


def print_selected(choice: str) -> None:
    """Echo the chosen menu entry so the active option is unmistakable."""
    C = _colors()
    try:
        index = int(choice)
    except (TypeError, ValueError):
        return
    if not 1 <= index <= len(MENU_ITEMS):
        return
    number, label, _ = MENU_ITEMS[index - 1]
    print(
        f"  {C.ACCENT}>{C.RESET} {C.BOLD}{label}{C.RESET}"
        f" {C.MUTED}({number}){C.RESET}"
    )


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
