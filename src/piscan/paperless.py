from dataclasses import dataclass
from typing import Literal

import httpx


class PaperlessError(Exception):
    """Message is meant to be shown to the user as is."""


@dataclass
class TaskResult:
    state: Literal["pending", "success", "failure"]
    document_id: int | None = None
    error: str | None = None


class PaperlessClient:
    def __init__(
        self,
        url: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30,
    ):
        self._http = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Token {token}"},
            transport=transport,
            timeout=timeout,
        )

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise PaperlessError("can't reach Paperless") from e
        if resp.status_code in (401, 403):
            raise PaperlessError("Paperless rejected the token")
        if not resp.is_success:
            raise PaperlessError(f"Paperless returned HTTP {resp.status_code}")
        return resp

    def _json(self, resp: httpx.Response):
        try:
            return resp.json()
        except ValueError as e:
            raise PaperlessError("Paperless sent an unreadable response") from e

    def upload(self, pdf: bytes, filename: str) -> str:
        resp = self._request(
            "POST",
            "/api/documents/post_document/",
            files={"document": (filename, pdf, "application/pdf")},
        )
        task_id = self._json(resp)
        if not isinstance(task_id, str) or not task_id:
            raise PaperlessError("Paperless sent an unexpected upload response")
        return task_id

    def task(self, task_id: str) -> TaskResult:
        resp = self._request("GET", "/api/tasks/", params={"task_id": task_id})
        items = self._json(resp)
        if not isinstance(items, list) or not items:
            return TaskResult("pending")  # not registered yet
        item = items[0]
        status = str(item.get("status", "")).upper()
        if status == "SUCCESS":
            doc = item.get("related_document")
            try:
                doc_id = int(doc) if doc not in (None, "") else None
            except (TypeError, ValueError):
                doc_id = None
            return TaskResult("success", document_id=doc_id)
        if status == "FAILURE":
            error = item.get("result") or "Paperless rejected the document"
            return TaskResult("failure", error=str(error))
        return TaskResult("pending")

    def verify(self) -> None:
        """Raise PaperlessError (with a readable reason) unless the token is accepted."""
        self._request("GET", "/api/")

    def ping(self) -> bool:
        try:
            self.verify()
        except PaperlessError:
            return False
        return True
