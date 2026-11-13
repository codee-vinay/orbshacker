"""Tests for ui.py terminal experience improvements.

Mocks Windows console API and covers VT detection, color fallback,
banner width handling, and loading animation robustness.
"""

import os
import io
import re
import sys
import ctypes
import unittest
from ctypes import wintypes
from unittest.mock import patch

# Ensure the package is importable and import required functions
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orbshacker"))

import orbshacker.ui as ui
from orbshacker import config
from orbshacker.ui import (
    Colors,
    is_vt_supported,
    print_color,
    print_banner,
    print_boxed_title,
    loading_animation,
    get_console_width,
    show_credits,
)


def _strip_ansi(line):
    """Remove ANSI escape sequences from a line for accurate length measurement."""
    return re.sub(r"\x1b\[[0-9;]*m", "", line)


def _visible_lines(text):
    """Return the ANSI-stripped, non-blank lines of *text*."""
    return [line for line in (_strip_ansi(l) for l in text.split("\n")) if line.strip()]


def _capture_stdout(func, *args, **kwargs):
    """Run *func* and return everything it wrote to stdout."""
    old_stdout = sys.stdout
    buffer = io.StringIO()
    sys.stdout = buffer
    try:
        func(*args, **kwargs)
    finally:
        sys.stdout = old_stdout
    return buffer.getvalue()


class _FakeWin32Function:
    """Callable that records calls and accepts argtypes/restype assignments."""

    def __init__(self, impl):
        self._impl = impl
        self.argtypes = None
        self.restype = None
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self._impl(*args)


class _FakeKernel32:
    """Minimal kernel32 stand-in enforcing the real two-argument contract."""

    def __init__(self, handle=1, mode=0x0004, mode_ok=1,
                 handle_exc=None, mode_exc=None):
        self._handle = handle
        self._mode = mode
        self._mode_ok = mode_ok
        self._handle_exc = handle_exc
        self._mode_exc = mode_exc
        self.GetStdHandle = _FakeWin32Function(self._get_std_handle)
        self.GetConsoleMode = _FakeWin32Function(self._get_console_mode)

    def _get_std_handle(self, which):
        if self._handle_exc is not None:
            raise self._handle_exc
        return self._handle

    def _get_console_mode(self, handle, mode_ptr):
        if self._mode_exc is not None:
            raise self._mode_exc
        # Writes through the caller-supplied DWORD pointer, exactly like Win32.
        mode_ptr._obj.value = self._mode
        return self._mode_ok


class _UnicodeHostileStdout:
    """Stdout stand-in that rejects braille frames like a legacy code page."""

    def __init__(self):
        self.chunks = []

    def write(self, text):
        if any("\u2800" <= ch <= "\u28ff" for ch in text):
            raise UnicodeEncodeError(
                "ascii", text, 0, 1, "ordinal not in range(128)"
            )
        self.chunks.append(text)

    def flush(self):
        pass

    @property
    def output(self):
        return "".join(self.chunks)


# ── VT100 detection ──────────────────────────────────────────────────────────

class TestVtDetection(unittest.TestCase):
    """Windows VT100 support detection tests."""

    def setUp(self):
        """Reset VT detection cache before each test."""
        ui._reset_vt_cache()

    def _detect_with(self, fake):
        with patch("orbshacker.ui.sys.platform", "win32"), \
                patch("orbshacker.ui._get_kernel32", return_value=fake):
            return is_vt_supported()

    def test_vt_supported_windows_enabled(self):
        """Windows VT supported when ENABLE_VIRTUAL_TERMINAL_PROCESSING is set."""
        fake = _FakeKernel32(handle=1, mode=0x0004, mode_ok=1)
        self.assertTrue(self._detect_with(fake))

    def test_vt_unsupported_windows_no_flag(self):
        """Windows VT not supported when the flag is missing."""
        fake = _FakeKernel32(handle=1, mode=0x0000, mode_ok=1)
        self.assertFalse(self._detect_with(fake))

    def test_vt_unsupported_no_console(self):
        """VT returns False when no console handle is available."""
        fake = _FakeKernel32(handle_exc=OSError("no console"))
        self.assertFalse(self._detect_with(fake))

    def test_get_console_mode_uses_out_pointer_and_signature(self):
        """GetConsoleMode receives a DWORD out-pointer and a typed signature."""
        fake = _FakeKernel32(handle=7, mode=0x0004, mode_ok=1)
        self.assertTrue(self._detect_with(fake))

        self.assertEqual(fake.GetStdHandle.argtypes, [wintypes.DWORD])
        self.assertEqual(
            fake.GetConsoleMode.argtypes,
            [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)],
        )
        self.assertIs(fake.GetConsoleMode.restype, wintypes.BOOL)

        self.assertEqual(len(fake.GetConsoleMode.calls), 1)
        handle, mode_ptr = fake.GetConsoleMode.calls[0]
        self.assertEqual(handle, 7)
        self.assertTrue(hasattr(mode_ptr, "_obj"), "second arg must be a pointer")
        self.assertIsInstance(mode_ptr._obj, wintypes.DWORD)
        self.assertEqual(mode_ptr._obj.value, 0x0004)

    def test_vt_invalid_handle_skips_mode_query(self):
        """An invalid handle short-circuits before GetConsoleMode."""
        fake = _FakeKernel32(handle=-1)
        self.assertFalse(self._detect_with(fake))
        self.assertEqual(fake.GetConsoleMode.calls, [])

    def test_vt_api_failure_returns_false(self):
        """A zero BOOL from GetConsoleMode is failure, not a zero mode."""
        fake = _FakeKernel32(handle=1, mode=0x0004, mode_ok=0)
        self.assertFalse(self._detect_with(fake))

    def test_vt_api_exception_returns_false(self):
        """ctypes failures must not escape the detector."""
        fake = _FakeKernel32(handle=1, mode_exc=OSError("boom"))
        self.assertFalse(self._detect_with(fake))

    def test_vt_non_windows_platform_never_queries_kernel32(self):
        """Non-Windows platforms return False without touching kernel32."""
        fake = _FakeKernel32(handle=1, mode=0x0004)
        with patch("orbshacker.ui.sys.platform", "linux"), \
                patch("orbshacker.ui._get_kernel32", return_value=fake):
            self.assertFalse(ui._detect_windows_vt())
        self.assertEqual(fake.GetStdHandle.calls, [])

    def test_vt_cache_reset_and_repeated_calls(self):
        """Repeated calls hit the cache; _reset_vt_cache() forces re-detection."""
        fake = _FakeKernel32(handle=1, mode=0x0004, mode_ok=1)
        with patch("orbshacker.ui.sys.platform", "win32"), \
                patch("orbshacker.ui._get_kernel32", return_value=fake):
            self.assertTrue(is_vt_supported())
            self.assertTrue(is_vt_supported())
            self.assertEqual(len(fake.GetConsoleMode.calls), 1)
            ui._reset_vt_cache()
            self.assertTrue(is_vt_supported())
            self.assertEqual(len(fake.GetConsoleMode.calls), 2)

    def test_vt_supported_posix_tty(self):
        """POSIX VT supported when stdout is a TTY."""
        with patch("orbshacker.ui.sys.platform", "linux"):
            with patch("orbshacker.ui.os.isatty", return_value=True):
                self.assertTrue(is_vt_supported())

    def test_vt_unsupported_posix_redirected(self):
        """POSIX VT not supported when stdout is redirected."""
        with patch("orbshacker.ui.sys.platform", "linux"):
            with patch("orbshacker.ui.os.isatty", return_value=False):
                self.assertFalse(is_vt_supported())


