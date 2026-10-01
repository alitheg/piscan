import httpx
import pytest

from piscan.paperless import PaperlessClient, PaperlessError


def client(handler):
    return PaperlessClient(
        "http://paperless.test/", "tok", transport=httpx.MockTransport(handler)
    )


def test_upload_posts_multipart_with_token():
    seen = {}

    def handler(req):
        seen["req"] = req
        return httpx.Response(200, json="abc-123")

    assert client(handler).upload(b"%PDF-x", "f.pdf") == "abc-123"
    req = seen["req"]
    assert req.method == "POST"
    assert req.url.path == "/api/documents/post_document/"
    assert req.headers["Authorization"] == "Token tok"
    assert b'name="document"; filename="f.pdf"' in req.content
    assert b"%PDF-x" in req.content


def task_client(items):
    seen = {}

    def handler(req):
        seen["q"] = dict(req.url.params)
        return httpx.Response(200, json=items)

    c = client(handler)
    c.seen = seen
    return c


def test_task_unknown_is_pending():
    c = task_client([])
    assert c.task("t1").state == "pending"
    assert c.seen["q"] == {"task_id": "t1"}


@pytest.mark.parametrize("status", ["PENDING", "STARTED", "RETRY"])
def test_task_in_progress_is_pending(status):
    assert task_client([{"status": status}]).task("t").state == "pending"


@pytest.mark.parametrize("doc", ["42", 42])
def test_task_success(doc):
    r = task_client([{"status": "SUCCESS", "related_document": doc}]).task("t")
    assert (r.state, r.document_id) == ("success", 42)


def test_task_success_without_document():
    r = task_client([{"status": "SUCCESS", "related_document": None}]).task("t")
    assert (r.state, r.document_id) == ("success", None)


def test_task_failure_carries_reason():
    r = task_client([{"status": "FAILURE", "result": "It is a duplicate."}]).task("t")
    assert (r.state, r.error) == ("failure", "It is a duplicate.")


@pytest.mark.parametrize("code", [401, 403])
def test_rejected_token(code):
    c = client(lambda req: httpx.Response(code))
    with pytest.raises(PaperlessError, match="rejected the token"):
        c.upload(b"x", "f.pdf")


def test_server_error():
    c = client(lambda req: httpx.Response(500))
    with pytest.raises(PaperlessError, match="500"):
        c.task("t")


def test_connection_error():
    def handler(req):
        raise httpx.ConnectError("nope")

    c = client(handler)
    with pytest.raises(PaperlessError, match="can't reach Paperless"):
        c.upload(b"x", "f.pdf")


def test_ping():
    assert client(lambda r: httpx.Response(200, json={})).ping() is True
    assert client(lambda r: httpx.Response(401)).ping() is False

    def boom(req):
        raise httpx.ConnectTimeout("slow")

    assert client(boom).ping() is False
