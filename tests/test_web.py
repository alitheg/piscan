import hashlib
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from piscan.ingest import ScannerState, ScannerStatus
from piscan.store import Store
from piscan.web.app import create_app

T0 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=UTC)
HX = {"HX-Request": "true"}


class FakeSender:
    def __init__(self, store):
        self.store = store
        self.enqueued = []
        self.retried = []

    def enqueue(self, draft_id):
        d = self.store.get_draft(draft_id)  # KeyError for unknown ids
        if d.status != "inbox":
            raise ValueError(f"draft {draft_id} is {d.status}, cannot send")
        self.enqueued.append(draft_id)
        self.store.set_sending(draft_id)

    def retry(self, draft_id):
        d = self.store.get_draft(draft_id)
        if d.status != "failed":
            raise ValueError(f"draft {draft_id} is {d.status}, cannot retry")
        self.retried.append(draft_id)
        self.store.set_sending(draft_id)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def state():
    return {
        "scanner": ScannerStatus(ScannerState.READY, ""),
        "paperless": True,
    }


@pytest.fixture
def sender(store):
    return FakeSender(store)


@pytest.fixture
def client(store, sender, state):
    app = create_app(
        store,
        sender,
        lambda: state["scanner"],
        lambda: state["paperless"],
        "http://paperless.lan:8000/",
        ["testserver"],
    )
    return TestClient(app, follow_redirects=False)


def add(store, seed, when=None):
    tmp = store.new_tmp_path()
    Image.new("RGB", (900, 600), (seed * 20 % 256, 80, 90)).save(tmp, "JPEG")
    sha = hashlib.sha256(f"scan-{seed}".encode()).hexdigest()
    return store.add_page(tmp, sha, when or T0 + timedelta(minutes=seed))


def ids(store, statuses=("inbox",)):
    return [d.id for d in store.list_drafts(statuses)]