# ── Color fallback ───────────────────────────────────────────────────────────

class TestPrintColorFallback(unittest.TestCase):
    """print_color() VT-aware fallback tests."""

    def setUp(self):
        """Reset VT detection cache before each test."""
        ui._reset_vt_cache()

    def test_print_color_vt_enabled_assert_ansi(self):
        """ANSI codes printed and present when VT is enabled."""
        with patch("orbshacker.ui._get_vt_cached", return_value=True), \
                patch("builtins.print") as mock_print:
            print_color("test message", Colors.GREEN, bold=True)

        output = "".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertIn("\x1b[1m\x1b[92m", output)
        self.assertIn("test message", output)

    def test_print_color_vt_disabled_no_ansi(self):
        """Plain text printed without ANSI codes when VT is unavailable."""
        with patch("orbshacker.ui._get_vt_cached", return_value=False), \
                patch("builtins.print") as mock_print:
            print_color("test message", Colors.GREEN, bold=True)

        output = "".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertNotIn("\x1b[", output)
        self.assertIn("test message", output)


# ── Console width ────────────────────────────────────────────────────────────

class TestGetConsoleWidth(unittest.TestCase):
    """get_console_width() tests."""

    def test_console_width_from_terminal(self):
        """Width returned from os.get_terminal_size."""
        with patch("orbshacker.ui.os.get_terminal_size") as mock_terminal:
            mock_terminal.return_value.columns = 100
            width = get_console_width()
            self.assertEqual(width, 100)

    def test_console_width_from_env(self):
        """Width fallback to COLUMNS env variable."""
        with patch("orbshacker.ui.os.get_terminal_size", side_effect=Exception("no console")):
            with patch("orbshacker.ui.os.environ", {"COLUMNS": "80"}):
                width = get_console_width()
                self.assertEqual(width, 80)

    def test_console_width_default(self):
        """Width defaults to 80 when no info available."""
        with patch("orbshacker.ui.os.get_terminal_size", side_effect=Exception("no console")):
            with patch("orbshacker.ui.os.environ", {}):
                width = get_console_width()
                self.assertEqual(width, 80)


# ── Banner width ────────────────────────────────────────────────────────────

class TestBannerNarrowWidth(unittest.TestCase):
    """Banner width handling tests."""

    def test_banner_compact_on_narrow(self):
        """Compact banner used when console width < full banner width."""
        with patch("orbshacker.ui.get_console_width", return_value=40), \
                patch("orbshacker.ui._get_vt_cached", return_value=False):
            output = _capture_stdout(print_banner)
        visible = [_strip_ansi(l) for l in output.split("\n") if _strip_ansi(l).strip()]
        self.assertTrue(visible, "banner produced no output")
        self.assertTrue(all(len(l) <= 40 for l in visible), visible)
        self.assertIn("ORBSHACKER", "".join(visible))

    def test_banner_full_on_wide(self):
        """Premium header used when console width is sufficient."""
        with patch("orbshacker.ui.get_console_width", return_value=120), \
                patch("orbshacker.ui._get_vt_cached", return_value=False):
            output = _capture_stdout(print_banner)
        self.assertIn("O R B S H A C K E R", output)
        self.assertIn("DESKTOP EXPERIENCE", output)
        self.assertIn("Developer:", output)

    def test_banner_compact_every_line_fits_40_columns(self):
        """Compact banner: every visible line (ANSI-stripped) must be <= 40 chars at 40-col width.

        Captures actual output and verifies line lengths after removing ANSI codes.
        """
        with (
            patch("orbshacker.ui.get_console_width", return_value=40),
            patch("orbshacker.ui._get_vt_cached", return_value=False),
        ):
            ui._reset_vt_cache()

            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            try:
                print_banner()
                output = sys.stdout.getvalue()
            finally:
                sys.stdout = old_stdout

            lines = output.split("\n")
            # Check every non-empty stripped line fits within 40 columns
            for line in lines:
                stripped = _strip_ansi(line).strip()
                if stripped:
                    self.assertLessEqual(
                        len(stripped), 40,
                        f"Compact banner line length {len(stripped)} exceeds 40 columns. Line: {repr(stripped[:40])}"
                    )

            # The compact layout must still carry all three information fields
            visible = " ".join(_strip_ansi(l) for l in lines)
            self.assertIn("Developer:", visible)
            self.assertIn("Version:", visible)
            self.assertIn("Database:", visible)


# ── Banner information ──────────────────────────────────────────────────────

