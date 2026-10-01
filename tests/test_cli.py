import logging
import threading
from datetime import datetime, timedelta

import pytest

from piscan import cli
from piscan.cli import main
from piscan.config import Config
from piscan.paperless import PaperlessError


def write_cfg(tmp_path, extra=""):
    cfg = tmp_path / "piscan.toml"
    cfg.write_text(f'[paperless]\nurl = "http://p"\ntoken = "t"\n{extra}')
    return cfg


def make_config(tmp_path) -> Config:
    return Config(
        paperless_url="http://p",
        paperless_token="t",
        scanner_device=tmp_path / "no-such-device",
        mountpoint=tmp_path / "mnt",
        data_dir=tmp_path / "data",
    )


def test_bad_config_exits_nonzero(tmp_path, capsys):
    cfg = tmp_path / "piscan.toml"
    cfg.write_text("[paperless]\n")
    assert main(["check", "--config", str(cfg)]) == 1
    assert "url" in capsys.readouterr().err


def test_serve_passes_config_to_serve(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "serve", lambda config: seen.append(config) or 0)
    assert main(["serve", "--config", str(write_cfg(tmp_path))]) == 0
    assert seen[0].paperless_url == "http://p"


def test_command_is_required():
    with pytest.raises(SystemExit):
        main([])


class FakeClient:
    def __init__(self, error=None):
        self.error = error

    def verify(self):
        if self.error:
            raise PaperlessError(self.error)


def test_check_ok(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "PaperlessClient", lambda url, token: FakeClient())
    assert main(["check", "--config", str(write_cfg(tmp_path))]) == 0
    assert capsys.readouterr().out == "OK: Paperless at http://p accepted the token\n"


def test_check_failure_uses_paperless_message(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "PaperlessClient", lambda url, token: FakeClient("Paperless rejected the token")
    )
    assert main(["check", "--config", str(write_cfg(tmp_path))]) == 1
    assert capsys.readouterr().out == "FAILED: Paperless rejected the token\n"


def test_wiring_builds_app_and_threads_start_and_stop(tmp_path):
    from fastapi.testclient import TestClient

    components = cli.build_components(make_config(tmp_path))
    stop = threading.Event()
    threads = cli.start_workers(components, stop, health_interval=0.05)
    try:
        assert {t.name for t in threads} == {"ingest", "sender", "housekeeping"}
        assert all(t.is_alive() for t in threads)
        body = TestClient(components.app, base_url="http://localhost").get("/fragments/status").text
        assert "Scanner off / unplugged" in body
    finally:
        cli.stop_workers(stop, threads, timeout=5)
    assert not any(t.is_alive() for t in threads)


def test_housekeeping_survives_exceptions_and_purges_with_aware_utc():
    calls = {"health": 0, "purge": []}
    stop = threading.Event()

    class FakeSender:
        def check_health(self):
            calls["health"] += 1
            if calls["health"] == 1:
                raise RuntimeError("boom")
            if calls["health"] >= 3:
                stop.set()

    class FakeStore:
        def purge_sent(self, older_than, now):
            calls["purge"].append((older_than, now))
            raise RuntimeError("boom")

    components = cli.Components(FakeStore(), None, FakeSender(), None)
    cli.housekeeping(components, stop, health_interval=0.01)
    assert calls["health"] == 3
    older_than, now = calls["purge"][0]
    assert older_than == timedelta(hours=24)
    assert isinstance(now, datetime) and now.utcoffset() == timedelta(0)
    assert len(calls["purge"]) == 1  # hourly, not every pass


def test_logging_quietens_uvicorn_access():
    cli.setup_logging()
    assert logging.getLogger("uvicorn.access").level == logging.WARNING
