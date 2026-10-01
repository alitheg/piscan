import hashlib
import random
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from piscan.store import Store

T0 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=UTC)
ALL = ["inbox", "sending", "sent", "failed"]


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


def make_scan(store: Store, seed: int, size=(900, 600)) -> tuple[Path, str]:
    tmp = store.new_tmp_path()
    Image.new("RGB", size, (seed % 256, (seed * 7) % 256, 90)).save(tmp, "JPEG")
    return tmp, hashlib.sha256(f"scan-{seed}".encode()).hexdigest()


def add(store: Store, seed: int):
    tmp, sha = make_scan(store, seed)
    return store.add_page(tmp, sha, T0 + timedelta(minutes=seed))


def all_pages(store: Store):
    return [p for d in store.list_drafts(ALL) for p in d.pages]


def test_creates_db_and_pages_dir(tmp_path):
    Store(tmp_path / "d")
    assert (tmp_path / "d" / "piscan.db").exists()
    assert (tmp_path / "d" / "pages").is_dir()


def test_new_tmp_path_is_unique_and_inside_pages(store):
    a, b = store.new_tmp_path(), store.new_tmp_path()
    assert a != b
    assert a.parent == store.data_dir / "pages"


def test_add_page_renames_thumbnails_and_creates_draft(store):
    tmp, sha = make_scan(store, 1)
    page = store.add_page(tmp, sha, T0)
    assert page is not None
    assert not tmp.exists()
    assert page.path == store.data_dir / "pages" / f"{sha}.jpg"
    assert page.path.exists()
    assert page.thumb_path == store.data_dir / "pages" / f"{sha}.thumb.jpg"
    with Image.open(page.thumb_path) as t:
        assert max(t.size) == 300
    assert page.rotation == 0 and page.position == 0
    assert store.has_sha(sha)
    d = store.get_draft(page.draft_id)
    assert d.status == "inbox" and d.created_at == T0
    assert [p.id for p in d.pages] == [page.id]


def test_add_page_duplicate_sha_deletes_tmp_and_returns_none(store):
    tmp, sha = make_scan(store, 1)
    store.add_page(tmp, sha, T0)
    tmp2, _ = make_scan(store, 1)
    assert store.add_page(tmp2, sha, T0) is None
    assert not tmp2.exists()
    assert len(store.list_drafts(["inbox"])) == 1


def test_add_page_over_orphan_file(store):
    tmp, sha = make_scan(store, 1)
    orphan = store.data_dir / "pages" / f"{sha}.jpg"
    orphan.write_bytes(b"stale")
    assert not store.has_sha(sha)
    page = store.add_page(tmp, sha, T0)
    assert page is not None
    with Image.open(page.path) as img:
        assert img.size == (900, 600)


def test_unknown_ids_raise_keyerror(store):
    for call in (
        lambda: store.get_draft(99),
        lambda: store.split(99),
        lambda: store.move_page(99, 1),
        lambda: store.rotate(99),
        lambda: store.delete_page(99),
        lambda: store.delete_draft(99),
        lambda: store.set_sending(99),
        lambda: store.set_task(99, "t"),
        lambda: store.mark_sent(99, 1, T0),
        lambda: store.mark_failed(99, "x"),
        lambda: store.reset_to_inbox(99),
        lambda: store.merge([98, 99]),
    ):
        with pytest.raises(KeyError):
            call()


def test_list_drafts_filters_and_orders_by_created_at(store):
    a = add(store, 5)
    b = add(store, 1)
    c = add(store, 3)
    store.mark_failed(c.draft_id, "boom")
    assert [d.id for d in store.list_drafts(["inbox"])] == [b.draft_id, a.draft_id]
    both = store.list_drafts(["inbox", "failed"])
    assert [d.id for d in both] == [b.draft_id, c.draft_id, a.draft_id]
    assert store.list_drafts([]) == []


def test_merge_moves_pages_into_earliest_draft_by_arrival(store):
    late = add(store, 5)
    early = add(store, 1)
    mid = add(store, 3)
    target = store.merge([late.draft_id, mid.draft_id, early.draft_id])
    assert target == early.draft_id
    d = store.get_draft(target)
    assert [p.id for p in d.pages] == [early.id, mid.id, late.id]
    assert [p.position for p in d.pages] == [0, 1, 2]
    assert len(store.list_drafts(["inbox"])) == 1


def test_merge_validation(store):
    a, b = add(store, 1), add(store, 2)
    with pytest.raises(ValueError):
        store.merge([a.draft_id])
    with pytest.raises(ValueError):
        store.merge([])
    with pytest.raises(ValueError):
        store.merge([a.draft_id, a.draft_id])
    store.set_sending(b.draft_id)
    with pytest.raises(ValueError):
        store.merge([a.draft_id, b.draft_id])
    assert len(all_pages(store)) == 2