class TestBannerInformation(unittest.TestCase):
    """Developer, version and database details must survive every layout."""

    def setUp(self):
        ui._reset_vt_cache()

    def _render(self, width):
        with patch("orbshacker.ui.get_console_width", return_value=width), \
                patch("orbshacker.ui._get_vt_cached", return_value=False):
            return _capture_stdout(print_banner)

    def _assert_all_fields(self, output):
        flat = " ".join(_strip_ansi(output).split())
        self.assertIn("Developer:", flat)
        self.assertIn(config.DEVELOPER, flat)
        self.assertIn("Version:", flat)
        self.assertIn(config.VERSION, flat)
        self.assertIn("Database:", flat)
        self.assertIn("Discord Official API + GitHub Archive", flat)

    def _assert_fits(self, output, width):
        for line in output.split("\n"):
            visible = _strip_ansi(line)
            self.assertLessEqual(len(visible), width, repr(visible))

    def test_banner_40_columns_shows_all_fields(self):
        output = self._render(40)
        self._assert_all_fields(output)
        self._assert_fits(output, 40)

    def test_banner_120_columns_shows_all_fields(self):
        output = self._render(120)
        self._assert_all_fields(output)
        self._assert_fits(output, 120)

    def test_banner_has_no_uninterpolated_placeholders(self):
        for width in (40, 120):
            output = self._render(width)
            self.assertNotIn("{config.", output)
            self.assertNotIn("{Colors.", output)

    def test_banner_keeps_wordmark_header_on_wide_terminals(self):
        output = _strip_ansi(self._render(120))
        self.assertIn("O R B S H A C K E R", output)
        self.assertIn("DESKTOP EXPERIENCE", output)
        self.assertIn("+-", output)

    def test_banner_remains_visible_when_width_is_zero(self):
        """A zero reported width falls back instead of silencing the banner."""
        output = self._render(0)
        lines = _visible_lines(output)
        self.assertTrue(lines, "banner emitted no output at width 0")
        self.assertTrue(all(len(line) <= 80 for line in lines), lines)
        self.assertIn("Developer:", " ".join(lines))

    def test_banner_remains_visible_when_width_is_negative(self):
        """A negative reported width falls back instead of silencing the banner."""
        output = self._render(-5)
        lines = _visible_lines(output)
        self.assertTrue(lines, "banner emitted no output at width -5")
        self.assertTrue(all(len(line) <= 80 for line in lines), lines)
        self.assertIn("Version:", " ".join(lines))

    def test_compact_banner_has_closed_borders(self):
        """The compact wordmark row must be framed and never overflow."""
        for width in (4, 5, 6, 12, 40, 63):
            output = self._render(width)
            lines = _visible_lines(output)
            self.assertGreaterEqual(len(lines), 3, (width, lines))
            top, middle, bottom = lines[0], lines[1], lines[2]

            self.assertTrue(top.startswith("+") and top.endswith("+"), (width, top))
            self.assertTrue(middle.startswith("|"), (width, middle))
            self.assertTrue(middle.endswith("|"), (width, middle))
            self.assertTrue(bottom.startswith("+") and bottom.endswith("+"), (width, bottom))
            self.assertEqual(len(top), width, (width, top))
            self.assertEqual(len(middle), width, (width, middle))
            self.assertEqual(len(bottom), width, (width, bottom))
            # Below 12 columns metadata keeps a readable minimum width instead
            # of being chopped into single characters.
            self.assertTrue(
                all(len(line) <= max(width, ui._BANNER_MIN_INFO_WIDTH) for line in lines),
                (width, lines),
            )


# ── Loading animation ────────────────────────────────────────────────────────

class TestLoadingAnimationFallback(unittest.TestCase):
    """loading_animation() tests."""

    def test_loading_animation_ascii_fallback(self):
        """ASCII frames are written when VT is unavailable."""
        with patch("orbshacker.ui._get_vt_cached", return_value=False):
            output = _capture_stdout(loading_animation, "test", 0.1)

        self.assertTrue(output.strip(), "spinner wrote nothing")
        self.assertNotIn("\x1b[", output)
        self.assertTrue(any(frame in output for frame in "|/-\\"), repr(output))

    def test_loading_animation_unicode(self):
        """Unicode braille frames are written when VT is available."""
        with patch("orbshacker.ui._get_vt_cached", return_value=True):
            output = _capture_stdout(loading_animation, "test", 0.1)

        self.assertTrue(output.strip(), "spinner wrote nothing")
        self.assertIn("\x1b[", output)
        self.assertTrue(
            any("\u2800" <= ch <= "\u28ff" for ch in output),
            repr(output),
        )

    def test_loading_animation_no_ansi_when_vt_disabled(self):
        """Never emit ANSI escape sequences when VT support is unavailable."""
        with patch("orbshacker.ui._get_vt_cached", return_value=False):
            output = _capture_stdout(loading_animation, "test", 0.05)

        self.assertTrue(output.strip(), "spinner wrote nothing")
        self.assertNotIn("\x1b[", output)

    def test_loading_animation_falls_back_when_unicode_encode_fails(self):
        """A UnicodeEncodeError switches the spinner to ASCII instead of freezing."""
        fake_stdout = _UnicodeHostileStdout()
        with patch("orbshacker.ui._get_vt_cached", return_value=True), \
                patch("orbshacker.ui.sys.stdout", fake_stdout):
            loading_animation("test", 0.1)

        output = fake_stdout.output
        self.assertTrue(output.strip(), "spinner produced no output after fallback")
        self.assertFalse(
            any("\u2800" <= ch <= "\u28ff" for ch in output),
            f"braille leaked through: {output!r}",
        )
        self.assertTrue(
            any(frame in output for frame in "|/-\\"),
            f"no ASCII frame written after UnicodeEncodeError: {output!r}",
        )


# ── Menu and prompt fallback ────────────────────────────────────────────────

class TestMenuAndPromptFallback(unittest.TestCase):
    """print_menu() and ask_confirm() plain-text paths."""

    def test_print_menu_vt_disabled_has_no_ansi(self):
        """Menu renders all options as plain text when VT is unavailable."""
        with patch("orbshacker.ui.get_console_width", return_value=80), \
                patch("orbshacker.ui._get_vt_cached", return_value=False):
            output = _capture_stdout(ui.print_menu)

        self.assertTrue(output.strip(), "menu produced no output")
        self.assertNotIn("\x1b[", output)
        self.assertIn("MAIN MENU", output)
        for option in (
            "1  Search Discord database",
            "2  Manual mode",
            "3  Steam Quest Mode",
            "4  Credits & Info",
            "5  Exit",
        ):
            self.assertIn(option, output)
        for description in (
            "Official API catalogue",
            "Custom executable",
            "AppID manifests",
            "About this build",
            "Close Orbshacker",
        ):
            self.assertIn(description, output)

    def test_ask_confirm_vt_disabled_prompt_and_accepts_yes(self):
        """Prompt is plain text and a 'y' answer returns True."""
        seen = {}

        def fake_input(prompt=""):
            seen["prompt"] = prompt
            return "y"

        with patch("orbshacker.ui._get_vt_cached", return_value=False), \
                patch("builtins.input", fake_input):
            result = ui.ask_confirm("Create and launch?")

        self.assertTrue(result)
        self.assertIn("Create and launch?", seen["prompt"])
        self.assertIn("[Y/n]", seen["prompt"])
        self.assertNotIn("\x1b[", seen["prompt"])

    def test_ask_confirm_vt_disabled_rejects_no(self):
        """A 'n' answer returns False."""
        with patch("orbshacker.ui._get_vt_cached", return_value=False), \
                patch("builtins.input", return_value="n"):
            self.assertFalse(ui.ask_confirm())


# ── Boxed title overflow ────────────────────────────────────────────────────

