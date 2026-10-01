"""Phone-first web UI: server-rendered pages, htmx for partial updates."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from piscan.ingest import ScannerState, ScannerStatus
from piscan.sender import Sender
from piscan.store import FAILED, INBOX, SENDING, SENT, Page, Store

HERE = Path(__file__).parent
LIVE = [INBOX, SENDING, FAILED]
SENT_WINDOW = timedelta(hours=24)
IMAGE_HEADERS = {"Cache-Control": "private, max-age=3600"}


def clock(dt: datetime) -> str:
    """HH:MM for today, a short date otherwise, in the Pi's local time."""
    local = dt.astimezone()
    if local.date() == datetime.now(UTC).astimezone().date():
        return local.strftime("%H:%M")
    return f"{local.day} {local.strftime('%b')}"


def scanner_text(s: ScannerStatus) -> str:
    match s.state:
        case ScannerState.OFF:
            return "Scanner off / unplugged"
        case ScannerState.SCANNING:
            return "Scanning..."
        case ScannerState.IMPORTING:
            return f"Importing... {s.done} of {s.total}"
        case ScannerState.READY:
            return "Ready"
        case _:
            return f"Problem: {s.message}"


def create_app(
    store: Store,
    sender: Sender,
    scanner_status: Callable[[], ScannerStatus],
    paperless_health: Callable[[], bool | None],
    paperless_url: str,
) -> FastAPI:
    app = FastAPI(title="piscan", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["clock"] = clock
    base_url = paperless_url.rstrip("/")

    def is_htmx(request: Request) -> bool:
        return request.headers.get("HX-Request") == "true"

    def render(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def status_ctx() -> dict:
        status = scanner_status()
        return {
            "scanner": status,
            "scanner_text": scanner_text(status),
            "paperless": paperless_health(),
            "draft_count": len(store.list_drafts(LIVE)),
        }

    def sent_ctx() -> dict:
        cutoff = datetime.now(UTC) - SENT_WINDOW
        sent = [
            d
            for d in store.list_drafts([SENT])
            if d.sent_at is not None and d.sent_at >= cutoff
        ]
        sent.sort(key=lambda d: d.sent_at, reverse=True)
        return {"sent": sent, "paperless_url": base_url}

    def drafts_response(request: Request, error: str | None = None) -> HTMLResponse:
        ctx = {"drafts": store.list_drafts(LIVE), "error": error}
        status = 409 if error else 200
        if is_htmx(request):
            return render(request, "_drafts.html", status, **ctx)
        return render(
            request, "index.html", status, **ctx, **status_ctx(), **sent_ctx()
        )

    def list_action(request: Request, action: Callable[[], None]) -> Response:
        try:
            action()
        except KeyError:
            raise HTTPException(404, "No such draft") from None
        except ValueError as e:
            return drafts_response(request, str(e))
        if is_htmx(request):
            return drafts_response(request)
        return RedirectResponse("/", status_code=303)

    def find_page(page_id: int) -> Page:
        for d in store.list_drafts([INBOX, SENDING, FAILED, SENT]):
            for p in d.pages:
                if p.id == page_id:
                    return p
        raise HTTPException(404, "No such page")

    def draft_response(
        request: Request, draft_id: int, error: str | None = None
    ) -> Response:
        try:
            draft = store.get_draft(draft_id)
        except KeyError:
            # The draft went away (its last page was deleted).
            if is_htmx(request):
                return Response(status_code=200, headers={"HX-Redirect": "/"})
            return RedirectResponse("/", status_code=303)
        status = 409 if error else 200
        if is_htmx(request):
            return render(request, "_draft_view.html", status, draft=draft, error=error)
        if error:
            return render(request, "draft.html", status, draft=draft, error=error)
        return RedirectResponse(f"/drafts/{draft_id}", status_code=303)

    def page_action(
        request: Request, page_id: int, action: Callable[[int], object]
    ) -> Response:
        page = find_page(page_id)
        error = None
        try:
            action(page_id)
        except KeyError:
            raise HTTPException(404, "No such page") from None
        except ValueError as e:
            error = str(e)
        return draft_response(request, page.draft_id, error)

    # -- pages and fragments ----------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return drafts_response(request)

    @app.get("/fragments/status", response_class=HTMLResponse)
    def status_fragment(request: Request):
        return render(request, "_status.html", **status_ctx())

    @app.get("/fragments/drafts", response_class=HTMLResponse)
    def drafts_fragment(request: Request):
        return render(request, "_drafts.html", drafts=store.list_drafts(LIVE), error=None)

    @app.get("/fragments/sent", response_class=HTMLResponse)
    def sent_fragment(request: Request):
        return render(request, "_sent.html", **sent_ctx())

    @app.get("/drafts/{draft_id}", response_class=HTMLResponse)
    def draft_view(request: Request, draft_id: int):
        try:
            draft = store.get_draft(draft_id)
        except KeyError:
            raise HTTPException(404, "No such draft") from None
        name = "_draft_view.html" if is_htmx(request) else "draft.html"
        return render(request, name, draft=draft, error=None)

    # -- draft actions -----------------------------------------------------

    @app.post("/merge")
    def merge(request: Request, draft_id: Annotated[list[int], Form(default_factory=list)]):
        return list_action(request, lambda: store.merge(draft_id))

    @app.post("/delete")
    def delete(request: Request, draft_id: Annotated[list[int], Form(default_factory=list)]):
        def run() -> None:
            for i in draft_id:
                store.delete_draft(i)

        return list_action(request, run)

    @app.post("/drafts/{draft_id}/send")
    def send(request: Request, draft_id: int):
        return list_action(request, lambda: sender.enqueue(draft_id))

    @app.post("/drafts/{draft_id}/retry")
    def retry(request: Request, draft_id: int):
        return list_action(request, lambda: sender.retry(draft_id))

    @app.post("/send-all")
    def send_all(request: Request):
        def run() -> None:
            for d in store.list_drafts([INBOX]):
                try:
                    sender.enqueue(d.id)
                except (ValueError, KeyError):
                    continue

        return list_action(request, run)

    # -- page actions ------------------------------------------------------

    @app.post("/pages/{page_id}/rotate")
    def rotate(request: Request, page_id: int):
        return page_action(request, page_id, store.rotate)

    @app.post("/pages/{page_id}/up")
    def up(request: Request, page_id: int):
        return page_action(request, page_id, lambda i: store.move_page(i, -1))

    @app.post("/pages/{page_id}/down")
    def down(request: Request, page_id: int):
        return page_action(request, page_id, lambda i: store.move_page(i, 1))

    @app.post("/pages/{page_id}/split")
    def split(request: Request, page_id: int):
        return page_action(request, page_id, store.split)

    @app.post("/pages/{page_id}/delete")
    def delete_page(request: Request, page_id: int):
        return page_action(request, page_id, store.delete_page)

    # -- images ------------------------------------------------------------

    def image_response(page_id: int, thumb: bool) -> FileResponse:
        page = find_page(page_id)
        path = page.thumb_path if thumb else page.path
        if not path.is_file():
            raise HTTPException(404, "File gone")
        return FileResponse(path, media_type="image/jpeg", headers=IMAGE_HEADERS)

    @app.get("/pages/{page_id}/thumb")
    def thumb(page_id: int):
        return image_response(page_id, True)

    @app.get("/pages/{page_id}/image")
    def image(page_id: int):
        return image_response(page_id, False)

    return app
