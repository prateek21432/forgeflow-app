"""The local launcher must work without invoking Docker."""

import httpx
import pytest

from scripts import manage


def test_local_services_never_call_docker(monkeypatch):
    monkeypatch.setattr(manage.settings, "execution_mode", "local")

    def forbidden(*args, **kwargs):
        raise AssertionError("Local startup must not run Docker commands")

    monkeypatch.setattr(manage, "command", forbidden)
    manage.prepare_services()


def test_health_accepts_sqlite_and_checks_execution_mode(monkeypatch):
    monkeypatch.setattr(manage.settings, "database_url", "sqlite:///platform.db")
    monkeypatch.setattr(manage.settings, "execution_mode", "local")
    response = {"status": "ok", "database": "sqlite", "execution": "local"}
    original_client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    monkeypatch.setattr(
        manage.httpx, "Client", lambda **kwargs: original_client(transport=transport)
    )
    assert manage.healthy()
    response["execution"] = "docker"
    assert not manage.healthy()


def test_docker_setup_builds_missing_runner(monkeypatch):
    monkeypatch.setattr(manage.settings, "execution_mode", "docker")
    monkeypatch.setattr(manage.settings, "database_url", "postgresql://local/database")
    calls = []

    def execute(args, timeout):
        calls.append(args)
        if args[1:3] == ["image", "inspect"]:
            raise RuntimeError("Missing image")

    monkeypatch.setattr(manage, "command", execute)
    manage.prepare_services()
    assert any(args[1] == "build" for args in calls)
    assert calls[-1] == ["docker", "compose", "up", "-d", "--wait", "database"]


def test_stop_reports_other_installation_without_killing_it(monkeypatch, tmp_path, capsys):
    class OtherServer:
        pid = 42

        def cwd(self):
            return str(tmp_path)

        def terminate(self):
            raise AssertionError("An unrelated server must not be terminated")

    monkeypatch.setattr(manage, "listeners", lambda: [OtherServer()])
    monkeypatch.setattr(manage, "owned_server", lambda process: False)
    with pytest.raises(RuntimeError, match="Nothing was stopped.*another installation"):
        manage.stop()
    assert "ForgeFlow stopped" not in capsys.readouterr().out


def test_stop_does_not_claim_success_when_port_remains_open(monkeypatch, capsys):
    class Server:
        pid = 42

        def parent(self):
            return None

        def children(self, recursive):
            return []

        def terminate(self):
            pass

    monkeypatch.setattr(manage, "listeners", lambda: [Server()])
    monkeypatch.setattr(manage, "owned_server", lambda process: True)
    monkeypatch.setattr(manage.psutil, "wait_procs", lambda *args, **kwargs: ([], []))
    monkeypatch.setattr(manage.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="still listening"):
        manage.stop()
    assert "ForgeFlow stopped" not in capsys.readouterr().out


def test_stop_reports_already_stopped(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(manage, "listeners", lambda: [])
    monkeypatch.setattr(manage, "RUNTIME", tmp_path)
    manage.stop()
    assert "already stopped" in capsys.readouterr().out
