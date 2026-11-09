"""Tests for DiscordGamesDB loading – validation, fallback and atomicity."""

import copy
import logging

import pytest

from orbshacker import config
from orbshacker.discord_db import DiscordGamesDB, normalise_game, validate_catalogue
from orbshacker.errors import DatabaseLoadError, NetworkError


DISCORD_URL = config.DISCORD_API_URL
GITHUB_URL = config.GITHUB_BACKUP_URL

VALID_GAMES = [
    {
        "id": "1",
        "name": "Minecraft",
        "aliases": ["MC"],
        "executables": [{"os": "win32", "name": "javaw.exe"}],
    },
    {
        "id": "2",
        "name": "Minecraft Dungeons",
        "aliases": [],
        "executables": [{"os": "win32", "name": "Dungeons.exe"}],
    },
]


class FakeFetcher:
    """Deterministic replacement for discord_db.fetch_json."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def _quiet_loading(monkeypatch):
    """Skip the spinner delay; it is unrelated to these tests."""
    monkeypatch.setattr("orbshacker.discord_db.loading_animation", lambda *a, **k: None)


def _use_fetcher(monkeypatch, responses):
    fetcher = FakeFetcher(responses)
    monkeypatch.setattr("orbshacker.discord_db.fetch_json", fetcher)
    return fetcher


def _blank_db():
    """A DiscordGamesDB without running __init__ (which would hit the network)."""
    db = object.__new__(DiscordGamesDB)
    db.games = []
    db.source = None
    db.last_error = None
    return db


class TestLoading:
    def test_loads_valid_catalogue(self, monkeypatch):
        fetcher = _use_fetcher(monkeypatch, {DISCORD_URL: VALID_GAMES})

        db = DiscordGamesDB()

        assert [g["id"] for g in db.games] == ["1", "2"]
        assert db.source == "Discord Official API"
        assert fetcher.calls == [DISCORD_URL]

    def test_falls_back_when_discord_fails(self, monkeypatch):
        fetcher = _use_fetcher(
            monkeypatch,
            {DISCORD_URL: NetworkError("boom"), GITHUB_URL: VALID_GAMES},
        )

        db = DiscordGamesDB()

        assert db.source == "GitHub Backup"
        assert fetcher.calls == [DISCORD_URL, GITHUB_URL]

    @pytest.mark.parametrize("payload", [{}, None, "games", 123])
    def test_non_list_payload_falls_back(self, monkeypatch, payload):
        _use_fetcher(monkeypatch, {DISCORD_URL: payload, GITHUB_URL: VALID_GAMES})

        db = DiscordGamesDB()

        assert db.source == "GitHub Backup"
        assert len(db.games) == 2

    def test_empty_catalogue_tries_backup(self, monkeypatch):
        fetcher = _use_fetcher(monkeypatch, {DISCORD_URL: [], GITHUB_URL: VALID_GAMES})

        db = DiscordGamesDB()

        assert db.source == "GitHub Backup"
        assert fetcher.calls == [DISCORD_URL, GITHUB_URL]

    def test_both_sources_fail_with_clear_message(self, monkeypatch):
        _use_fetcher(
            monkeypatch,
            {
                DISCORD_URL: NetworkError("discord down"),
                GITHUB_URL: NetworkError("github down"),
            },
        )
        db = _blank_db()

        with pytest.raises(DatabaseLoadError) as excinfo:
            db._load()

        message = str(excinfo.value)
        assert "Discord Official API" in message and "discord down" in message
        assert "GitHub Backup" in message and "github down" in message
        assert db.games == []
        assert db.last_error is not None

    def test_backup_payload_is_validated_too(self, monkeypatch):
        _use_fetcher(monkeypatch, {DISCORD_URL: NetworkError("down"), GITHUB_URL: {}})
        db = _blank_db()

        with pytest.raises(DatabaseLoadError) as excinfo:
            db._load()

        assert "GitHub Backup payload is not a list" in str(excinfo.value)


class TestValidation:
    def test_invalid_rows_are_dropped(self, monkeypatch):
        payload = [
            VALID_GAMES[0],
            None,
            {},
            {"id": "x"},
            {"name": "No Id"},
            {"id": "y", "name": "   "},
        ]
        _use_fetcher(monkeypatch, {DISCORD_URL: payload})

        db = DiscordGamesDB()

        assert len(db.games) == 1
        assert db.games[0]["id"] == "1"

    def test_boolean_id_rejected_and_int_id_converted(self, monkeypatch):
        payload = [
            {"id": True, "name": "Boolean"},
            {"id": 42, "name": "Integer"},
            {"id": "  ", "name": "Blank"},
            {"id": "7", "name": "  Spaced Name  "},
        ]
        _use_fetcher(monkeypatch, {DISCORD_URL: payload})

        db = DiscordGamesDB()

        assert [g["id"] for g in db.games] == ["42", "7"]
        assert db.games[0]["name"] == "Integer"
        assert db.games[1]["name"] == "Spaced Name"

    def test_malformed_optional_metadata_becomes_empty(self, monkeypatch):
        payload = [{
            "id": "9",
            "name": "No Metadata",
            "aliases": "MC",
            "executables": [{"os": "win32"}, "not-a-dict", {"name": "Game.exe"}],
        }]
        _use_fetcher(monkeypatch, {DISCORD_URL: payload})

        db = DiscordGamesDB()

        game = db.games[0]
        assert game["aliases"] == []
        assert game["executables"] == []
        assert db.get_win32_executable(game) is None
        assert db.search_games("No Metadata")[0]["id"] == "9"

    def test_duplicate_ids_are_preserved(self, monkeypatch):
        payload = [
            {"id": "1", "name": "First"},
            {"id": "1", "name": "Second"},
        ]
        _use_fetcher(monkeypatch, {DISCORD_URL: payload})

        db = DiscordGamesDB()

        assert len(db.games) == 2
        assert [g["name"] for g in db.games] == ["First", "Second"]

    def test_payload_is_not_mutated(self, monkeypatch):
        payload = copy.deepcopy(VALID_GAMES)
        snapshot = copy.deepcopy(payload)
        _use_fetcher(monkeypatch, {DISCORD_URL: payload})

        DiscordGamesDB()

        assert payload == snapshot

    def test_rejected_records_logged_at_debug(self, monkeypatch, caplog):
        _use_fetcher(monkeypatch, {DISCORD_URL: [VALID_GAMES[0], {}]})

        with caplog.at_level(logging.DEBUG, logger="orbshacker.discord_db"):
            DiscordGamesDB()

        assert "rejected 1" in caplog.text

    def test_search_behaviour_unchanged(self, monkeypatch):
        _use_fetcher(monkeypatch, {DISCORD_URL: VALID_GAMES})

        db = DiscordGamesDB()

        assert [g["id"] for g in db.search_games("mine")] == ["1", "2"]
        assert db.search_games("MC")[0]["id"] == "1"
        assert db.search_games("nonexistent") == []


class TestNormalisation:
    def test_requires_mapping(self):
        assert normalise_game(None) is None
        assert normalise_game(["id"]) is None

    def test_returns_copy_with_expected_fields(self):
        raw = VALID_GAMES[0]
        normalised = normalise_game(raw)

        assert normalised == raw
        assert normalised is not raw

    def test_validate_raises_for_empty_payload(self):
        with pytest.raises(DatabaseLoadError):
            validate_catalogue([], source_label="Discord Official API")


class TestAtomicity:
    def test_failed_reload_keeps_last_known_good_catalogue(self, monkeypatch):
        db = _blank_db()
        db.games = copy.deepcopy(VALID_GAMES)
        db.source = "Discord Official API"
        _use_fetcher(
            monkeypatch,
            {DISCORD_URL: NetworkError("down"), GITHUB_URL: {}},
        )

        with pytest.raises(DatabaseLoadError):
            db._load()

        assert len(db.games) == 2
        assert db.source == "Discord Official API"
