"""Background dispatcher that delivers, retries and re-fires webhooks."""
from __future__ import annotations

import hashlib
import hmac
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .store import Store

USER_AGENT = "decisions-webhooks/1"


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(secret: str, body: bytes, header: str | None) -> bool:
    return bool(header) and hmac.compare_digest(sign(secret, body), header)


class Dispatcher(threading.Thread):
    def __init__(self, store: Store, workers: int = 4, poll_interval: float = 0.5):
        super().__init__(daemon=True, name="webhook-dispatcher")
        self.store = store
        self.poll_interval = poll_interval
        self.stopping = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="webhook")
        self.in_flight: set[str] = set()
        self.in_flight_lock = threading.Lock()

    def stop(self):
        self.stopping.set()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def run(self):
        while not self.stopping.is_set():
            try:
                for delivery in self.store.due_deliveries():
                    with self.in_flight_lock:
                        if delivery["id"] in self.in_flight:
                            continue
                        self.in_flight.add(delivery["id"])
                    self.pool.submit(self._attempt, delivery)
            except Exception as exc:  # keep the dispatcher alive no matter what
                print(f"[webhooks] dispatcher error: {exc!r}")
            self.stopping.wait(self.poll_interval)

    def _attempt(self, delivery: dict):
        try:
            body = delivery["payload"].encode()
            req = urllib.request.Request(delivery["sub_url"], data=body, method="POST", headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "X-Decisions-Event": delivery["event_type"],
                "X-Decisions-Delivery": delivery["id"],
                "X-Decisions-Attempt": str(delivery["total_attempts"] + 1),
                "X-Decisions-Signature": sign(delivery["sub_secret"], body),
            })
            started = time.monotonic()
            status, error = None, None
            try:
                with urllib.request.urlopen(req, timeout=self.store.cfg.webhook_timeout) as resp:
                    status = resp.status
            except urllib.error.HTTPError as exc:
                status, error = exc.code, f"HTTP {exc.code}"
                exc.close()
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            self.store.record_attempt(delivery, status, error, int((time.monotonic() - started) * 1000))
        finally:
            with self.in_flight_lock:
                self.in_flight.discard(delivery["id"])
