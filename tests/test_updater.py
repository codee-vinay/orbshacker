"""Tests for updater.py – version lookup, integrity checks and replacement safety."""

import hashlib
import os
from pathlib import Path

import pytest
import requests

from orbshacker import updater
from orbshacker.updater import (
    UpdateError,
    _find_checksum_asset,
    _parse_checksum,
    _sha256,
    _validate_executable,
)


def _exe_payload(marker: bytes = b"OLD") -> bytes:
    return b"MZ" + marker * 1024


class FakeStreamResponse:
    def __init__(self, chunks, *, content_length=None, content_encoding=None, error=None):
        self._chunks = list(chunks)
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        if content_encoding is not None:
            self.headers["Content-Encoding"] = content_encoding
        self._error = error

    def raise_for_status(self):
        if self._error is not None:
            raise self._error

    def iter_content(self, chunk_size):
        for chunk in self._chunks:
            yield chunk


class FakeTextResponse:
    def __init__(self, text, *, error=None):
        self.text = text
        self._error = error

    def raise_for_status(self):
        if self._error is not None:
            raise self._error


def _mock_responses(monkeypatch, responses):
    iterator = iter(responses)
    monkeypatch.setattr(updater.requests, "get", lambda *args, **kwargs: next(iterator))


def _flaky_replace(fail_calls: set[int], real_replace):
    calls = {"count": 0}

    def replace(src, dst):
        calls["count"] += 1
        if calls["count"] in fail_calls:
            raise OSError(f"simulated replace failure #{calls['count']}")
        return real_replace(src, dst)

    return replace


@pytest.fixture
def replace_env(tmp_path, monkeypatch):
    """A fake current executable and downloaded executable, never the real one."""
    current = tmp_path / "orbshacker.exe"
    new = tmp_path / "download.exe"
    current.write_bytes(_exe_payload(b"OLD"))
    new.write_bytes(_exe_payload(b"NEW"))

    monkeypatch.setattr(updater.sys, "executable", str(current))
    monkeypatch.setattr(updater.ui, "print_color", lambda *args, **kwargs: None)
    return current, new


@pytest.fixture
def startfile_calls(monkeypatch):
    calls = []

    def fake_startfile(path):
        calls.append(path)

    monkeypatch.setattr(updater.os, "startfile", fake_startfile, raising=False)
    return calls


class TestSha256:
    def test_consistent_hash(self, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_text("hello world")
        h1 = _sha256(f)
        h2 = _sha256(f)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex digest length

    def test_different_content_different_hash(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("aaa")
        f2.write_text("bbb")
        assert _sha256(f1) != _sha256(f2)


class TestFindChecksumAsset:
    EXE = {"name": "orbshacker.exe", "browser_download_url": "exe-url"}

    def test_finds_matching_asset(self):
        checksum = {"name": "orbshacker.exe.sha256", "browser_download_url": "sum-url"}
        assert _find_checksum_asset([self.EXE, checksum], self.EXE) == checksum

    def test_is_case_insensitive(self):
        checksum = {"name": "ORBSHACKER.EXE.SHA256", "browser_download_url": "sum-url"}
        assert _find_checksum_asset([self.EXE, checksum], self.EXE) == checksum

    def test_missing_asset_returns_none(self):
        other = {"name": "orbshacker.exe", "browser_download_url": "exe-url"}
        assert _find_checksum_asset([other], self.EXE) is None

    def test_rejects_similarly_named_assets(self):
        assets = [
            self.EXE,
            {"name": "orbshacker.exe.sha256.txt", "browser_download_url": "a"},
            {"name": "other.exe.sha256", "browser_download_url": "b"},
            {"name": "SHA256SUMS", "browser_download_url": "c"},
        ]
        assert _find_checksum_asset(assets, self.EXE) is None

    def test_empty_exe_name_returns_none(self):
        assert _find_checksum_asset([], {"name": "", "browser_download_url": "x"}) is None


class TestParseChecksum:
    digest = "a" * 64

    def test_accepts_bare_digest(self):
        assert _parse_checksum(self.digest) == self.digest

    def test_accepts_sha256sum_format(self):
        assert _parse_checksum(f"{self.digest}  orbshacker.exe\n") == self.digest

    def test_accepts_bom_uppercase_and_crlf(self):
        text = f"\ufeff{self.digest.upper()} *orbshacker.exe\r\n"
        assert _parse_checksum(text) == self.digest

    def test_ignores_trailing_lines(self):
        assert _parse_checksum(f"{self.digest}\nsecond line") == self.digest

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   \n\t",
            "not-a-checksum",
            "a" * 63,
            "a" * 65,
            "g" * 64,
            "0x" + "a" * 64,
        ],
    )
    def test_rejects_malformed_checksums(self, text):
        with pytest.raises(UpdateError, match="checksum file"):
            _parse_checksum(text)


