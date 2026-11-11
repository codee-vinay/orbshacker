"""Tests for config.py and faker.py settings loading and cleanup behavior."""

import sys
import json
import time
import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from orbshacker import config as config_module
from orbshacker.config import _load_settings
from orbshacker.faker import GameFaker


def test_load_settings_fallback_in_dev():
    # In normal dev mode (not frozen), if settings.json does not exist, it falls back to importing settings.py
    with patch("builtins.__import__") as mock_import:
        _load_settings()
        called_modules = [args[0] for args, _ in mock_import.call_args_list if args]
        assert "settings" in called_modules


def test_load_settings_json_in_dev(tmp_path):
    # If settings.json exists in dev mode, it should load it
    json_path = tmp_path / "settings.json"
    json_path.write_text('{"CHOSEN_FOLDER": "CustomDir"}', encoding="utf-8")

    # We patch __file__ in config module to point to our temp folder structure
    fake_config_file = tmp_path / "orbshacker" / "config.py"
    with patch("orbshacker.config.__file__", str(fake_config_file)):
        settings = _load_settings()
        assert isinstance(settings, dict)
        assert settings.get("CHOSEN_FOLDER") == "CustomDir"


def test_load_settings_frozen_creates_json_and_loads(tmp_path):# Simulate frozen mode where no AppData is available and no settings exist
    exe_path = tmp_path / "orbshacker.exe"
    exe_path.touch()

    with patch("sys.frozen", True, create=True), \
         patch("sys.executable", str(exe_path)), \
         patch.dict("os.environ", {}, clear=True):  # Ensure LOCALAPPDATA is absent

        settings = _load_settings()
        # In frozen mode without AppData, return in-memory defaults
        assert isinstance(settings, dict)
        assert settings.get("CHOSEN_FOLDER") == "Desktop"
        assert settings.get("TIMER_MINUTES") == 15

        # No settings.json should be created beside the executable
        exe_dir = Path(sys.executable).parent
        assert not (exe_dir / "settings.json").exists(), \
            "Settings should not be written beside the executable in frozen mode without AppData"


def test_load_settings_frozen_existing_json(tmp_path):
    # Simulate frozen mode with existing legacy settings.json beside exe,
    # and NO AppData available (LOCALAPPDATA not set).
    # The code returns in-memory defaults and preserves the legacy file.
    exe_path = tmp_path / "orbshacker.exe"
    exe_path.touch()

    json_file = tmp_path / "settings.json"
    json_file.write_text('{"CHOSEN_FOLDER": "FrozenDir"}', encoding="utf-8")

    with patch("sys.frozen", True, create=True), \
         patch("sys.executable", str(exe_path)), \
         patch.dict("os.environ", {}, clear=True):  # No LOCALAPPDATA

        settings = _load_settings()
        # In frozen mode without AppData, return in-memory defaults
        assert isinstance(settings, dict)
        assert settings.get("CHOSEN_FOLDER") == "Desktop"
        assert settings.get("TIMER_MINUTES") == 15

        # Legacy file should be preserved since AppData is unavailable for migration
        legacy = Path(sys.executable).parent / "settings.json"
        assert legacy.exists(), "Legacy settings.json should be preserved when AppData is unavailable"
        assert legacy.read_text(encoding="utf-8") == '{"CHOSEN_FOLDER": "FrozenDir"}'


def test_faker_cleanup_deletes_files_and_processes(tmp_path):
    # Mock config.AUTO_DELETE to True for testing
    with patch("orbshacker.config.AUTO_DELETE", True):
        faker = GameFaker()

        # Mock process
        mock_proc = MagicMock()
        faker._processes.append(mock_proc)

        # Create dummy file to delete
        dummy_file = tmp_path / "faked_game.exe"
        dummy_file.touch()
        faker.register_created_file(dummy_file)

        # Create dummy directory to delete
        dummy_dir = tmp_path / "Win64"
        dummy_dir.mkdir()
        faker._created_dirs.append(dummy_dir)

        # Run cleanup
        faker.cleanup()

        # Assert process was terminated and killed
        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()

        # Assert file was deleted
        assert not dummy_file.exists()

        # Assert directory was deleted
        assert not dummy_dir.exists()