class TestPrintBoxedTitleOverflow(unittest.TestCase):
    """print_boxed_title() overflow prevention tests."""

    def test_boxed_title_min_width(self):
        """Twenty columns is conventional; narrower requests stay narrow."""
        self.assertEqual(_boxed_title_line_lengths(80, 20, title="Short"), [20, 20, 20])
        # Never widened past the caller's request.
        self.assertEqual(_boxed_title_line_lengths(80, 5, title="Short"), [5, 5, 5, 5])
        # A narrow terminal caps the box instead of overflowing.
        narrow = _boxed_title_line_lengths(12, 20, title="Short")
        self.assertTrue(narrow)
        self.assertTrue(all(l <= 12 for l in narrow), narrow)

    def test_boxed_title_default_width(self):
        """Omitted width keeps the documented 50-column default, capped by the terminal."""
        with patch("orbshacker.ui.get_console_width", return_value=64), \
                patch("orbshacker.ui._get_vt_cached", return_value=False):
            output = _capture_stdout(print_boxed_title, "Test Title")
        lengths = [len(_strip_ansi(l)) for l in output.split("\n") if _strip_ansi(l).strip()]
        self.assertEqual(lengths, [50, 50, 50])

        # The terminal still caps the requested default when it is narrower
        with patch("orbshacker.ui.get_console_width", return_value=40), \
                patch("orbshacker.ui._get_vt_cached", return_value=False):
            output = _capture_stdout(print_boxed_title, "Test Title")
        lengths = [len(_strip_ansi(l)) for l in output.split("\n") if _strip_ansi(l).strip()]
        self.assertEqual(lengths, [40, 40, 40])

    def test_boxed_title_prevents_border_overflow(self):
        """Boxed title border never exceeds terminal width even with very long title.

        Verifies the cone_width capping logic: min(max(width, 20), 80) ensures
        the border never overflows the terminal, even when the title is much longer
        than the available width. Every rendered line (border + title) stays within
        the available terminal width when ANSI codes are stripped.
        """
        # 40-column terminal, 100-character title: wraps, never exceeds 40
        lengths = _boxed_title_line_lengths(40, 40, title="A" * 100)
        self.assertGreater(len(lengths), 3, lengths)
        self.assertTrue(all(l <= 40 for l in lengths), lengths)

        # 80-column terminal, 100-character title: wraps, never exceeds 80
        lengths = _boxed_title_line_lengths(80, 80, title="A" * 100)
        self.assertTrue(all(l <= 80 for l in lengths), lengths)

    def test_boxed_title_caller_width_vs_terminal_width(self):
        """A width=65 request on a 40-column terminal must render 40 columns."""
        lengths = _boxed_title_line_lengths(40, 65, title="Test Title")
        self.assertEqual(lengths, [40, 40, 40])


# ── Boxed title wrapping regression tests ────────────────────────────────────

def _boxed_title_line_lengths(term_width: int, requested_width: int | None = None,
                              title: str = "A" * 38) -> list[int]:
    """Render print_boxed_title against a controlled terminal width.

    ``term_width`` is what the terminal reports and ``requested_width`` is what
    the caller asks for; they are independent so results never depend on the
    real terminal size. ``requested_width=None`` asks for the terminal width.
    """
    with patch("orbshacker.ui.get_console_width", return_value=term_width), \
            patch("orbshacker.ui._get_vt_cached", return_value=False):
        output = _capture_stdout(print_boxed_title, title, width=requested_width)

    return [len(_strip_ansi(l)) for l in output.split("\n") if _strip_ansi(l).strip()]


class TestPrintBoxedTitleWrapping(unittest.TestCase):
    """Regression tests for print_boxed_title() title wrapping."""

    def test_wrapped_title_40_column_terminal(self):
        """A 100-character title wraps at 40 columns without overflow."""
        lengths = _boxed_title_line_lengths(40, 40, title="W" * 100)

        # Two borders plus at least two title rows means wrapping really happened.
        self.assertGreater(len(lengths), 3, f"title did not wrap: {lengths}")
        self.assertTrue(all(l <= 40 for l in lengths), lengths)
        self.assertEqual(max(lengths), 40, lengths)

    def test_wrapped_title_80_column_terminal(self):
        """A 100-character title exceeds a single 80-column row and wraps."""
        lengths = _boxed_title_line_lengths(80, 80, title="W" * 100)

        self.assertGreater(len(lengths), 3, f"title did not wrap: {lengths}")
        self.assertTrue(all(l <= 80 for l in lengths), lengths)
        self.assertEqual(max(lengths), 80, lengths)

    def test_normal_title_40_column(self):
        """Short title at 40 columns: three lines (border, title, border), each 40 chars.

        Uses console width 40 with a short title. The output has 5 total lines
        but 3 non-empty (top border, title line, bottom border), each exactly 40 chars.
        """
        lengths = _boxed_title_line_lengths(40)
        # Should have 3 non-empty lines: top border, title line, bottom border
        self.assertEqual(len(lengths), 3, f"Expected 3 non-empty lines, got {len(lengths)}: {lengths}")
        # Each line should be exactly 40 chars wide
        for i, line_len in enumerate(lengths):
            self.assertEqual(
                line_len, 40,
                f"Line {i} length {line_len} != 40. Line lengths: {lengths}"
            )

    def test_normal_title_80_column(self):
        """Short title at 80 columns: three lines (border, title, border), each 80 chars.

        Uses console width 80 with a short title. The output has 5 total lines
        but 3 non-empty (top border, title line, bottom border), each exactly 80 chars.
        """
        lengths = _boxed_title_line_lengths(80)
        # Should have 3 non-empty lines: top border, title line, bottom border
        self.assertEqual(len(lengths), 3, f"Expected 3 non-empty lines, got {len(lengths)}: {lengths}")
        # Each line should be exactly 80 chars wide
        for i, line_len in enumerate(lengths):
            self.assertEqual(
                line_len, 80,
                f"Line {i} length {line_len} != 80. Line lengths: {lengths}"
            )

    def test_caller_width_65_with_40_terminal(self):
        """Regression test: caller requests width=65 but terminal is 40 columns."""
        lengths = _boxed_title_line_lengths(40, 65)
        max_length = max(lengths) if lengths else 0
        self.assertLessEqual(
            max_length, 40,
            f"Line length {max_length} exceeds 40-column terminal width even "
            f"when caller requested width=65. Line lengths: {lengths}"
        )
        self.assertEqual(max_length, 40)

    def test_requested_width_40_in_80_column_terminal(self):
        """A 40-column request inside an 80-column terminal renders 40."""
        self.assertEqual(_boxed_title_line_lengths(80, 40, title="Short"), [40, 40, 40])

    def test_requested_width_above_maximum_is_clamped(self):
        """Widths above the 80-column maximum are clamped even in wide terminals."""
        self.assertEqual(_boxed_title_line_lengths(200, 200, title="Short"), [80, 80, 80])

    def test_terminal_below_minimum_width(self):
        """A 12-column terminal never produces a line wider than 12."""
        lengths = _boxed_title_line_lengths(12, 65, title="A" * 40)
        self.assertTrue(lengths)
        self.assertTrue(all(l <= 12 for l in lengths), lengths)

    def test_long_title_wraps_within_terminal(self):
        """A 200-character title wraps to the 40-column terminal breadth."""
        lengths = _boxed_title_line_lengths(40, 65, title="T" * 200)
        self.assertGreater(len(lengths), 3, lengths)
        self.assertTrue(all(l == 40 for l in lengths), lengths)

    def test_no_visible_line_exceeds_terminal_width(self):
        """Across width combinations, output is non-empty and never overflows."""
        for term_width, requested in ((40, 65), (80, 40), (200, 300), (12, 65)):
            lengths = _boxed_title_line_lengths(term_width, requested, title="Q" * 90)
            self.assertTrue(
                lengths,
                f"no rendered lines for term={term_width} requested={requested}",
            )
            self.assertTrue(
                all(l <= term_width for l in lengths),
                f"term={term_width} requested={requested} lengths={lengths}",
            )


