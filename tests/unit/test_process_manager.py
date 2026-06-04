from types import SimpleNamespace

from openhachimi_agent.service.process import ProcessManager


class FakeRunningProcess:
    def __init__(self, session_id: str | None, running: bool = True) -> None:
        self.session_id = session_id
        self.running = running
        self.cleaned = False

    def is_running(self) -> bool:
        return self.running

    def cleanup(self) -> None:
        self.cleaned = True
        self.running = False


def test_process_manager_terminate_session_only_stops_matching_processes():
    manager = ProcessManager()
    target = FakeRunningProcess("target")
    other = FakeRunningProcess("other")
    finished_target = FakeRunningProcess("target", running=False)
    manager._processes = {
        "target": target,
        "other": other,
        "finished": finished_target,
    }

    count = manager.terminate_session("target")

    assert count == 1
    assert target.cleaned is True
    assert other.cleaned is False
    assert finished_target.cleaned is False