class TestDownloadFile:
    def test_writes_without_content_length(self, tmp_path, monkeypatch):
        _mock_responses(monkeypatch, [FakeStreamResponse([b"abc", b"def"])])
        dest = tmp_path / "file.bin"
        updater._download_file("https://example/file", dest)
        assert dest.read_bytes() == b"abcdef"

    def test_accepts_matching_content_length(self, tmp_path, monkeypatch):
        _mock_responses(monkeypatch, [FakeStreamResponse([b"abc", b"def"], content_length=6)])
        dest = tmp_path / "file.bin"
        updater._download_file("https://example/file", dest)
        assert dest.read_bytes() == b"abcdef"

    def test_rejects_truncated_download(self, tmp_path, monkeypatch):
        _mock_responses(monkeypatch, [FakeStreamResponse([b"abc"], content_length=10)])
        dest = tmp_path / "file.bin"
        with pytest.raises(UpdateError, match="incomplete download"):
            updater._download_file("https://example/file", dest)

    def test_rejects_oversized_download(self, tmp_path, monkeypatch):
        _mock_responses(monkeypatch, [FakeStreamResponse([b"abcdef"], content_length=2)])
        dest = tmp_path / "file.bin"
        with pytest.raises(UpdateError, match="incomplete download"):
            updater._download_file("https://example/file", dest)

    def test_ignores_content_length_for_encoded_response(self, tmp_path, monkeypatch):
        response = FakeStreamResponse([b"abcdef"], content_length=2, content_encoding="gzip")
        _mock_responses(monkeypatch, [response])
        dest = tmp_path / "file.bin"
        updater._download_file("https://example/file", dest)
        assert dest.read_bytes() == b"abcdef"

    def test_ignores_unparseable_content_length(self, tmp_path, monkeypatch):
        _mock_responses(monkeypatch, [FakeStreamResponse([b"abc"], content_length="unknown")])
        dest = tmp_path / "file.bin"
        updater._download_file("https://example/file", dest)
        assert dest.read_bytes() == b"abc"

    def test_propagates_http_errors(self, tmp_path, monkeypatch):
        _mock_responses(monkeypatch, [FakeStreamResponse([], error=requests.HTTPError("500"))])
        dest = tmp_path / "file.bin"
        with pytest.raises(requests.HTTPError):
            updater._download_file("https://example/file", dest)
        assert not dest.exists()


class TestDownloadVerified:
    def test_accepts_matching_checksum(self, tmp_path, monkeypatch):
        payload = _exe_payload()
        digest = hashlib.sha256(payload).hexdigest()
        _mock_responses(
            monkeypatch,
            [
                FakeStreamResponse([payload], content_length=len(payload)),
                FakeTextResponse(f"{digest}  orbshacker.exe\n"),
            ],
        )
        dest = tmp_path / "orbshacker.exe"
        updater._download_verified("https://example/exe", "https://example/exe.sha256", dest)
        assert dest.read_bytes() == payload

    def test_rejects_checksum_mismatch_and_removes_download(self, tmp_path, monkeypatch):
        payload = _exe_payload()
        _mock_responses(
            monkeypatch,
            [
                FakeStreamResponse([payload], content_length=len(payload)),
                FakeTextResponse("0" * 64),
            ],
        )
        dest = tmp_path / "orbshacker.exe"
        with pytest.raises(UpdateError, match="checksum mismatch"):
            updater._download_verified("https://example/exe", "https://example/exe.sha256", dest)
        assert not dest.exists()

    def test_rejects_malformed_checksum_and_removes_download(self, tmp_path, monkeypatch):
        payload = _exe_payload()
        _mock_responses(
            monkeypatch,
            [
                FakeStreamResponse([payload], content_length=len(payload)),
                FakeTextResponse("definitely not a digest"),
            ],
        )
        dest = tmp_path / "orbshacker.exe"
        with pytest.raises(UpdateError, match="checksum file is malformed"):
            updater._download_verified("https://example/exe", "https://example/exe.sha256", dest)
        assert not dest.exists()

    def test_failed_executable_download_removes_partial_file(self, tmp_path, monkeypatch):
        _mock_responses(
            monkeypatch,
            [FakeStreamResponse([b"MZ" + b"\x00" * 10], content_length=9999)],
        )
        dest = tmp_path / "orbshacker.exe"
        with pytest.raises(UpdateError, match="incomplete download"):
            updater._download_verified("https://example/exe", "https://example/exe.sha256", dest)
        assert not dest.exists()

    def test_failed_checksum_download_removes_download(self, tmp_path, monkeypatch):
        payload = _exe_payload()
        _mock_responses(
            monkeypatch,
            [
                FakeStreamResponse([payload], content_length=len(payload)),
                FakeTextResponse("", error=requests.HTTPError("404")),
            ],
        )
        dest = tmp_path / "orbshacker.exe"
        with pytest.raises(requests.HTTPError):
            updater._download_verified("https://example/exe", "https://example/exe.sha256", dest)
        assert not dest.exists()