def test_faker_custom_timer_minutes(tmp_path):
    import orbshacker.config as config
    from orbshacker.faker import GameFaker

    # 1. Test source mode replacement
    with patch("orbshacker.config.TIMER_MINUTES", 25), \
         patch("orbshacker.config.AUTO_DELETE", False):

        faker = GameFaker()
        # Set dummy source exe
        dummy_src = tmp_path / "pythonw.exe"
        dummy_src.touch()
        faker._source_exe = dummy_src
        faker._frozen = False

        target_exe = tmp_path / "Win64" / "Game.exe"
        faker.copy_exe_to(target_exe)

        timer_script = tmp_path / "Win64" / "_orbshacker_timer.pyw"
        assert timer_script.exists()
        script_code = timer_script.read_text(encoding="utf-8")
        assert "TIMER_MINUTES = 25" in script_code
        assert "AUTO_DELETE = False" in script_code

    # 2. Test frozen mode launcher arguments
    with patch("orbshacker.config.TIMER_MINUTES", 35):
        faker = GameFaker()
        faker._frozen = True

        with patch("subprocess.Popen") as mock_popen:
            faker.launch_executable(Path("C:/Dummy/Game.exe"))

            # Assert subprocess.Popen was called with just the executable path
            mock_popen.assert_called_once()
            called_args = mock_popen.call_args[1].get("args", mock_popen.call_args[0][0])
            assert called_args == [str(Path("C:/Dummy/Game.exe"))]


def test_is_faked_game():
    # Load orbshacker entrypoint dynamically to test its functions
    script_path = Path(__file__).resolve().parents[1] / "orbshacker.py"
    spec = importlib.util.spec_from_file_location("orbshacker_script", script_path)
    orb_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(orb_module)

    # 1. Dev mode cases
    with patch("sys.frozen", False, create=True), \
         patch("sys.argv", ["orbshacker.py"]):
        assert not orb_module.is_faked_game()

    with patch("sys.frozen", False, create=True), \
         patch("sys.argv", ["TslGame.py"]):
        assert orb_module.is_faked_game()

    # 2. Frozen mode cases
    with patch("sys.frozen", True, create=True), \
         patch("sys.executable", "C:/Users/jjjda/Desktop/orbshacker.exe"):
        assert not orb_module.is_faked_game()

    with patch("sys.frozen", True, create=True), \
         patch("sys.executable", "C:/Users/jjjda/Desktop/Win64/TslGame.exe"):
        assert orb_module.is_faked_game()


def test_timer_self_destruction(tmp_path):
    pytest.importorskip("tkinter", reason="Tkinter is unavailable in this Python environment.")
    from orbshacker.timer import TimerApp
    import orbshacker.config as config

    with patch("orbshacker.config.AUTO_DELETE", True), \
         patch("orbshacker.config.STEAM_MANIFEST_PATH", str(tmp_path / "appmanifest_123.acf")):

        root = MagicMock()

        # Instantiate TimerApp without running _tick
        with patch.object(TimerApp, "_tick"):
            app = TimerApp(root, minutes=15)

        # Mock files next to executable
        exe_path = tmp_path / "Win64" / "TslGame.exe"
        exe_path.parent.mkdir()
        exe_path.touch()

        settings_path = exe_path.parent / "settings.json"
        settings_path.touch()

        with patch("sys.frozen", True, create=True), \
             patch("sys.executable", str(exe_path)), \
             patch("subprocess.Popen") as mock_popen, \
             patch.object(subprocess, "CREATE_NO_WINDOW", 0, create=True), \
             patch("sys.exit") as mock_exit:

            app.trigger_self_destruction()

            # Assert subprocess.Popen spawned self-destruct command including paths to delete
            mock_popen.assert_called_once()
            called_cmd = mock_popen.call_args[1].get("args", mock_popen.call_args[0][0])
            assert "TslGame.exe" in called_cmd
            assert "settings.json" in called_cmd
            assert "appmanifest_123.acf" in called_cmd

            # Verify UI clean shutdown
            root.destroy.assert_called_once()
            mock_exit.assert_called_once_with(0)