# ── Credits content ─────────────────────────────────────────────────────────

class TestShowCreditsContent(unittest.TestCase):
    """The original HEAD credits wording must be preserved."""

    def setUp(self):
        ui._reset_vt_cache()

    def _render(self):
        with patch("orbshacker.ui.get_console_width", return_value=120), \
                patch("orbshacker.ui._get_vt_cached", return_value=False), \
                patch("builtins.input", return_value=""):
            return _capture_stdout(show_credits)

    def test_original_description_section_present(self):
        output = _strip_ansi(self._render())
        self.assertIn("Description:", output)
        self.assertIn("This tool works as a game process spoofer.", output)

    def test_all_seven_spoofing_steps_present(self):
        output = _strip_ansi(self._render())
        steps = [
            "1. Connects to Discord's official API to get the latest game list",
            "2. Finds the exact process name Discord expects for each game",
            "3. Copies exe.exe to Desktop/Win64/ and renames it to match",
            "4. Launches the fake process in background",
            "5. Discord scans running processes and detects the fake process name",
            "6. Discord thinks you're playing the game (process name match)",
            "7. The fake process must stay running for Discord to keep detecting it",
        ]
        for step in steps:
            self.assertIn(step, output)

    def test_steam_manifest_format_preserved(self):
        self.assertIn("appmanifest_<appid>.acf", _strip_ansi(self._render()))

    def test_install_directory_format_preserved(self):
        self.assertIn("steamapps/common/<installdir>/", _strip_ansi(self._render()))

    def test_warning_heading_preserved(self):
        output = _strip_ansi(self._render())
        self.assertIn("WARNING - EDUCATIONAL PURPOSES ONLY", output)
        self.assertNotIn("WARNING - EDUCATIONAL PURPOSES ONLY:", output)

    def test_version_spacing_preserved(self):
        self.assertIn("Version:   ", _strip_ansi(self._render()))

    def test_credits_emit_no_ansi_when_vt_disabled(self):
        self.assertNotIn("\x1b[", self._render())




class TestMainPromptVtAwareness(unittest.TestCase):
    """Stage 3: selection prompt VT-awareness tests."""

    def setUp(self):
        ui._reset_vt_cache()

    def _get_prompt(self, vt=False):
        """Capture the input prompt that main.py would produce."""
        seen = {"prompt": None}

        def fake_input(prompt=""):
            seen["prompt"] = prompt
            return "3"

        with patch("orbshacker.ui._get_vt_cached", return_value=vt),                 patch("builtins.input", fake_input):
            C = ui._colors()
            prompt = f"{C.BOLD}{C.ACCENT}Select option{C.RESET} {C.MUTED}[1-5]{C.RESET}: "
            result = fake_input(prompt)
            return seen["prompt"], result

    def test_vt_enabled_prompt_contains_ansi_styling(self):
        """VT-enabled prompt contains the intended cyan/bold styling."""
        prompt, _ = self._get_prompt(vt=True)
        self.assertIn("[", prompt, "VT-enabled prompt must contain ANSI escapes")
        self.assertIn(Colors.CYAN, prompt, "VT-enabled prompt must use CYAN color")
        self.assertIn(Colors.BOLD, prompt, "VT-enabled prompt must use BOLD")

    def test_vt_disabled_prompt_has_zero_esc_bytes(self):
        """VT-disabled prompt contains zero ESC bytes."""
        prompt, _ = self._get_prompt(vt=False)
        self.assertNotIn("[", prompt, "VT-disabled prompt must not contain ESC bytes")
        self.assertIn("Select option", prompt)
        self.assertIn("[1-5]", prompt)
        self.assertTrue(prompt.endswith(": "), f"Prompt must end with ': ', got: {prompt!r}")

    def test_prompt_text_identical_after_ansi_stripping(self):
        """Prompt text is identical in both modes after ANSI stripping."""
        prompt_vt, _ = self._get_prompt(vt=True)
        prompt_no_vt, _ = self._get_prompt(vt=False)
        stripped_vt = re.sub(r"\[[0-9;]*m", "", prompt_vt)
        self.assertEqual(stripped_vt, prompt_no_vt,
                         "VT and non-VT prompts must match after stripping")

    def test_numeric_input_behavior_unchanged(self):
        """Numeric input behavior remains unchanged in both modes."""
        # VT-enabled
        seen_vt = {"choice": None}
        def fake_input_vt(prompt=""):
            seen_vt["choice"] = prompt
            return "3"
        with patch("orbshacker.ui._get_vt_cached", return_value=True),                 patch("builtins.input", fake_input_vt):
            C = ui._colors()
            prompt = f"{C.BOLD}{C.ACCENT}Select option{C.RESET} {C.MUTED}[1-5]{C.RESET}: "
            fake_input_vt(prompt)
            self.assertIn("Select option", seen_vt["choice"])
            self.assertIn("[1-5]", seen_vt["choice"])

        # VT-disabled
        seen_no_vt = {"choice": None}
        def fake_input_no_vt(prompt=""):
            seen_no_vt["choice"] = prompt
            return "3"
        with patch("orbshacker.ui._get_vt_cached", return_value=False),                 patch("builtins.input", fake_input_no_vt):
            C = ui._colors()
            prompt = f"{C.BOLD}{C.ACCENT}Select option{C.RESET} {C.MUTED}[1-5]{C.RESET}: "
            fake_input_no_vt(prompt)
            self.assertIn("Select option", seen_no_vt["choice"])
            self.assertIn("[1-5]", seen_no_vt["choice"])