class TestValidateExecutable:
    def test_accepts_pe_file(self, tmp_path):
        path = tmp_path / "orbshacker.exe"
        path.write_bytes(_exe_payload())
        _validate_executable(path)

    def test_rejects_missing_file(self, tmp_path):
        with pytest.raises(UpdateError, match="missing"):
            _validate_executable(tmp_path / "missing.exe")

    def test_rejects_tiny_file(self, tmp_path):
        path = tmp_path / "tiny.exe"
        path.write_bytes(b"MZ")
        with pytest.raises(UpdateError, match="too small"):
            _validate_executable(path)

    def test_rejects_non_pe_file(self, tmp_path):
        path = tmp_path / "page.exe"
        path.write_bytes(b"<html>" + b" " * 2048)
        with pytest.raises(UpdateError, match="MZ header"):
            _validate_executable(path)


class TestReplaceExe:
    def test_success_keeps_backup_for_recovery(self, replace_env, startfile_calls):
        current, new = replace_env
        old = current.with_suffix(".old")
        new_bytes = new.read_bytes()

        with pytest.raises(SystemExit) as excinfo:
            updater._replace_exe(new)

        assert excinfo.value.code == 0
        assert current.read_bytes() == new_bytes
        assert old.exists()
        assert old.read_bytes() == _exe_payload(b"OLD")
        assert startfile_calls == [str(current)]

    def test_rejects_invalid_executable_before_touching_current(self, replace_env):
        current, new = replace_env
        new.write_bytes(b"<html>" + b" " * 2048)

        with pytest.raises(UpdateError, match="MZ header"):
            updater._replace_exe(new)

        assert current.read_bytes() == _exe_payload(b"OLD")
        assert not current.with_suffix(".old").exists()

    def test_move_aside_failure_leaves_current_untouched(self, replace_env, monkeypatch):
        current, new = replace_env
        real_replace = os.replace
        monkeypatch.setattr(updater.os, "replace", _flaky_replace({1}, real_replace))

        with pytest.raises(UpdateError, match="move the current executable aside"):
            updater._replace_exe(new)

        assert current.read_bytes() == _exe_payload(b"OLD")
        assert not current.with_suffix(".old").exists()
        assert new.exists()

    def test_install_failure_restores_backup(self, replace_env, monkeypatch):
        current, new = replace_env
        real_replace = os.replace
        monkeypatch.setattr(updater.os, "replace", _flaky_replace({2}, real_replace))

        with pytest.raises(UpdateError, match="cannot install"):
            updater._replace_exe(new)

        assert current.read_bytes() == _exe_payload(b"OLD")
        assert not current.with_suffix(".old").exists()
        assert new.exists()

    def test_install_and_restore_failure_preserves_backup(self, replace_env, monkeypatch):
        current, new = replace_env
        old = current.with_suffix(".old")
        real_replace = os.replace
        monkeypatch.setattr(updater.os, "replace", _flaky_replace({2, 3}, real_replace))

        with pytest.raises(UpdateError, match="preserved at"):
            updater._replace_exe(new)

        assert not current.exists()
        assert old.exists()
        assert old.read_bytes() == _exe_payload(b"OLD")
        assert new.exists()

    def test_launch_failure_restores_backup(self, replace_env, monkeypatch):
        current, new = replace_env

        def fail_startfile(path):
            raise OSError("blocked by policy")

        monkeypatch.setattr(updater.os, "startfile", fail_startfile, raising=False)

        with pytest.raises(UpdateError, match="could not be started"):
            updater._replace_exe(new)

        assert current.read_bytes() == _exe_payload(b"OLD")
        assert not current.with_suffix(".old").exists()
        assert not new.exists()

    def test_launch_failure_preserves_backup_when_restore_fails(self, replace_env, monkeypatch):
        current, new = replace_env
        old = current.with_suffix(".old")
        real_replace = os.replace
        monkeypatch.setattr(updater.os, "replace", _flaky_replace({3}, real_replace))

        def fail_startfile(path):
            raise OSError("blocked by policy")

        monkeypatch.setattr(updater.os, "startfile", fail_startfile, raising=False)

        with pytest.raises(UpdateError, match="preserved at"):
            updater._replace_exe(new)

        assert not current.exists()
        assert old.exists()
        assert old.read_bytes() == _exe_payload(b"OLD")


