"""Database polling with commit-after-callback watermarks."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .models.message import from_db_row
from .services import ConversationService


class ListenerService:
    def __init__(self, service: ConversationService, state_file: Path,
                 interval: float = 1.0, chat_factory=None):
        self.service = service
        self.state_file = state_file
        self.interval = interval
        self.chat_factory = chat_factory
        self._callbacks: dict[str, tuple[str, object]] = {}
        self._watermarks: dict[str, int] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if state_file.exists():
            try:
                self._watermarks = {k: int(v) for k, v in json.loads(state_file.read_text("utf-8")).items()}
            except (OSError, ValueError, TypeError):
                raise ValueError(f"监听水位文件损坏：{state_file}")

    def _key(self, chat_id: str) -> str:
        return f"{self.service.database.account_id}|{chat_id}"

    def add(self, target: str, callback, *, replay: bool = False):
        ident, name = self.service.resolve(target)
        key = self._key(ident)
        if not replay and key not in self._watermarks:
            latest = self.service.database.messages(ident, limit=1)
            self._watermarks[key] = int(latest[0]["sort_seq"]) if latest else 0
            self._persist()
        self._callbacks[ident] = (name, callback)

    def remove(self, target: str):
        ident, _ = self.service.resolve(target)
        self._callbacks.pop(ident, None)

    def _persist(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._watermarks, ensure_ascii=False), "utf-8")
        tmp.replace(self.state_file)

    def poll_once(self) -> int:
        delivered = 0
        for ident, (name, callback) in list(self._callbacks.items()):
            key = self._key(ident)
            while True:
                since = self._watermarks.get(key, 0)
                rows = self.service.database.new_messages(ident, since, limit=200)
                if not rows:
                    break
                for row in rows:
                    seq = int(row["sort_seq"])
                    if seq <= self._watermarks.get(key, 0):
                        continue
                    msg = from_db_row(row, account_id=self.service.database.account_id,
                                      chat_id=ident, chat_name=name, actions=self.service)
                    callback(msg, self.chat_factory(ident) if self.chat_factory else self.service)
                    self._watermarks[key] = seq
                    self._persist()
                    delivered += 1
                if len(rows) < 200:
                    break
        return delivered

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="mywxplus-listener")
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                # No watermark advance; a later poll retries the same message.
                import logging
                logging.getLogger(__name__).exception("监听回调失败，保留水位等待重试")
            self._stop.wait(self.interval)

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
            self._thread = None