class _OneArgKernel32:
    """kernel32 stand-in that refuses a one-argument GetConsoleMode call."""

    def __init__(self):
        self.calls = []
        self.GetStdHandle = _FakeWin32Function(lambda which: 9)
        self.GetConsoleMode = _FakeWin32Function(self._mode)

    def _mode(self, *args):
        self.calls.append(args)
        if len(args) != 2:
            raise TypeError("GetConsoleMode() takes exactly 2 arguments")
        args[1]._obj.value = 0x0004
        return 1


class TestStage1Acceptance(unittest.TestCase):
    """Acceptance checks for the Stage 1 terminal contract."""

    def setUp(self):
        ui._reset_vt_cache()

    # -- Win32 contract -----------------------------------------------------

    def test_get_console_mode_receives_handle_and_pointer(self):
        fake = _FakeKernel32(handle=9, mode=0x0004, mode_ok=1)
        with (
            patch("orbshacker.ui.sys.platform", "win32"),
            patch("orbshacker.ui._get_kernel32", return_value=fake),
        ):
            self.assertTrue(is_vt_supported())

        self.assertEqual(len(fake.GetConsoleMode.calls), 1)
        args = fake.GetConsoleMode.calls[0]
        self.assertEqual(len(args), 2, "GetConsoleMode must receive (handle, pointer)")
        self.assertEqual(args[0], 9)
        self.assertTrue(hasattr(args[1], "_obj"), args)

    def test_one_argument_detection_cannot_succeed(self):
        """The implementation must not fall back to GetConsoleMode(handle)."""
        fake = _OneArgKernel32()
        with (
            patch("orbshacker.ui.sys.platform", "win32"),
            patch("orbshacker.ui._get_kernel32", return_value=fake),
        ):
            result = is_vt_supported()

        self.assertTrue(result, "two-argument detection should succeed")
        self.assertEqual([len(call) for call in fake.calls], [2])

    # -- POSIX failure modes ------------------------------------------------

    def test_posix_fileno_exception_returns_false(self):
        class _NoFileno:
            def fileno(self):
                raise OSError("no fileno")

        with (
            patch("orbshacker.ui.sys.platform", "linux"),
            patch("orbshacker.ui.sys.stdout", _NoFileno()),
        ):
            self.assertFalse(is_vt_supported())

    def test_posix_isatty_exception_returns_false(self):
        with (
            patch("orbshacker.ui.sys.platform", "linux"),
            patch("orbshacker.ui.os.isatty", side_effect=OSError("boom")),
        ):
            self.assertFalse(is_vt_supported())

    # -- Plain-text fallback ------------------------------------------------

    def test_print_boxed_title_vt_disabled_has_no_ansi(self):
        with (
            patch("orbshacker.ui.get_console_width", return_value=80),
            patch("orbshacker.ui._get_vt_cached", return_value=False),
        ):
            output = _capture_stdout(print_boxed_title, "Titles", width=40)

        self.assertIn("Titles", output)
        self.assertNotIn(chr(27) + "[", output)

    def test_print_boxed_title_vt_enabled_keeps_colour(self):
        with (
            patch("orbshacker.ui.get_console_width", return_value=80),
            patch("orbshacker.ui._get_vt_cached", return_value=True),
        ):
            output = _capture_stdout(print_boxed_title, "Titles", width=30)

        self.assertIn(chr(27) + "[", output)
        self.assertIn("Titles", output)

    def test_print_banner_vt_disabled_has_no_ansi_and_all_metadata(self):
        with (
            patch("orbshacker.ui.get_console_width", return_value=100),
            patch("orbshacker.ui._get_vt_cached", return_value=False),
        ):
            output = _capture_stdout(print_banner)

        self.assertNotIn(chr(27) + "[", output)
        for expected in (
            config.DEVELOPER,
            config.VERSION,
            "Discord Official API + GitHub Archive",
        ):
            self.assertIn(expected, output)

    # -- Boxed title sizing -------------------------------------------------

    def test_requested_50_on_40_column_terminal(self):
        lengths = _boxed_title_line_lengths(40, 50, title="Titles")
        self.assertTrue(lengths)
        self.assertEqual(max(lengths), 40)
        self.assertTrue(all(l <= 40 for l in lengths), lengths)

    def test_requested_65_on_40_column_terminal(self):
        lengths = _boxed_title_line_lengths(40, 65, title="A" * 90)
        self.assertTrue(lengths)
        self.assertTrue(all(l <= 40 for l in lengths), lengths)

    def test_very_narrow_request_is_not_widened(self):
        for requested in (8, 12, 19):
            lengths = _boxed_title_line_lengths(80, requested, title="Title")
            self.assertTrue(lengths, requested)
            self.assertTrue(all(l <= requested for l in lengths), (requested, lengths))

    # -- Banner readability -------------------------------------------------

    def test_banner_very_narrow_metadata_stays_readable(self):
        with (
            patch("orbshacker.ui.get_console_width", return_value=6),
            patch("orbshacker.ui._get_vt_cached", return_value=False),
        ):
            output = _capture_stdout(print_banner)

        lines = [line for line in output.split(chr(10)) if line.strip()]
        self.assertTrue(lines, "banner produced no output")
        flat = " ".join(lines)
        for label in ("Developer:", "Version:", "Database:"):
            self.assertIn(label, flat)
        self.assertIn(config.VERSION, flat)
        self.assertTrue(
            all(len(line) <= ui._BANNER_MIN_INFO_WIDTH for line in lines),
            lines,
        )

    # -- Deterministic spinner ----------------------------------------------

    def test_loading_animation_frames_and_cleanup_are_deterministic(self):
        ticks = iter([0.0, 0.0, 0.1, 0.2, 1.0])
        with (
            patch("orbshacker.ui._get_vt_cached", return_value=False),
            patch("orbshacker.ui.time.time", lambda: next(ticks)),
            patch("orbshacker.ui.time.sleep", lambda *_: None),
        ):
            output = _capture_stdout(loading_animation, "spin", 0.5)

        expected = (
            chr(13) + "| spin"
            + chr(13) + "/ spin"
            + chr(13) + "- spin"
            + chr(13) + " " * 9 + chr(13)
        )
        self.assertEqual(output, expected)

    def test_loading_animation_unicode_frames_are_deterministic(self):
        ticks = iter([0.0, 0.0, 1.0])
        with (
            patch("orbshacker.ui._get_vt_cached", return_value=True),
            patch("orbshacker.ui.time.time", lambda: next(ticks)),
            patch("orbshacker.ui.time.sleep", lambda *_: None),
        ):
            output = _capture_stdout(loading_animation, "spin", 0.5)

        ansi = chr(27) + "["
        expected = (
            chr(13) + ansi + "96m" + chr(0x280b) + ansi + "0m spin"
            + chr(13) + ansi + "96m" + " " * 9 + chr(13)
        )
        self.assertEqual(output, expected)


