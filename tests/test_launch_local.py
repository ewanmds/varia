import io
import json

from tools import launch_local


class Probe:
    occupied = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def settimeout(self, value):
        pass

    def connect_ex(self, address):
        return 0 if self.occupied else 10061


def test_existing_real_interface_does_not_start_second_process(monkeypatch):
    monkeypatch.setattr(launch_local.socket, "socket", Probe)
    monkeypatch.setattr(launch_local.urllib.request, "urlopen", lambda *a, **k:
                        io.BytesIO(json.dumps({"mode": "pair", "paper": False, "running": False}).encode()))
    monkeypatch.setattr(launch_local.subprocess, "call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("second server")))
    assert launch_local.main() == 0


def test_foreign_server_port_is_not_reused_or_replaced(monkeypatch):
    monkeypatch.setattr(launch_local.socket, "socket", Probe)
    monkeypatch.setattr(launch_local.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b'{}'))
    monkeypatch.setattr(launch_local.subprocess, "call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("new process")))
    assert launch_local.main() == 1


def test_free_port_launches_interface_without_autostart(monkeypatch):
    class Free(Probe):
        occupied = False

    calls = []
    monkeypatch.setattr(launch_local.socket, "socket", Free)
    monkeypatch.setattr(launch_local.subprocess, "call", lambda args, **kwargs: calls.append(args) or 0)
    assert launch_local.main() == 0
    assert len(calls) == 1 and "--start-pair" not in calls[0]
