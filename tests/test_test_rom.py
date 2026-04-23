from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from mgba_live_mcp import test_rom


def test_default_test_rom_path_uses_repo_roms_dir(tmp_path: Path) -> None:
    path = test_rom.default_test_rom_path(tmp_path)
    assert path == (tmp_path / "roms" / test_rom.TEST_ROM_NAME).resolve()


def test_verify_test_rom_returns_resolved_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"verified-rom"
    rom = tmp_path / test_rom.TEST_ROM_NAME
    rom.write_bytes(payload)
    monkeypatch.setattr(test_rom, "TEST_ROM_SHA256", hashlib.sha256(payload).hexdigest())

    assert test_rom.verify_test_rom(rom) == rom.resolve()


def test_verify_test_rom_raises_for_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Test ROM not found"):
        test_rom.verify_test_rom(tmp_path / "missing.gbc")


def test_verify_test_rom_raises_for_checksum_mismatch(tmp_path: Path) -> None:
    rom = tmp_path / test_rom.TEST_ROM_NAME
    rom.write_bytes(b"wrong")

    with pytest.raises(ValueError, match="checksum mismatch"):
        test_rom.verify_test_rom(rom)


class _FakeResponse:
    def __init__(self, payload: bytes, *, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


def test_fetch_test_rom_downloads_and_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"open-source-rom"
    monkeypatch.setattr(test_rom, "TEST_ROM_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(
        test_rom.urllib.request,
        "urlopen",
        lambda request, timeout=30.0: _FakeResponse(payload),
    )

    out = tmp_path / "roms" / test_rom.TEST_ROM_NAME
    assert test_rom.fetch_test_rom(out=out) == out.resolve()
    assert out.read_bytes() == payload


def test_fetch_test_rom_rejects_bad_checksum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        test_rom.urllib.request,
        "urlopen",
        lambda request, timeout=30.0: _FakeResponse(b"bad"),
    )

    out = tmp_path / "roms" / test_rom.TEST_ROM_NAME
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        test_rom.fetch_test_rom(out=out)
    assert not out.exists()


def test_main_path_prints_repo_relative_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(test_rom, "repo_root", lambda: tmp_path)

    assert test_rom.main(["path"]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "roms" / test_rom.TEST_ROM_NAME)