def test_merge_failed_draft_returns_to_inbox(store):
    a, b = add(store, 1), add(store, 2)
    store.mark_failed(a.draft_id, "boom")
    d = store.get_draft(store.merge([a.draft_id, b.draft_id]))
    assert d.status == "inbox" and d.error is None


def test_split_moves_page_to_new_draft(store):
    a, b, c = add(store, 1), add(store, 2), add(store, 3)
    target = store.merge([a.draft_id, b.draft_id, c.draft_id])
    new = store.get_draft(store.split(b.id))
    assert [p.id for p in new.pages] == [b.id]
    assert new.created_at == b.arrived_at and new.status == "inbox"
    old = store.get_draft(target)
    assert [p.id for p in old.pages] == [a.id, c.id]
    assert [p.position for p in old.pages] == [0, 1]


def test_split_only_page_is_rejected(store):
    a = add(store, 1)
    with pytest.raises(ValueError):
        store.split(a.id)


def test_move_page_swaps_and_noops_at_ends(store):
    a, b, c = add(store, 1), add(store, 2), add(store, 3)
    t = store.merge([a.draft_id, b.draft_id, c.draft_id])

    def order():
        return [p.id for p in store.get_draft(t).pages]

    store.move_page(a.id, -1)
    assert order() == [a.id, b.id, c.id]
    store.move_page(a.id, 1)
    assert order() == [b.id, a.id, c.id]
    store.move_page(a.id, 1)
    assert order() == [b.id, c.id, a.id]
    store.move_page(a.id, 1)
    assert order() == [b.id, c.id, a.id]
    with pytest.raises(ValueError):
        store.move_page(a.id, 2)


def test_rotate_cycles(store):
    p = add(store, 1)
    seen = []
    for _ in range(4):
        store.rotate(p.id)
        seen.append(store.get_draft(p.draft_id).pages[0].rotation)
    assert seen == [90, 180, 270, 0]


def test_delete_page_removes_files_and_renumbers(store):
    a, b, c = add(store, 1), add(store, 2), add(store, 3)
    t = store.merge([a.draft_id, b.draft_id, c.draft_id])
    store.delete_page(a.id)
    assert not a.path.exists() and not a.thumb_path.exists()
    d = store.get_draft(t)
    assert [(p.id, p.position) for p in d.pages] == [(b.id, 0), (c.id, 1)]
    assert not store.has_sha(a.sha256)


def test_delete_last_page_deletes_draft(store):
    a = add(store, 1)
    store.delete_page(a.id)
    with pytest.raises(KeyError):
        store.get_draft(a.draft_id)
    assert not a.path.exists()


def test_delete_draft_removes_pages_and_files(store):
    a, b = add(store, 1), add(store, 2)
    t = store.merge([a.draft_id, b.draft_id])
    store.delete_draft(t)
    assert all_pages(store) == []
    for p in (a, b):
        assert not p.path.exists() and not p.thumb_path.exists()


def test_send_lifecycle_touches_only_status_fields(store):
    a, b = add(store, 1), add(store, 2)
    t = store.merge([a.draft_id, b.draft_id])
    store.rotate(a.id)
    store.set_sending(t)
    store.set_task(t, "task-1")
    d = store.get_draft(t)
    assert d.status == "sending" and d.paperless_task_id == "task-1"
    assert [p.id for p in d.pages] == [a.id, b.id]
    assert d.pages[0].rotation == 90

    store.mark_failed(t, "paperless said no")
    d = store.get_draft(t)
    assert d.status == "failed" and d.error == "paperless said no"
    assert len(d.pages) == 2 and a.path.exists()

    store.reset_to_inbox(t)
    d = store.get_draft(t)
    assert d.status == "inbox" and d.error is None and d.paperless_task_id is None
    assert [p.id for p in d.pages] == [a.id, b.id]
    assert d.pages[0].rotation == 90


def test_mark_sent_deletes_files_keeps_draft_and_rows(store):
    a, b = add(store, 1), add(store, 2)
    t = store.merge([a.draft_id, b.draft_id])
    now = T0 + timedelta(hours=1)
    store.mark_sent(t, 42, now)
    d = store.get_draft(t)
    assert d.status == "sent" and d.paperless_document_id == 42 and d.sent_at == now
    assert len(d.pages) == 2
    for p in (a, b):
        assert not p.path.exists() and not p.thumb_path.exists()
    assert store.has_sha(a.sha256)


def test_mark_sent_without_document_id(store):
    a = add(store, 1)
    store.mark_sent(a.draft_id, None, T0)
    assert store.get_draft(a.draft_id).paperless_document_id is None


def test_purge_sent(store):
    old, new, inbox = add(store, 1), add(store, 2), add(store, 3)
    now = T0 + timedelta(days=2)
    store.mark_sent(old.draft_id, 1, now - timedelta(hours=25))
    store.mark_sent(new.draft_id, 2, now - timedelta(hours=1))
    store.purge_sent(timedelta(hours=24), now)
    ids = {d.id for d in store.list_drafts(["inbox", "sent"])}
    assert ids == {new.draft_id, inbox.draft_id}
    assert not store.has_sha(old.sha256)