def test_index_renders_cards_and_assets(client, store):
    p = add(store, 1)
    r = client.get("/")
    assert r.status_code == 200
    assert f"/pages/{p.id}/thumb" in r.text
    assert "/static/htmx.min.js" in r.text
    assert "1 draft" in r.text
    assert "Recently sent" in r.text
    assert client.get("/static/htmx.min.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_empty_index(client):
    assert "No drafts" in client.get("/").text


def test_thumbs_rotated_with_css(client, store):
    p = add(store, 1)
    store.rotate(p.id)
    assert "rotate(90deg)" in client.get("/fragments/drafts").text


@pytest.mark.parametrize(
    "status,text",
    [
        (ScannerStatus(ScannerState.OFF, ""), "Scanner off / unplugged"),
        (ScannerStatus(ScannerState.SCANNING, ""), "Scanning..."),
        (ScannerStatus(ScannerState.IMPORTING, "", 2, 5), "Importing... 2 of 5"),
        (ScannerStatus(ScannerState.IMPORTING, "", 0, 0), "Importing..."),
        (ScannerStatus(ScannerState.READY, ""), "Ready"),
        (ScannerStatus(ScannerState.PROBLEM, "won't mount"), "Problem: won&#39;t mount"),
    ],
)
def test_status_text(client, state, status, text):
    state["scanner"] = status
    assert text in client.get("/fragments/status").text


@pytest.mark.parametrize(
    "health,cls", [(True, "dot-ok"), (False, "dot-bad"), (None, "dot-grey")]
)
def test_paperless_dot(client, state, health, cls):
    state["paperless"] = health
    html = client.get("/fragments/status").text
    assert re.search(rf"dot {cls}\"></span>\s*Paperless", html)


def test_status_counts_drafts(client, store):
    add(store, 1)
    add(store, 2)
    assert "2 drafts" in client.get("/fragments/status").text


def test_merge_htmx_returns_fragment(client, store):
    add(store, 1)
    add(store, 2)
    a, b = ids(store)
    r = client.post("/merge", data={"draft_id": [a, b]}, headers=HX)
    assert r.status_code == 200
    assert "<html" not in r.text
    assert len(ids(store)) == 1
    assert "2 pages" in r.text


def test_merge_plain_redirects(client, store):
    add(store, 1)
    add(store, 2)
    r = client.post("/merge", data={"draft_id": ids(store)})
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_merge_single_is_409_with_message(client, store):
    add(store, 1)
    r = client.post("/merge", data={"draft_id": ids(store)}, headers=HX)
    assert r.status_code == 409
    assert "at least two" in r.text
    assert len(ids(store)) == 1


def test_merge_nothing_ticked_is_409(client):
    assert client.post("/merge", headers=HX).status_code == 409


def test_merge_unknown_404(client, store):
    add(store, 1)
    r = client.post("/merge", data={"draft_id": [ids(store)[0], 999]}, headers=HX)
    assert r.status_code == 404


def test_delete_many(client, store):
    add(store, 1)
    add(store, 2)
    add(store, 3)
    a, b, c = ids(store)
    r = client.post("/delete", data={"draft_id": [a, c]}, headers=HX)
    assert r.status_code == 200
    assert ids(store) == [b]
    assert "hx-confirm" in r.text


def test_delete_unknown_404(client):
    assert client.post("/delete", data={"draft_id": [999]}).status_code == 404


def test_send_and_errors(client, store, sender):
    add(store, 1)
    (a,) = ids(store)
    r = client.post(f"/drafts/{a}/send", headers=HX)
    assert r.status_code == 200 and sender.enqueued == [a]
    assert "sending" in r.text
    again = client.post(f"/drafts/{a}/send", headers=HX)
    assert again.status_code == 409 and "cannot send" in again.text
    assert client.post("/drafts/999/send").status_code == 404


def test_send_all_in_screen_order_skips_bad(client, store, sender):
    for i in (3, 1, 2):
        add(store, i)
    order = ids(store)
    store.mark_failed(order[1], "boom")
    r = client.post("/send-all", headers=HX)
    assert r.status_code == 200
    assert sender.enqueued == [order[0], order[2]]


def test_retry_failed_shows_error_and_button(client, store, sender):
    add(store, 1)
    (a,) = ids(store)
    store.mark_failed(a, "duplicate document")
    html = client.get("/fragments/drafts").text
    assert "duplicate document" in html and "Retry" in html
    assert client.post(f"/drafts/{a}/retry", headers=HX).status_code == 200
    assert sender.retried == [a]
    assert client.post(f"/drafts/{a}/retry", headers=HX).status_code == 409


def test_draft_view_and_404(client, store):
    add(store, 1)
    (a,) = ids(store)
    full = client.get(f"/drafts/{a}")
    assert "<html" in full.text and "/image" in full.text
    frag = client.get(f"/drafts/{a}", headers=HX)
    assert "<html" not in frag.text
    assert client.get("/drafts/999").status_code == 404


def _two_page_draft(store):
    add(store, 1)
    add(store, 2)
    a, b = ids(store)
    store.merge([a, b])
    (d,) = store.list_drafts(["inbox"])
    return d


def test_rotate_page(client, store):
    d = _two_page_draft(store)
    p = d.pages[0]
    r = client.post(f"/pages/{p.id}/rotate", headers=HX)
    assert r.status_code == 200 and "rotate(90deg)" in r.text
    assert store.get_draft(d.id).pages[0].rotation == 90
    assert client.post(f"/pages/{p.id}/rotate").headers["location"] == f"/drafts/{d.id}"


def test_move_page_up_down(client, store):
    d = _two_page_draft(store)
    first, second = d.pages
    client.post(f"/pages/{first.id}/down", headers=HX)
    assert [p.id for p in store.get_draft(d.id).pages] == [second.id, first.id]
    client.post(f"/pages/{first.id}/up", headers=HX)
    assert [p.id for p in store.get_draft(d.id).pages] == [first.id, second.id]


def test_split_page(client, store):
    d = _two_page_draft(store)
    r = client.post(f"/pages/{d.pages[1].id}/split", headers=HX)
    assert r.status_code == 200
    assert len(ids(store)) == 2


def test_split_only_page_is_409_with_notice(client, store):
    add(store, 1)
    p = store.list_drafts(["inbox"])[0].pages[0]
    r = client.post(f"/pages/{p.id}/split", headers=HX)
    assert r.status_code == 409 and "only page" in r.text


def test_delete_page_and_last_page_redirects(client, store):
    d = _two_page_draft(store)
    client.post(f"/pages/{d.pages[0].id}/delete", headers=HX)
    assert len(store.get_draft(d.id).pages) == 1
    r = client.post(f"/pages/{d.pages[1].id}/delete", headers=HX)
    assert r.headers["HX-Redirect"] == "/"
    assert ids(store) == []


def test_edit_of_sending_draft_is_409(client, store, sender):
    add(store, 1)
    (a,) = ids(store)
    p = store.get_draft(a).pages[0]
    sender.enqueue(a)
    r = client.post(f"/pages/{p.id}/rotate", headers=HX)
    assert r.status_code == 409 and "cannot edit" in r.text


@pytest.mark.parametrize("action", ["rotate", "up", "down", "split", "delete"])
def test_page_actions_unknown_404(client, action):
    assert client.post(f"/pages/999/{action}").status_code == 404


def test_images(client, store):
    p = add(store, 1)
    t = client.get(f"/pages/{p.id}/thumb")
    assert t.status_code == 200 and t.headers["content-type"] == "image/jpeg"
    assert client.get(f"/pages/{p.id}/image").content == p.path.read_bytes()
    p.path.unlink()
    assert client.get(f"/pages/{p.id}/image").status_code == 404
    assert client.get("/pages/999/thumb").status_code == 404


def test_recently_sent_links_to_paperless(client, store):
    add(store, 1)
    add(store, 2)
    old, new = ids(store)
    now = datetime.now(UTC)
    store.mark_sent(new, 42, now - timedelta(hours=1))
    store.mark_sent(old, 7, now - timedelta(hours=30))
    html = client.get("/fragments/sent").text
    assert "http://paperless.lan:8000/documents/42/details" in html
    assert "/documents/7/" not in html
    assert "/documents/42/details" in client.get("/").text


def test_polling_pauses_while_ticked(client):
    # Ticks live in the DOM, so the poll trigger must be conditional on them.
    assert ".pick:checked" in client.get("/").text


def test_importing_with_no_total_has_no_counts(client, state):
    state["scanner"] = ScannerStatus(ScannerState.IMPORTING, "", 0, 0)
    assert "0 of 0" not in client.get("/fragments/status").text


def test_real_ingest_problem_renders_with_one_prefix(store, sender, tmp_path):
    from piscan.ingest import Ingest, MountError

    class Probe:
        def present(self):
            return True

        def size(self):
            return 1000

    class Mounter:
        mountpoint = tmp_path

        def mount(self):
            raise MountError("mount failed: wrong fs type")

    ing = Ingest(Probe(), Mounter(), store)
    ing.tick()
    app = create_app(store, sender, ing.status, lambda: True, "http://p/", ["testserver"])
    text = TestClient(app).get("/fragments/status").text
    assert "Problem: mount failed: wrong fs type" in text
    assert "Problem: Problem" not in text


# -- same-origin guard --------------------------------------------------------


def test_same_origin_post_works(client, store):
    add(store, 1)
    r = client.post("/send-all", headers={"Origin": "http://testserver"})
    assert r.status_code == 303


def test_cross_origin_post_is_refused(client, store):
    add(store, 1)
    r = client.post("/send-all", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert ids(store) != []  # nothing was sent


def test_cross_site_fetch_metadata_is_refused(client):
    r = client.post("/send-all", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_same_origin_fetch_metadata_works(client):
    r = client.post("/send-all", headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 303


def test_post_without_origin_works(client):
    assert client.post("/send-all").status_code == 303


def test_get_ignores_origin(client):
    assert client.get("/", headers={"Origin": "http://evil.example"}).status_code == 200


# -- Host allowlist (DNS rebinding) --------------------------------------------


def test_rebound_hostname_is_refused_even_same_origin(client, store):
    add(store, 1)
    h = {"Host": "evil.example:8080", "Origin": "http://evil.example:8080"}
    assert client.post("/send-all", headers=h).status_code == 400
    assert ids(store) != []
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


@pytest.mark.parametrize(
    "host", ["192.168.1.20:8080", "[fe80::1]:8080", "localhost:8080", "TestServer"]
)
def test_ip_literals_and_known_names_pass(client, host):
    assert client.get("/", headers={"Host": host}).status_code == 200


def test_pi_hostname_dot_local_passes(client, monkeypatch):
    import socket

    from piscan.web.app import default_hosts

    monkeypatch.setattr(socket, "gethostname", lambda: "PiScan")
    assert {"piscan", "piscan.local", "localhost"} <= default_hosts()
