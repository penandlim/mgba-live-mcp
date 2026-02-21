from __future__ import annotations

import pytest

import mgba_live_mcp.live_controller as live_controller
from mgba_live_mcp.live_controller import LiveControllerClient


class _FakeProc:
    def __init__(self, *, stdout: bytes, stderr: bytes, returncode: int) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True


@pytest.mark.anyio
async def test_run_raises_timeout_and_kills_process(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc(stdout=b"{}", stderr=b"", returncode=0)

    async def fake_create(*_args, **_kwargs):
        return proc

    async def fake_wait_for(_coro, timeout: float):  # pragma: no cover - branch-only helper
        del timeout
        _coro.close()
        raise TimeoutError

    monkeypatch.setattr(live_controller.asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(live_controller.asyncio, "wait_for", fake_wait_for)

    client = LiveControllerClient(module_name="x.y")
    with pytest.raises(RuntimeError, match="timed out"):
        await client.run("status", [], timeout=0.01)
    assert proc.killed is True


@pytest.mark.anyio
async def test_run_raises_on_non_zero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc(stdout=b"bad", stderr=b"err", returncode=2)

    async def fake_create(*_args, **_kwargs):
        return proc

    async def passthrough(coro, timeout: float):
        del timeout
        return await coro

    monkeypatch.setattr(live_controller.asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(live_controller.asyncio, "wait_for", passthrough)

    client = LiveControllerClient(module_name="x.y")
    with pytest.raises(RuntimeError, match="Command failed"):
        await client.run("status", [])


@pytest.mark.anyio
async def test_run_raises_when_stdout_empty_with_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc(stdout=b"  \n", stderr=b"bridge error\n", returncode=0)

    async def fake_create(*_args, **_kwargs):
        return proc

    async def passthrough(coro, timeout: float):
        del timeout
        return await coro

    monkeypatch.setattr(live_controller.asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(live_controller.asyncio, "wait_for", passthrough)

    client = LiveControllerClient(module_name="x.y")
    with pytest.raises(RuntimeError, match="bridge error"):
        await client.run("status", [])


@pytest.mark.anyio
async def test_run_raises_when_no_output(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc(stdout=b"  ", stderr=b" ", returncode=0)

    async def fake_create(*_args, **_kwargs):
        return proc

    async def passthrough(coro, timeout: float):
        del timeout
        return await coro

    monkeypatch.setattr(live_controller.asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(live_controller.asyncio, "wait_for", passthrough)

    client = LiveControllerClient(module_name="x.y")
    with pytest.raises(RuntimeError, match="No output"):
        await client.run("status", [])


@pytest.mark.anyio
async def test_run_raises_on_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc(stdout=b"{", stderr=b"", returncode=0)

    async def fake_create(*_args, **_kwargs):
        return proc

    async def passthrough(coro, timeout: float):
        del timeout
        return await coro

    monkeypatch.setattr(live_controller.asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(live_controller.asyncio, "wait_for", passthrough)

    client = LiveControllerClient(module_name="x.y")
    with pytest.raises(RuntimeError, match="Invalid JSON"):
        await client.run("status", [])