class TestRepositoryLookup:
    def test_latest_release_uses_configured_repository(self, monkeypatch):
        requested_urls = []

        class JsonResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"tag_name": "v1.0.0", "assets": []}

        def fake_get(url, **kwargs):
            requested_urls.append(url)
            return JsonResponse()

        monkeypatch.setattr(updater.requests, "get", fake_get)
        monkeypatch.setattr(updater.config, "GITHUB_REPO_OWNER", "codee-vinay")
        monkeypatch.setattr(updater.config, "GITHUB_REPO_NAME", "orbshacker")

        release = updater._get_latest_release()

        assert release == {"tag_name": "v1.0.0", "assets": []}
        assert requested_urls == [
            "https://api.github.com/repos/codee-vinay/orbshacker/releases/latest"
        ]


class TestAutoUpdate:
    def _patch_environment(self, monkeypatch, messages):
        monkeypatch.setattr(updater, "is_frozen", lambda: True)
        monkeypatch.setattr(updater, "_cleanup_old_exe", lambda path: None)
        monkeypatch.setattr(updater.ui, "loading_animation", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            updater.ui,
            "print_color",
            lambda text, *args, **kwargs: messages.append(text),
        )
        monkeypatch.setattr(updater.config, "VERSION", "1.0.0")

    def _release(self, assets):
        return {"tag_name": "v9.9.9", "assets": assets}

    EXE_ASSET = {"name": "orbshacker.exe", "browser_download_url": "https://example/exe"}
    SUM_ASSET = {
        "name": "orbshacker.exe.sha256",
        "browser_download_url": "https://example/exe.sha256",
    }

    def test_missing_checksum_asset_aborts_without_downloading(self, monkeypatch):
        messages = []
        self._patch_environment(monkeypatch, messages)
        monkeypatch.setattr(updater, "_get_latest_release", lambda: self._release([self.EXE_ASSET]))

        def unexpected_get(*args, **kwargs):
            raise AssertionError("no download should start without a checksum asset")

        monkeypatch.setattr(updater.requests, "get", unexpected_get)

        updater.auto_update()

        assert any("sha256 is missing" in message for message in messages)

    def test_checksum_mismatch_prevents_replacement(self, monkeypatch):
        messages = []
        self._patch_environment(monkeypatch, messages)
        monkeypatch.setattr(
            updater,
            "_get_latest_release",
            lambda: self._release([self.EXE_ASSET, self.SUM_ASSET]),
        )

        payload = _exe_payload()
        _mock_responses(
            monkeypatch,
            [
                FakeStreamResponse([payload], content_length=len(payload)),
                FakeTextResponse("0" * 64),
            ],
        )

        replacements = []
        monkeypatch.setattr(updater, "_replace_exe", lambda path: replacements.append(path))

        updater.auto_update()

        assert replacements == []
        assert any("checksum mismatch" in message for message in messages)

    def test_verified_update_is_installed(self, monkeypatch):
        messages = []
        self._patch_environment(monkeypatch, messages)
        monkeypatch.setattr(
            updater,
            "_get_latest_release",
            lambda: self._release([self.EXE_ASSET, self.SUM_ASSET]),
        )

        payload = _exe_payload()
        digest = hashlib.sha256(payload).hexdigest()
        _mock_responses(
            monkeypatch,
            [
                FakeStreamResponse([payload], content_length=len(payload)),
                FakeTextResponse(f"{digest}  orbshacker.exe\n"),
            ],
        )

        replacements = []
        monkeypatch.setattr(
            updater,
            "_replace_exe",
            lambda path: replacements.append(path.read_bytes()),
        )

        updater.auto_update()

        assert replacements == [payload]
        assert not any("aborted" in message for message in messages)