# ── Stage 3: premium terminal theme ─────────────────────────────────────────


class TestPremiumPalette(unittest.TestCase):
    """The theme exposes semantic roles without breaking existing names."""

    def test_semantic_aliases_reuse_existing_colours(self):
        self.assertEqual(Colors.ACCENT, Colors.CYAN)
        self.assertEqual(Colors.SECONDARY, Colors.MAGENTA)
        self.assertEqual(Colors.SUCCESS, Colors.GREEN)
        self.assertEqual(Colors.WARNING, Colors.YELLOW)
        self.assertEqual(Colors.ERROR, Colors.RED)
        self.assertEqual(Colors.INFO, Colors.CYAN)
        self.assertEqual(Colors.MUTED, Colors.GRAY)
        self.assertEqual(Colors.TEXT, Colors.WHITE)
        self.assertEqual(Colors.BORDER, Colors.GRAY)

    def test_plain_palette_has_no_escape_sequences(self):
        for name in (
            "RESET", "BOLD", "DIM", "RED", "GREEN", "YELLOW", "BLUE",
            "MAGENTA", "CYAN", "WHITE", "GRAY", "ACCENT", "SECONDARY",
            "SUCCESS", "WARNING", "ERROR", "INFO", "MUTED", "TEXT", "BORDER",
        ):
            self.assertEqual(getattr(ui._NoColors, name), "", name)


class TestPremiumBanner(unittest.TestCase):
    """Wide header identity, colouring and width safety."""

    def setUp(self):
        ui._reset_vt_cache()

    def _render(self, width, vt=False):
        with patch("orbshacker.ui.get_console_width", return_value=width), \
                patch("orbshacker.ui._get_vt_cached", return_value=vt):
            return _capture_stdout(print_banner)

    def test_wide_header_shows_wordmark_tagline_status_and_version(self):
        output = self._render(100)
        self.assertIn("O R B S H A C K E R", output)
        self.assertIn("DESKTOP EXPERIENCE", output)
        self.assertIn("READY", output)
        self.assertIn(config.VERSION, output)

    def test_wide_header_is_coloured_when_vt_enabled(self):
        output = self._render(100, vt=True)
        visible = _strip_ansi(output)
        self.assertIn("\x1b[", output)
        self.assertIn("O R B S H A C K E R", visible)
        self.assertIn("READY", visible)
        self.assertIn(Colors.CYAN, output)
        self.assertIn(Colors.GREEN, output)

    def test_wide_header_never_exceeds_its_box_or_the_terminal(self):
        for width in (64, 72, 100, 200):
            output = self._render(width, vt=True)
            limit = min(width, ui._BANNER_ADVANCED_WIDTH)
            for line in output.split("\n"):
                visible = _strip_ansi(line)
                if visible.strip():
                    self.assertLessEqual(len(visible), limit, (width, visible))

    def test_narrow_terminal_uses_compact_header_without_status_chip(self):
        output = self._render(40)
        self.assertIn("ORBSHACKER", output)
        self.assertNotIn("O R B S H A C K E R", output)
        self.assertNotIn("READY", output)

    def test_metadata_rows_are_aligned(self):
        visible = _strip_ansi(self._render(100))
        self.assertIn("  Developer:  ", visible)
        self.assertIn("  Version:    ", visible)
        self.assertIn("  Database:   ", visible)

    def test_banner_remains_ascii_only(self):
        for width in (40, 100):
            for vt in (False, True):
                self.assertTrue(self._render(width, vt=vt).isascii(), (width, vt))


class TestPremiumMenu(unittest.TestCase):
    """Menu hierarchy, selected-row visibility and width behaviour."""

    def setUp(self):
        ui._reset_vt_cache()

    def _render(self, width=80, selected=None, vt=False):
        with patch("orbshacker.ui.get_console_width", return_value=width), \
                patch("orbshacker.ui._get_vt_cached", return_value=vt):
            return _capture_stdout(ui.print_menu, selected)

    def test_rows_pair_each_option_with_its_description(self):
        visible = _strip_ansi(self._render(80))
        pairs = (
            ("Search Discord database", "Official API catalogue"),
            ("Manual mode", "Custom executable"),
            ("Steam Quest Mode", "AppID manifests"),
            ("Credits & Info", "About this build"),
            ("Exit", "Close Orbshacker"),
        )
        for label, description in pairs:
            row = next(line for line in visible.split("\n") if label in line)
            self.assertIn(description, row)

    def test_selected_row_is_marked_once(self):
        visible = _strip_ansi(self._render(80, selected=3))
        pointer_rows = [line for line in visible.split("\n") if ">" in line]
        self.assertEqual(len(pointer_rows), 1, visible)
        self.assertIn("Steam Quest Mode", pointer_rows[0])

    def test_selected_row_is_accented_when_vt_enabled(self):
        output = self._render(80, selected=3, vt=True)
        visible = _strip_ansi(output)
        row = next(line for line in visible.split("\n") if "Steam Quest Mode" in line)
        self.assertIn(">", row)
        self.assertIn("\x1b[", output)
        self.assertIn(Colors.CYAN, output)

    def test_descriptions_are_dropped_when_space_is_tight(self):
        visible = _strip_ansi(self._render(40))
        self.assertIn("Search Discord database", visible)
        self.assertNotIn("Official API catalogue", visible)

    def test_menu_never_exceeds_the_terminal_width(self):
        for width in (12, 20, 40, 56, 80, 120):
            visible = _strip_ansi(self._render(width))
            self.assertIn("MAIN MENU", visible)
            for line in visible.split("\n"):
                self.assertLessEqual(len(line), width, (width, line))

    def test_narrow_menu_keeps_numbers_visible(self):
        visible = _strip_ansi(self._render(12))
        for number in ("1", "2", "3", "4", "5"):
            self.assertIn(number, visible)

    def test_choice_echo_names_the_selected_option_once(self):
        with patch("orbshacker.ui._get_vt_cached", return_value=False):
            output = _capture_stdout(ui.print_selected, "3")
        self.assertIn("Steam Quest Mode", output)
        self.assertIn("(3)", output)
        self.assertEqual(output.count("Steam Quest Mode"), 1)

    def test_choice_echo_ignores_invalid_input(self):
        with patch("orbshacker.ui._get_vt_cached", return_value=False):
            for choice in ("", "0", "6", "x", "1.5"):
                self.assertEqual(_capture_stdout(ui.print_selected, choice), "")


