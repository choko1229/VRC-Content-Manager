"""db_write_lock is what keeps every DB-writing entrypoint (request-path item
create/update/delete/merge, plus the background upload_sync_service /
drive_sync_service / drive_reconcile_service / dedup-sweep flows) from racing
SQLite's busy_timeout when several fire close together -- see
app/db/session.py. This exercises the lock itself as a plain mutual-exclusion
primitive (and its reentrant nesting, since some entrypoints call each other
on the same thread) rather than re-testing each service's full DB plumbing
under real threading, which each already has its own single-threaded coverage
for."""

from __future__ import annotations

import threading
import time

from app.db.session import db_write_lock


def test_db_write_lock_serializes_concurrent_critical_sections() -> None:
    in_critical_section = threading.Event()
    overlap_detected = threading.Event()
    entries = 0
    lock = threading.Lock()  # guards the plain `entries` counter itself

    def critical_work() -> None:
        nonlocal entries
        with db_write_lock:
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


def test_db_write_lock_allows_reentrant_acquire_on_the_same_thread() -> None:
    # Several entrypoints call each other on the same thread (e.g.
    # auto_merge_duplicate_products -> merge_item_into, merge_duplicate_group
    # -> delete_item) -- a plain threading.Lock would deadlock on the nested
    # acquire below; db_write_lock must be an RLock so this just works.
    with db_write_lock:
        with db_write_lock:
            entered = True
    assert entered


def test_db_write_lock_still_blocks_other_threads_while_held_reentrantly() -> None:
    other_thread_acquired = threading.Event()

    def try_acquire() -> None:
        with db_write_lock:
            other_thread_acquired.set()

    with db_write_lock:
        with db_write_lock:
            t = threading.Thread(target=try_acquire)
            t.start()
            acquired_while_held = other_thread_acquired.wait(timeout=0.2)
        # Still holding the outer level here -- t must still be blocked.
        assert other_thread_acquired.is_set() is False
    t.join(timeout=5)

    assert acquired_while_held is False
    assert other_thread_acquired.is_set()  # it does get in once the holder releases both levels
