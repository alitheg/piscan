import argparse
import logging
import signal
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI

from piscan.config import Config, ConfigError, load_config
from piscan.ingest import DeviceProbe, Ingest, Mounter
from piscan.paperless import PaperlessClient, PaperlessError
from piscan.sender import Sender
from piscan.store import Store
from piscan.web.app import create_app

DEFAULT_CONFIG = Path("/etc/piscan.toml")
HEALTH_INTERVAL = 60.0
PURGE_INTERVAL = 3600.0
PURGE_AGE = timedelta(hours=24)
JOIN_TIMEOUT = 5.0

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="piscan")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("serve", "run the web UI and ingest loop"),
        ("check", "check config and connectivity"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser


@dataclass
class Components:
    store: Store
    ingest: Ingest
    sender: Sender
    app: FastAPI


def build_components(config: Config) -> Components:
    store = Store(config.data_dir)
    ingest = Ingest(DeviceProbe(config.scanner_device), Mounter(config.mountpoint), store)
    client = PaperlessClient(config.paperless_url, config.paperless_token)
    sender = Sender(store, client)
    sender.recover()
    app = create_app(store, sender, ingest.status, sender.health, config.paperless_url)
    return Components(store, ingest, sender, app)


def _now() -> datetime:
    return datetime.now(UTC)


def housekeeping(
    components: Components,
    stop: threading.Event,
    health_interval: float = HEALTH_INTERVAL,
    purge_interval: float = PURGE_INTERVAL,
    now: Callable[[], datetime] = _now,
) -> None:
    """Health check on every pass, purge on its own slower schedule."""
    next_purge = 0.0
    elapsed = 0.0
    while not stop.is_set():
        try:
            components.sender.check_health()
        except Exception:
            log.exception("health check failed")
        if elapsed >= next_purge:
            try:
                components.store.purge_sent(PURGE_AGE, now())
            except Exception:
                log.exception("purging sent drafts failed")
            next_purge = elapsed + purge_interval
        if stop.wait(health_interval):
            break
        elapsed += health_interval


def start_workers(
    components: Components, stop: threading.Event, **housekeeping_kwargs
) -> list[threading.Thread]:
    targets = [
        ("ingest", lambda: components.ingest.run(stop)),
        ("sender", lambda: components.sender.run(stop)),
        ("housekeeping", lambda: housekeeping(components, stop, **housekeeping_kwargs)),
    ]
    threads = [threading.Thread(target=t, name=n, daemon=True) for n, t in targets]
    for t in threads:
        t.start()
    return threads


def stop_workers(
    stop: threading.Event, threads: list[threading.Thread], timeout: float = JOIN_TIMEOUT
) -> None:
    stop.set()
    for t in threads:
        t.join(timeout)
        if t.is_alive():
            log.warning("thread %s did not stop within %.0fs", t.name, timeout)


def serve(config: Config) -> int:
    import uvicorn

    components = build_components(config)
    stop = threading.Event()
    threads = start_workers(components, stop)
    server = uvicorn.Server(
        uvicorn.Config(
            components.app,
            host="0.0.0.0",
            port=config.web_port,
            log_level="info",
            # The 2 s status polling would flood the journal.
            access_log=False,
            # Its default dictConfig would replace our logging setup and
            # put the access log back to INFO.
            log_config=None,
        )
    )
    # Recent uvicorn re-raises the signal after a graceful shutdown, restoring
    # whatever handler was there before. With the default handler that kills
    # the process inside server.run(), skipping the thread cleanup below.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: None)
    try:
        server.run()  # handles SIGINT/SIGTERM itself and returns
    finally:
        stop_workers(stop, threads)
    return 0


def check(config: Config) -> int:
    client = PaperlessClient(config.paperless_url, config.paperless_token)
    try:
        client.verify()
    except PaperlessError as e:
        print(f"FAILED: {e}")
        return 1
    print(f"OK: Paperless at {config.paperless_url} accepted the token")
    return 0


def setup_logging() -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"piscan: {e}", file=sys.stderr)
        return 1
    setup_logging()
    if args.command == "serve":
        return serve(config)
    return check(config)