def test_free_bytes(store):
    assert store.free_bytes() > 0


def test_reopen_keeps_data(tmp_path):
    s = Store(tmp_path / "d")
    p = add(s, 1)
    s2 = Store(tmp_path / "d")
    assert s2.get_draft(p.draft_id).pages[0].sha256 == p.sha256


def test_concurrent_add_page(store):
    errors = []

    def worker(seed):
        try:
            add(store, seed)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(all_pages(store)) == 16


@pytest.mark.parametrize("seed", range(5))
def test_random_operations_never_lose_or_duplicate_pages(store, seed):
    rng = random.Random(seed)
    expected: set[int] = set()
    for i in range(12):
        expected.add(add(store, i).id)

    for _ in range(150):
        drafts = store.list_drafts(["inbox"])
        pages = [p for d in drafts for p in d.pages]
        op = rng.choice(
            ["merge", "split", "move", "delete_page", "delete_draft", "rotate"]
        )
        if op == "merge" and len(drafts) >= 2:
            store.merge(
                [d.id for d in rng.sample(drafts, rng.randint(2, min(4, len(drafts))))]
            )
        elif op == "split" and pages:
            p = rng.choice(pages)
            if len(store.get_draft(p.draft_id).pages) > 1:
                store.split(p.id)
        elif op == "move" and pages:
            store.move_page(rng.choice(pages).id, rng.choice([-1, 1]))
        elif op == "delete_page" and len(pages) > 3:
            p = rng.choice(pages)
            store.delete_page(p.id)
            expected.discard(p.id)
        elif op == "delete_draft" and len(drafts) > 2:
            d = rng.choice(drafts)
            store.delete_draft(d.id)
            expected -= {p.id for p in d.pages}
        elif op == "rotate" and pages:
            store.rotate(rng.choice(pages).id)

        assert sorted(p.id for p in all_pages(store)) == sorted(expected)
        for d in store.list_drafts(["inbox"]):
            assert d.pages, "empty draft left behind"
            assert [p.position for p in d.pages] == list(range(len(d.pages)))
            assert all(p.path.exists() for p in d.pages)


@pytest.mark.parametrize("state", ["sending", "sent"])
def test_user_edits_refused_unless_inbox_or_failed(store, state):
    a, b = add(store, 1), add(store, 2)
    t = store.merge([a.draft_id, b.draft_id])
    if state == "sending":
        store.set_sending(t)
    else:
        store.mark_sent(t, 1, T0)
    for call in (
        lambda: store.split(a.id),
        lambda: store.move_page(a.id, 1),
        lambda: store.rotate(a.id),
        lambda: store.delete_page(a.id),
        lambda: store.delete_draft(t),
    ):
        with pytest.raises(ValueError):
            call()
    d = store.get_draft(t)
    assert [p.id for p in d.pages] == [a.id, b.id]
    assert d.pages[0].rotation == 0


def test_failed_draft_is_editable(store):
    a, b = add(store, 1), add(store, 2)
    t = store.merge([a.draft_id, b.draft_id])
    store.mark_failed(t, "x")
    store.rotate(a.id)
    store.split(b.id)


def test_naive_datetimes_rejected(store):
    naive = T0.replace(tzinfo=None)
    tmp, sha = make_scan(store, 1)
    with pytest.raises(ValueError):
        store.add_page(tmp, sha, naive)
    assert not store.has_sha(sha)
    p = add(store, 2)
    with pytest.raises(ValueError):
        store.mark_sent(p.draft_id, 1, naive)
    with pytest.raises(ValueError):
        store.purge_sent(timedelta(hours=1), naive)


def test_non_utc_datetimes_normalised(store):
    plus2 = timezone(timedelta(hours=2))
    # 10:00+02:00 is 08:00 UTC, earlier than 09:00 UTC despite the larger clock reading.
    tmp1, sha1 = make_scan(store, 1)
    tmp2, sha2 = make_scan(store, 2)
    later = store.add_page(tmp1, sha1, T0)
    earlier = store.add_page(tmp2, sha2, datetime(2026, 10, 1, 10, 0, tzinfo=plus2))
    assert [d.id for d in store.list_drafts(["inbox"])] == [
        earlier.draft_id,
        later.draft_id,
    ]
    assert earlier.arrived_at == datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def test_set_sending_clears_old_task_id(store):
    t = add(store, 1).draft_id
    store.set_sending(t)
    store.set_task(t, "old")
    store.mark_failed(t, "nope")
    store.set_sending(t)
    d = store.get_draft(t)
    assert d.status == "sending" and d.paperless_task_id is None and d.error is None