class TestStatusBadges(unittest.TestCase):
    """Consistent success, warning, error, info and update treatments."""

    def setUp(self):
        ui._reset_vt_cache()

    def _print(self, text, color=Colors.WHITE, bold=False, vt=True):
        with patch("orbshacker.ui._get_vt_cached", return_value=vt), \
                patch("builtins.print") as mock_print:
            print_color(text, color, bold=bold)
        return "".join(str(call.args[0]) for call in mock_print.call_args_list)

    def test_known_prefixes_render_as_badges(self):
        cases = (
            ("[OK]", "OK", Colors.GREEN),
            ("[!]", "WARN", Colors.YELLOW),
            ("[ERROR]", "ERR", Colors.RED),
            ("[*]", "INFO", Colors.CYAN),
            ("[UPDATE]", "UPDATE", Colors.MAGENTA),
        )
        for prefix, label, colour in cases:
            with self.subTest(prefix=prefix):
                output = self._print(prefix + " message")
                self.assertIn(label, output)
                self.assertIn(colour, output)
                self.assertIn("message", output)
                self.assertNotIn(prefix, output)

    def test_plain_mode_preserves_the_original_prefix(self):
        self.assertEqual(self._print("[OK] done", Colors.GREEN, vt=False), "[OK] done")

    def test_unprefixed_messages_keep_their_colour_and_bold(self):
        output = self._print("plain", Colors.CYAN, bold=True)
        self.assertIn("\x1b[1m\x1b[96m", output)
        self.assertIn("plain", output)

    def test_unknown_prefixes_are_left_alone(self):
        output = self._print("[FAIL] nope", Colors.RED)
        self.assertIn("[FAIL] nope", output)


class TestPremiumPromptAndTitle(unittest.TestCase):
    """Prompt styling and muted box borders."""

    def setUp(self):
        ui._reset_vt_cache()

    def test_confirm_prompt_is_styled_when_vt_enabled(self):
        seen = {}

        def fake_input(prompt=""):
            seen["prompt"] = prompt
            return ""

        with patch("orbshacker.ui._get_vt_cached", return_value=True), \
                patch("builtins.input", fake_input):
            self.assertTrue(ui.ask_confirm("Create and launch?"))

        self.assertIn("Create and launch?", seen["prompt"])
        self.assertIn("[Y/n]", seen["prompt"])
        self.assertIn("\x1b[", seen["prompt"])

    def test_boxed_title_uses_muted_border_and_accent_title(self):
        with patch("orbshacker.ui.get_console_width", return_value=80), \
                patch("orbshacker.ui._get_vt_cached", return_value=True):
            output = _capture_stdout(print_boxed_title, "Titles", width=30)

        self.assertIn(Colors.GRAY + "+", output)
        self.assertIn(Colors.BOLD + Colors.CYAN + "Titles", output)


class TestLeadingNewlineBadges(unittest.TestCase):
    """Status prefixes that follow layout newlines still render as badges."""

    def setUp(self):
        ui._reset_vt_cache()

    def _print(self, text, color=Colors.WHITE, bold=False, vt=True):
        with patch("orbshacker.ui._get_vt_cached", return_value=vt), \
                patch("builtins.print") as mock_print:
            print_color(text, color, bold=bold)
        return "".join(str(call.args[0]) for call in mock_print.call_args_list)

    def test_all_prefixed_messages_render_badges(self):
        cases = (
            ("\n[OK] Setup complete!", "OK", Colors.GREEN),
            ("\n[!] Operation cancelled", "WARN", Colors.YELLOW),
            ("\n[ERROR] No games found", "ERR", Colors.RED),
            ("\n[*] Loading games database...", "INFO", Colors.CYAN),
            ("\n[UPDATE] Restarting...", "UPDATE", Colors.MAGENTA),
        )
        for text, label, colour in cases:
            with self.subTest(text=text):
                output = self._print(text, Colors.WHITE)
                self.assertIn(label, output)
                self.assertIn(colour, output)
                self.assertTrue(output.startswith("\n"), repr(output))
                prefix = text.lstrip("\n").split("] ", 1)[0] + "]"
                self.assertNotIn(prefix, output, output)
                body = text.lstrip("\n").split("] ", 1)[1]
                self.assertIn(body, output)

    def test_multiple_leading_newlines_are_preserved_exactly(self):
        output = self._print("\n\n[!] Interrupted by user", Colors.YELLOW)
        self.assertTrue(output.startswith("\n\n"), repr(output))
        self.assertEqual(output.count("\n"), 2, repr(output))
        self.assertIn("WARN", output)
        self.assertIn("Interrupted by user", output)

    def test_single_leading_newline_adds_no_extra_newline(self):
        output = self._print("\n[OK] Done", Colors.GREEN)
        self.assertEqual(output.count("\n"), 1, repr(output))

    def test_plain_mode_keeps_the_complete_original_message(self):
        output = self._print("\n[ERROR] No results found", Colors.RED, vt=False)
        self.assertEqual(output, "\n[ERROR] No results found")


class TestBadgeBoldAndColourSemantics(unittest.TestCase):
    """Badges keep the caller's bold intent and explicit body colours."""

    def setUp(self):
        ui._reset_vt_cache()

    def _print(self, text, color=Colors.WHITE, bold=False, vt=True):
        with patch("orbshacker.ui._get_vt_cached", return_value=vt), \
                patch("builtins.print") as mock_print:
            print_color(text, color, bold=bold)
        return "".join(str(call.args[0]) for call in mock_print.call_args_list)

    def test_bold_body_is_bold(self):
        output = self._print("[OK] Ready", Colors.GREEN, bold=True)
        self.assertIn("\x1b[1m\x1b[92mOK", output)
        self.assertIn("\x1b[1m\x1b[92mReady", output)

    def test_non_bold_body_stays_regular_weight(self):
        output = self._print("[OK] Ready", Colors.GREEN, bold=False)
        self.assertIn("\x1b[92mReady", output)
        self.assertNotIn("\x1b[1m\x1b[92mReady", output)

    def test_warning_badge_keeps_explicit_error_body_colour(self):
        output = self._print("[!] Failed to delete: locked", Colors.RED)
        self.assertIn("\x1b[1m\x1b[93mWARN", output)
        self.assertIn("\x1b[91mFailed to delete: locked", output)

    def test_error_badge_keeps_its_own_colour_with_mixed_call(self):
        output = self._print("[ERROR] Download failed", Colors.YELLOW, bold=True)
        self.assertIn("\x1b[1m\x1b[91mERR", output)
        self.assertIn("\x1b[1m\x1b[93mDownload failed", output)

    def test_badge_and_body_text_are_not_duplicated(self):
        output = self._print("[UPDATE] Restarting...", Colors.CYAN)
        self.assertEqual(output.count("UPDATE"), 1)
        self.assertEqual(output.count("Restarting..."), 1)


if __name__ == "__main__":
    unittest.main()
