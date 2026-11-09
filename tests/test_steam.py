"""Tests for steam.py – pure functions only (no network calls)."""

import builtins
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import orbshacker.config as config
from orbshacker.steam import (
    _pick_windows_exe,
    _resolve_executable,
    generate_appmanifest,
    manifest_path,
    steam_quest_mode,
)


SENTINEL = b'"AppState"\n{\n\t"appid"\t\t"999"\n\t"name"\t\t"Existing"\n}\n'
APPID = 4321


@pytest.fixture(autouse=True)
def _restore_steam_manifest_setting():
    """steam_quest_mode() deletes config.STEAM_MANIFEST_PATH; keep module state stable."""
    missing = object()
    original = getattr(config, "STEAM_MANIFEST_PATH", missing)
    yield
    if original is missing:
        if hasattr(config, "STEAM_MANIFEST_PATH"):
            delattr(config, "STEAM_MANIFEST_PATH")
    else:
        config.STEAM_MANIFEST_PATH = original


def _steam_root(tmp_path: Path) -> Path:
    root = tmp_path / "Steam"
    (root / "steamapps").mkdir(parents=True)
    return root


def _failing_open(target: Path):
    """open() that writes a partial manifest then fails, only for *target*."""
    real_open = builtins.open

    class _FailingWriter:
        def __init__(self, handle, target: Path):
            self._handle = handle
            self._target = target

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self._handle.close()
            return False

        def write(self, data):
            self._handle.write(data[:16])
            self._handle.flush()
            # The manifest must exist at failure time; otherwise cleanup has
            # nothing to remove and the test would pass vacuously.
            assert self._target.exists(), "manifest must exist after the partial write"
            raise OSError("disk full")

    def failing_open(file, mode="r", *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        if "x" in mode and Path(file) == target:
            return _FailingWriter(handle, target)
        return handle

    return failing_open


def _patch_steam_workflow(monkeypatch, root: Path, answers=("game", "", "")):
    """Replace every network/interactive seam of steam_quest_mode()."""
    monkeypatch.setattr("orbshacker.steam._resolve_steam_path", lambda: root)
    monkeypatch.setattr(
        "orbshacker.steam._pick_steam_game",
        lambda query: {"id": APPID, "name": "Workflow Game"},
    )
    monkeypatch.setattr(
        "orbshacker.steam.fetch_steam_app_info",
        lambda appid: {
            "name": "Workflow Game",
            "installdir": "WorkflowGame",
            "executable": "Bin/Game.exe",
            "depot_id": "4322",
        },
    )
    monkeypatch.setattr("orbshacker.steam.loading_animation", lambda *a, **k: None)
    monkeypatch.setattr("orbshacker.steam.time.sleep", lambda *a, **k: None)
    monkeypatch.setattr("orbshacker.steam.get_steam_user_id", lambda: "0")
    remaining = iter(answers)
    monkeypatch.setattr(builtins, "input", lambda *a, **k: next(remaining))


class TestPickWindowsExe:
    def test_finds_first_windows_exe(self):
        launch = {
            "0": {
                "executable": "game.exe",
                "config": {"oslist": "windows"},
            },
            "1": {
                "executable": "game_server.exe",
                "config": {"oslist": "windows"},
            },
        }
        assert _pick_windows_exe(launch) == "game.exe"

    def test_skips_non_windows(self):
        launch = {
            "0": {
                "executable": "game.app",
                "config": {"oslist": "macos"},
            },
            "1": {
                "executable": "game.exe",
                "config": {"oslist": "windows"},
            },
        }
        assert _pick_windows_exe(launch) == "game.exe"

    def test_skips_non_exe(self):
        launch = {
            "0": {
                "executable": "game.sh",
                "config": {"oslist": "windows"},
            },
        }
        assert _pick_windows_exe(launch) is None

    def test_empty_launch(self):
        assert _pick_windows_exe({}) is None

    def test_normalises_backslashes(self):
        launch = {
            "0": {
                "executable": "Bin\\Win64\\game.exe",
                "config": {"oslist": "windows"},
            },
        }
        assert _pick_windows_exe(launch) == "Bin/Win64/game.exe"

    def test_empty_oslist_counts_as_windows(self):
        launch = {
            "0": {
                "executable": "game.exe",
                "config": {"oslist": ""},
            },
        }
        assert _pick_windows_exe(launch) == "game.exe"

    def test_handles_special_symbols_in_launch(self):
        launch = {
            "0": {
                "executable": "Bin\\Win64™\\Game®:Quest.exe",
                "config": {"oslist": "windows"},
            },
        }
        from orbshacker.path_utils import sanitize_relative_path
        raw_exe = _pick_windows_exe(launch)
        assert sanitize_relative_path(raw_exe) == "Bin/Win64/GameQuest.exe"


class TestResolveExecutable:
    def test_fc27_uses_known_executable_without_launch_metadata(self):
        # EA's launch URI doesn't give Steam a normal Windows executable to detect.
        assert _resolve_executable(4080220, "EA SPORTS FC 27", {}) == "fc27.exe"

    def test_other_games_keep_normal_detection(self):
        launch = {
            "0": {
                "executable": "Bin\\game.exe",
                "config": {"oslist": "windows"},
            },
        }
        assert _resolve_executable(1234, "Example Game", launch) == "Bin/game.exe"


class TestManifestPath:
    def test_manifest_path_shape(self, tmp_path):
        root = tmp_path / "Steam"
        assert manifest_path(730, root) == root / "steamapps" / "appmanifest_730.acf"

    def test_manifest_path_does_not_touch_disk(self, tmp_path):
        root = tmp_path / "Steam"
        path = manifest_path(440, root)
        assert not root.exists()
        assert path.name == "appmanifest_440.acf"


class TestGenerateAppmanifest:
    def test_creates_manifest_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("orbshacker.steam.get_steam_user_id", lambda: "76561198000000000")
        root = _steam_root(tmp_path)

        result = generate_appmanifest(1234, "Test Game", "TestGame", root, depot_id="1235")

        assert result == manifest_path(1234, root)
        assert result.exists()
        text = result.read_text(encoding="utf-8")
        assert '"appid"\t\t"1234"' in text
        assert '"name"\t\t"Test Game"' in text
        assert '"installdir"\t\t"TestGame"' in text
        assert '"StateFlags"\t\t"1026"' in text
        assert '"LastOwner"\t\t"76561198000000000"' in text
        assert '"1235"' in text  # staged depot entry

    def test_refuses_when_manifest_exists(self, tmp_path, capsys):
        root = _steam_root(tmp_path)
        acf = manifest_path(1234, root)
        acf.write_bytes(SENTINEL)

        result = generate_appmanifest(1234, "Test Game", "TestGame", root)

        assert result is None
        out = capsys.readouterr().out.lower()
        assert "already exists" in out
        assert "refusing" in out
        assert "no changes were made" in out

    def test_existing_manifest_bytes_remain_unchanged(self, tmp_path):
        root = _steam_root(tmp_path)
        acf = manifest_path(1234, root)
        acf.write_bytes(SENTINEL)

        generate_appmanifest(1234, "Different Name", "DifferentDir", root, depot_id="9999")

        assert acf.read_bytes() == SENTINEL
        assert acf.stat().st_size == len(SENTINEL)

    def test_race_condition_file_appears_before_create(self, tmp_path, monkeypatch):
        """The file is created by a competitor between our check and our open()."""
        root = _steam_root(tmp_path)
        acf = manifest_path(777, root)
        real_open = builtins.open

        def racing_open(file, mode="r", *args, **kwargs):
            if "x" in mode and Path(file) == acf:
                acf.write_bytes(SENTINEL)
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", racing_open)

        result = generate_appmanifest(777, "Race Game", "RaceGame", root)

        assert result is None
        assert acf.read_bytes() == SENTINEL
        assert acf.stat().st_size == len(SENTINEL)

    def test_write_failure_removes_incomplete_file(self, tmp_path, monkeypatch):
        root = _steam_root(tmp_path)
        acf = manifest_path(555, root)
        monkeypatch.setattr(builtins, "open", _failing_open(acf))

        result = generate_appmanifest(555, "Broken Game", "BrokenGame", root)

        assert result is None
        assert not acf.exists()

    def test_write_failure_does_not_touch_steamapps_siblings(self, tmp_path, monkeypatch):
        root = _steam_root(tmp_path)
        sibling = root / "steamapps" / "appmanifest_556.acf"
        sibling.write_bytes(SENTINEL)
        monkeypatch.setattr(builtins, "open", _failing_open(manifest_path(555, root)))

        generate_appmanifest(555, "Broken Game", "BrokenGame", root)

        assert sibling.read_bytes() == SENTINEL


class TestSteamQuestMode:
    def test_workflow_creates_and_registers_manifest(self, tmp_path, monkeypatch):
        root = _steam_root(tmp_path)
        _patch_steam_workflow(monkeypatch, root)
        monkeypatch.setattr("orbshacker.steam.ask_confirm", lambda *a, **k: True)
        faker = MagicMock()

        steam_quest_mode(faker)

        acf = manifest_path(APPID, root)
        assert acf.exists()
        faker.register_created_file.assert_called_once_with(acf)
        expected_exe = root / "steamapps" / "common" / "WorkflowGame" / "Bin" / "Game.exe"
        assert faker.copy_exe_to.call_args[0][0] == expected_exe
        faker.launch_executable.assert_called_once()

    def test_workflow_refuses_existing_manifest(self, tmp_path, monkeypatch, capsys):
        root = _steam_root(tmp_path)
        acf = manifest_path(APPID, root)
        acf.write_bytes(SENTINEL)
        # query, then the executable-override prompt; the refusal happens before confirm
        _patch_steam_workflow(monkeypatch, root, answers=("game", ""))
        confirm = MagicMock(return_value=True)
        monkeypatch.setattr("orbshacker.steam.ask_confirm", confirm)
        faker = MagicMock()

        steam_quest_mode(faker)

        confirm.assert_not_called()
        faker.register_created_file.assert_not_called()
        faker.copy_exe_to.assert_not_called()
        faker.launch_executable.assert_not_called()
        assert acf.read_bytes() == SENTINEL
        out = capsys.readouterr().out.lower()
        assert "already exists" in out
        assert "will not be overwritten" in out

    def test_partial_write_failure_cleans_up_and_preserves_siblings(self, tmp_path, monkeypatch):
        root = _steam_root(tmp_path)
        acf = manifest_path(APPID, root)
        sibling = root / "steamapps" / "appmanifest_9999.acf"
        sibling.write_bytes(SENTINEL)
        _patch_steam_workflow(monkeypatch, root)
        monkeypatch.setattr("orbshacker.steam.ask_confirm", lambda *a, **k: True)
        # Fails after writing part of the manifest; _FailingWriter asserts the
        # file exists before raising, so the cleanup path is genuinely exercised.
        monkeypatch.setattr(builtins, "open", _failing_open(acf))
        faker = MagicMock()

        steam_quest_mode(faker)

        assert not acf.exists()                                  # incomplete file removed
        assert sibling.read_bytes() == SENTINEL                  # unrelated manifest untouched
        faker.register_created_file.assert_not_called()          # never scheduled for cleanup
        faker.copy_exe_to.assert_not_called()
        faker.launch_executable.assert_not_called()