def test_load_settings_baked_frozen(tmp_path):
    # Simulate a faked game with embedded settings at the end of the exe
    exe_path = tmp_path / "TslGame.exe"
    
    config_data = {
        "CHOSEN_FOLDER": "BakedDir",
        "AUTO_DELETE": True,
        "TIMER_MINUTES": 45
    }
    
    # Write the exe with appended marker and JSON settings
    import json
    marker = b"__ORBSHACKER_BAKED_CONFIG__"
    json_bytes = json.dumps(config_data).encode("utf-8")
    
    exe_path.write_bytes(b"MZ_DUMMY_EXE_BYTES..." + marker + json_bytes + marker)
    
    with patch("sys.frozen", True, create=True), \
         patch("sys.executable", str(exe_path)):
         
        settings = _load_settings()
        assert isinstance(settings, dict)
        assert settings.get("CHOSEN_FOLDER") == "BakedDir"
        assert settings.get("AUTO_DELETE") is True
        assert settings.get("TIMER_MINUTES") == 45


class TestResolveRepository:
    default = ("codee-vinay", "orbshacker")

    def test_defaults_to_fork(self, monkeypatch):
        monkeypatch.delattr(
            config_module._build_version, "GITHUB_REPOSITORY", raising=False
        )
        assert config_module._resolve_repository() == self.default

    def test_uses_repository_injected_at_build_time(self, monkeypatch):
        monkeypatch.setattr(
            config_module._build_version,
            "GITHUB_REPOSITORY",
            "codee-vinay/orbshacker",
            raising=False,
        )
        assert config_module._resolve_repository() == self.default

    def test_uses_a_different_injected_repository(self, monkeypatch):
        monkeypatch.setattr(
            config_module._build_version,
            "GITHUB_REPOSITORY",
            "someone-else/orbshacker-fork",
            raising=False,
        )
        assert config_module._resolve_repository() == (
            "someone-else",
            "orbshacker-fork",
        )

    @pytest.mark.parametrize(
        "value",
        ["", "owner", "/name", "owner/", "owner/name/extra", None, 123],
    )
    def test_malformed_values_fall_back_to_default(self, monkeypatch, value):
        monkeypatch.setattr(
            config_module._build_version,
            "GITHUB_REPOSITORY",
            value,
            raising=False,
        )
        assert config_module._resolve_repository() == self.default

def test_fresh_process_no_name_error_on_frozen_import():
    """
    Regression test: verify that importing orbshacker.config in a fresh
    Python process with sys.frozen = True set BEFORE import does NOT raise
    NameError for _is_valid_json_file or _read_json_safely.

    This catches the exact bug where _load_settings() calls _is_valid_json_file()
    at module initialization before the function was defined.
    """
    import subprocess, sys, os, tempfile

    with tempfile.TemporaryDirectory() as tmp_loc:
        # Build child process code using string concatenation + chr(10)
        code = "import sys" + chr(10)
        code += "sys.frozen = True" + chr(10)
        code += "# Use isolated LOCALAPPDATA so we do not read/write real user config" + chr(10)
        code += "import os" + chr(10)
        # Build the code string for LOCALAPPDATA assignment
        # Build the code string for LOCALAPPDATA assignment
        code += 'import os' + chr(10)
        code += 'os.environ["LOCALAPPDATA"] = "' + tmp_loc.replace(chr(92), '/') + '"' + chr(10)
        code += "# Now import - this should complete without NameError" + chr(10)
        code += "from orbshacker import config as config_module" + chr(10)
        code += "# Verify import-time initialization succeeded" + chr(10)
        code += "print(" + chr(34) + "FROZEN_IMPORT_OK" + chr(34) + ")" + chr(10)

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": ""},
        )

        assert result.returncode == 0, (
            f"FAIL: Subprocess exited with code {result.returncode}",
            f"stdout: {result.stdout}",
            f"stderr: {result.stderr}",
        )

        assert "FROZEN_IMPORT_OK" in result.stdout, (
            f"FAIL: Expected FROZEN_IMPORT_OK in stdout, got: {result.stdout}",
        )