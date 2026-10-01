"""
Сторож зависання. Працює в окремому потоці — не залежить від asyncio, тому
спрацьовує навіть якщо завис сам event loop.

arm(name, seconds)  — почати відлік дедлайну
disarm()            — відлік більше не потрібен

Якщо дедлайн минув: дамп стеків усіх потоків у stderr (journal) і os._exit(1).
systemd (Restart=on-failure) піднімає сервіс заново.
"""

import faulthandler
import logging
import os
import sys
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_CHECK_INTERVAL_SEC = 5

_lock = threading.Lock()
_name: Optional[str] = None
_deadline: float = 0.0
_thread: Optional[threading.Thread] = None


def arm(name: str, seconds: float) -> None:
    global _name, _deadline, _thread
    with _lock:
        _name = name
        _deadline = time.monotonic() + seconds
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_run, name="hang-watchdog", daemon=True)
            _thread.start()


def disarm() -> None:
    global _name
    with _lock:
        _name = None


def _run() -> None:
    while True:
        time.sleep(_CHECK_INTERVAL_SEC)
        with _lock:
            name, deadline = _name, _deadline
        if name is None or time.monotonic() <= deadline:
            continue

        logger.critical(
            f"[WATCHDOG] Фаза '{name}' не завершилась вчасно — сервіс завис. "
            f"Дамп стеків потоків нижче, перезапуск процесу."
        )
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass
        try:
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(1)
