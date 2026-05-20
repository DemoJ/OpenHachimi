# pyrefly: ignore [missing-import]
import asyncio
import time
from types import SimpleNamespace

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).parents[2] / "openhachimi_agent" / "tools"
_tools_pkg = types.ModuleType("openhachimi_agent.tools")
_tools_pkg.__path__ = [str(_TOOLS_DIR)]
sys.modules["openhachimi_agent.tools"] = _tools_pkg

_command_spec = importlib.util.spec_from_file_location(
    "openhachimi_agent.tools.command",
    _TOOLS_DIR / "command.py",
)
_command_module = importlib.util.module_from_spec(_command_spec)
assert _command_spec.loader is not None
_command_spec.loader.exec_module(_command_module)
send_command_input = _command_module.send_command_input
run_command = _command_module.run_command
MAX_RUN_COMMAND_WAIT_SECONDS = _command_module.MAX_RUN_COMMAND_WAIT_SECONDS


class FakeProcess:
    def __init__(self, output: str = "") -> None:
        self.id = "cmd"
        self.output = output
        self.running = True
        self.inputs: list[str] = []

    def is_running(self) -> bool:
        return self.running

    def send_input(self, text: str) -> None:
        self.inputs.append(text)

    def get_output(self, limit: int = 12000) -> tuple[str, bool]:
        return self.output, False


class FakeProcessManager:
    def __init__(self, process: FakeProcess | None) -> None:
        self.process = process

    def start_process(self, command, cwd, shell_name):
        return self.process

    def get_process(self, command_id: str) -> FakeProcess | None:
        return self.process


def make_ctx(process: FakeProcess):
    deps = SimpleNamespace(
        base_dir=Path.cwd(),
        process_manager=FakeProcessManager(process),
    )
    return SimpleNamespace(deps=deps)


@pytest.mark.asyncio
async def test_send_command_input_returns_when_output_changes():
    process = FakeProcess("prompt")

    async def update_output():
        await asyncio.sleep(0.05)
        process.output = "promptdone"

    updater = asyncio.create_task(update_output())
    start = time.monotonic()
    result = await send_command_input(make_ctx(process), "cmd", text="y", wait_seconds=2.0)
    elapsed = time.monotonic() - start
    await updater

    assert result["output"] == "promptdone"
    assert process.inputs == ["y"]
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_send_command_input_wait_seconds_zero_does_not_wait():
    process = FakeProcess("prompt")
    start = time.monotonic()
    result = await send_command_input(make_ctx(process), "cmd", special_key="enter", wait_seconds=0)
    elapsed = time.monotonic() - start

    assert result["output"] == "prompt"
    assert process.inputs == ["\r"]
    assert elapsed < 0.2


@pytest.mark.asyncio
async def test_run_command_wait_seconds_zero_returns_immediately(monkeypatch):
    process = FakeProcess("started")

    monkeypatch.setattr(_command_module, "assert_safe_command", lambda command: None)
    monkeypatch.setattr(_command_module, "get_command_shell", lambda: (["shell", "-c"], "test-shell"))

    start = time.monotonic()
    result = await run_command(make_ctx(process), "long task", wait_seconds=0)
    elapsed = time.monotonic() - start

    assert result["command_id"] == "cmd"
    assert result["is_running"] is True
    assert result["output"] == "started"
    assert elapsed < 0.2


@pytest.mark.asyncio
async def test_run_command_clamps_wait_seconds_to_max(monkeypatch):
    process = FakeProcess("started")
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(_command_module, "MAX_RUN_COMMAND_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(_command_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(_command_module, "assert_safe_command", lambda command: None)
    monkeypatch.setattr(_command_module, "get_command_shell", lambda: (["shell", "-c"], "test-shell"))

    result = await run_command(make_ctx(process), "long task", wait_seconds=999)

    assert result["is_running"] is True
    assert sleeps == []


def test_run_command_max_wait_seconds_is_120():
    assert MAX_RUN_COMMAND_WAIT_SECONDS == 120
