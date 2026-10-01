import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from piscan.paperless import PaperlessError, TaskResult
from piscan.sender import Sender
from piscan.store import Store

T0 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=UTC)


class FakeClient:
    def __init__(self):
        self.uploads = []
        self.upload_error = None
        self.upload_id = "task-1"
        self.results = []  # consumed in order; the last one repeats
        self.polls = 0
        self.ping_result = True

    def upload(self, pdf, filename):
        if self.upload_error:
            raise PaperlessError(self.upload_error)
        self.uploads.append((pdf, filename))
        return self.upload_id

    def task(self, task_id):
        self.polls += 1
        r = self.results[0] if len(self.results) == 1 else self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def ping(self):
        return self.ping_result


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)
        return False


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def sender(store, client, clock):
    return Sender(store, client, clock=clock, sleep=clock.sleep)


def make_draft(store, n=2) -> int:
    ids = []
    for i in range(n):
        tmp = store.new_tmp_path()
        Image.new("RGB", (80, 60), (i * 40, 90, 90)).save(tmp, "JPEG")
        ids.append(store.add_page(tmp, f"sha-{i}-{id(tmp)}", T0).draft_id)
    return store.merge(ids) if n > 1 else ids[0]


def pages_exist(store, d):
    pages = store.get_draft(d).pages
    return bool(pages) and all(Path(p.path).exists() for p in pages)


def test_success(store, client, sender):
    d = make_draft(store)
    client.results = [TaskResult("pending"), TaskResult("success", document_id=7)]
    sender.enqueue(d)
    assert store.get_draft(d).status == "sending"
    assert sender.process_one() is True
    got = store.get_draft(d)
    assert got.status == "sent" and got.paperless_document_id == 7
    assert got.paperless_task_id == "task-1"
    assert client.polls == 2
    pdf, name = client.uploads[0]
    assert pdf.startswith(b"%PDF")
    assert name == f"piscan-{got.created_at.astimezone(UTC):%Y%m%d-%H%M%S}-{d}.pdf"
    assert not any(Path(p.path).exists() for p in got.pages)


def test_process_one_idle(sender):
    assert sender.process_one() is False
    assert sender.process_one(timeout=0.01) is False


def test_failure_keeps_pages_and_retry_resends(store, client, sender):
    d = make_draft(store)
    client.results = [TaskResult("failure", error="duplicate")]
    sender.enqueue(d)
    sender.process_one()
    got = store.get_draft(d)
    assert (got.status, got.error) == ("failed", "duplicate")
    assert pages_exist(store, d)

    client.upload_id = "task-2"
    client.results = [TaskResult("success", document_id=9)]
    sender.retry(d)
    sender.process_one()
    got = store.get_draft(d)
    assert got.status == "sent" and got.paperless_task_id == "task-2"
    assert len(client.uploads) == 2


def test_upload_error_marks_failed(store, client, sender):
    d = make_draft(store)
    client.upload_error = "can't reach Paperless"
    sender.enqueue(d)
    sender.process_one()
    got = store.get_draft(d)
    assert (got.status, got.error) == ("failed", "can't reach Paperless")
    assert got.paperless_task_id is None
    assert pages_exist(store, d)


def test_timeout(store, client, sender):
    d = make_draft(store)
    client.results = [TaskResult("pending")]
    sender.enqueue(d)
    sender.process_one()
    got = store.get_draft(d)
    assert (got.status, got.error) == ("failed", "unknown - check Paperless")
    assert pages_exist(store, d)


def test_poll_connection_errors_are_retried(store, client, sender):
    d = make_draft(store)
    blip = PaperlessError("can't reach Paperless")
    client.results = [blip, blip, TaskResult("success", document_id=3)]
    sender.enqueue(d)
    sender.process_one()
    assert store.get_draft(d).status == "sent"


def test_poll_connection_errors_until_timeout(store, client, sender):
    d = make_draft(store)
    client.results = [PaperlessError("can't reach Paperless")]
    sender.enqueue(d)
    sender.process_one()
    got = store.get_draft(d)
    assert got.status == "failed"
    assert got.error == "unknown - check Paperless [can't reach Paperless]"
    assert pages_exist(store, d)


def test_retry_then_restart_does_not_poll_old_task(store, client, clock, sender):
    d = make_draft(store)
    client.results = [TaskResult("failure", error="duplicate")]
    sender.enqueue(d)
    sender.process_one()
    sender.retry(d)
    polls = client.polls

    fresh = Sender(store, client, clock=clock, sleep=clock.sleep)
    fresh.recover()
    assert store.get_draft(d).status == "inbox"
    assert fresh.process_one() is False
    assert client.polls == polls


def test_run_processes_a_draft(store, client):
    d = make_draft(store)
    client.results = [TaskResult("success", document_id=4)]
    s = Sender(store, client)
    stop = threading.Event()
    t = threading.Thread(target=s.run, args=(stop,))
    s.enqueue(d)
    t.start()
    try:
        for _ in range(200):
            if store.get_draft(d).status == "sent":
                break
            time.sleep(0.05)
    finally:
        stop.set()
        t.join(timeout=5)
    assert not t.is_alive()
    assert store.get_draft(d).status == "sent"


def test_enqueue_and_retry_state_checks(store, sender):
    d = make_draft(store)
    with pytest.raises(ValueError):
        sender.retry(d)
    sender.enqueue(d)
    with pytest.raises(ValueError):
        sender.enqueue(d)


def test_stop_while_polling_leaves_draft_sending(store, client, clock):
    s = Sender(store, client, clock=clock, sleep=lambda secs: True)
    d = make_draft(store)
    client.results = [TaskResult("pending")]
    s.enqueue(d)
    s.process_one()
    got = store.get_draft(d)
    assert got.status == "sending" and got.paperless_task_id == "task-1"


def test_recover_with_task_id_resumes_without_upload(store, client, sender):
    d = make_draft(store)
    store.set_sending(d)
    store.set_task(d, "old-task")
    client.results = [TaskResult("success", document_id=5)]
    sender.recover()
    assert sender.process_one() is True
    assert store.get_draft(d).status == "sent"
    assert client.uploads == []


def test_recover_without_task_id_goes_to_inbox(store, sender):
    d = make_draft(store)
    store.set_sending(d)
    sender.recover()
    assert store.get_draft(d).status == "inbox"
    assert sender.process_one() is False


def test_unexpected_error_marks_failed(store, sender):
    d = make_draft(store)
    sender.enqueue(d)
    for p in store.get_draft(d).pages:
        Path(p.path).unlink()
    sender.process_one()
    got = store.get_draft(d)
    assert got.status == "failed" and got.error


def test_health(client, sender):
    assert sender.health() is None
    sender.check_health()
    assert sender.health() is True
    client.ping_result = False
    sender.check_health()
    assert sender.health() is False


def test_run_returns_when_stopped(store, client):
    stop = threading.Event()
    stop.set()
    Sender(store, client).run(stop)
