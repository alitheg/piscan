import logging
import queue
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from .paperless import PaperlessClient, PaperlessError
from .pdf import build_pdf
from .store import Store

log = logging.getLogger(__name__)

_TIMEOUT_MESSAGE = "unknown - check Paperless"


def _timeout_message(last_error: str | None) -> str:
    # Never "can't reach Paperless" here: the upload worked, and that wording
    # invites a duplicate retry.
    if last_error:
        return f"{_TIMEOUT_MESSAGE} [{last_error}]"
    return _TIMEOUT_MESSAGE


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Sender:
    def __init__(
        self,
        store: Store,
        client: PaperlessClient,
        clock: Callable[[], datetime] = _utcnow,
        poll_interval: float = 3.0,
        task_timeout: timedelta = timedelta(minutes=15),
        sleep: Callable[[float], bool] | None = None,
    ):
        self.store = store
        self.client = client
        self.clock = clock
        self.poll_interval = poll_interval
        self.task_timeout = task_timeout
        self._stop = threading.Event()
        # Returns True when asked to stop, like Event.wait.
        self._sleep = sleep or self._stop.wait
        # Draft ids. A task id in the store means "already uploaded, keep polling".
        self._queue: queue.Queue[int] = queue.Queue()
        self._health: bool | None = None

    # -- queueing ----------------------------------------------------------

    def enqueue(self, draft_id: int) -> None:
        self._queue_draft(draft_id, "inbox")

    def retry(self, draft_id: int) -> None:
        self._queue_draft(draft_id, "failed")

    def _queue_draft(self, draft_id: int, required: str) -> None:
        draft = self.store.get_draft(draft_id)
        if draft.status != required:
            raise ValueError(f"draft {draft_id} is {draft.status}, not {required}")
        # Marked before queuing so the UI shows it straight away. set_sending
        # clears any old task id, so a restart before the upload re-queues to inbox.
        self.store.set_sending(draft_id)
        self._queue.put(draft_id)

    def recover(self) -> None:
        for draft in self.store.list_drafts(["sending"]):
            if draft.paperless_task_id:
                self._queue.put(draft.id)
            else:
                self.store.reset_to_inbox(draft.id)

    # -- health ------------------------------------------------------------

    def health(self) -> bool | None:
        return self._health

    def check_health(self) -> None:
        self._health = self.client.ping()

    # -- worker ------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        self._stop = stop
        self._sleep = stop.wait
        while not stop.is_set():
            try:
                self.process_one(timeout=1.0)
            except Exception:
                log.exception("sender loop error")

    def process_one(self, timeout: float | None = None) -> bool:
        try:
            if timeout is None:
                draft_id = self._queue.get_nowait()
            else:
                draft_id = self._queue.get(timeout=timeout)
        except queue.Empty:
            return False
        try:
            self._send(draft_id)
        except Exception as e:
            log.exception("sending draft %s failed", draft_id)
            self._fail(draft_id, f"unexpected error: {e}")
        return True

    def _fail(self, draft_id: int, error: str) -> None:
        self.store.mark_failed(draft_id, error)

    def _send(self, draft_id: int) -> None:
        draft = self.store.get_draft(draft_id)
        if draft.status != "sending":
            return  # changed under us; nothing to do
        if draft.paperless_task_id:
            task_id = draft.paperless_task_id
        else:
            try:
                pdf = build_pdf([(p.path, p.rotation) for p in draft.pages])
                name = f"piscan-{draft.created_at.astimezone(UTC):%Y%m%d-%H%M%S}-{draft.id}.pdf"
                task_id = self.client.upload(pdf, name)
            except PaperlessError as e:
                self._fail(draft_id, str(e))
                return
            self.store.set_task(draft_id, task_id)
        self._poll(draft_id, task_id)

    def _poll(self, draft_id: int, task_id: str) -> None:
        deadline = self.clock() + self.task_timeout
        while True:
            poll_error = None
            try:
                result = self.client.task(task_id)
            except PaperlessError as e:
                # Connection blips are retried until the deadline.
                log.warning("polling task %s: %s", task_id, e)
                poll_error = str(e)
            else:
                if result.state == "success":
                    self.store.mark_sent(draft_id, result.document_id, self.clock())
                    return
                if result.state == "failure":
                    self._fail(draft_id, result.error or "Paperless rejected the document")
                    return
            if self.clock() >= deadline:
                self._fail(draft_id, _timeout_message(poll_error))
                return
            if self._sleep(self.poll_interval):
                # Shutting down: stays "sending" with its task id, recover() resumes.
                return
