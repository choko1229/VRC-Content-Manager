"""background_write_lock is what keeps the background DB-writing flows
(upload_sync_service, drive_sync_service, drive_reconcile_service,
item_service's dedup sweep) from racing SQLite's busy_timeout when several
fire close together -- see app/db/session.py. This exercises the lock
itself as a plain mutual-exclusion primitive rather than re-testing each
service's full DB plumbing under real threading, which each already has
its own single-threaded coverage for."""

from __future__ import annotations

import threading
import time

from app.db.session import background_write_lock


def test_background_write_lock_serializes_concurrent_critical_sections() -> None:
    in_critical_section = threading.Event()
    overlap_detected = threading.Event()
    entries = 0
    lock = threading.Lock()  # guards the plain `entries` counter itself

    def critical_work() -> None:
        nonlocal entries
        with background_write_lock:
            if in_critical_section.is_set():
                overlap_detected.set()
            in_critical_section.set()
            with lock:
                entries += 1
            time.sleep(0.05)  # long enough that a second thread would overlap if unguarded
            in_critical_section.clear()

    threads = [threading.Thread(target=critical_work) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert entries == 5
    assert not overlap_detected.is_set()
