from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
from google.oauth2.credentials import Credentials
from sqlalchemy.orm import Session

from app.db.session import get_sessionmaker
from app.drive import folder_layout
from app.drive.fake_drive_client import FakeDriveClient
from app.services import app_settings_service, drive_sync_service, oauth_service


def _fake_credentials() -> Credentials:
    return Credentials(
        token="access-xyz",
        refresh_token="refresh-abc",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="client-id",
        client_secret="client-secret",
        scopes=oauth_service.SCOPES,
        expiry=None,
    )


@pytest.fixture(autouse=True)
def _reset_dirty_flag():
    drive_sync_service._dirty = False
    yield
    drive_sync_service._dirty = False


def test_needs_setup_true_when_no_local_db(configured_settings) -> None:
    assert drive_sync_service.needs_setup() is True


def test_needs_setup_false_once_migrated(migrated_settings) -> None:
    assert drive_sync_service.needs_setup() is False


def test_mark_dirty_flush_now_pushes_snapshot_and_clears_flag(app_db_session: Session) -> None:
    fake_client = FakeDriveClient()
    folder_id = fake_client.get_or_create_folder("BOOTH管理")
    folder_id = fake_client.get_or_create_folder("_db", folder_id)
    uploaded = fake_client.upload_file(
        local_path=drive_sync_service.get_settings().local_db_path,
        name="app.db",
        parent_id=folder_id,
        mime_type="application/x-sqlite3",
    )
    app_settings_service.set_setting(app_db_session, "drive_db_file_id", uploaded.id)

    drive_sync_service.mark_dirty()
    assert drive_sync_service.is_dirty() is True

    synced = drive_sync_service.flush_now(app_db_session, drive_client=fake_client)

    assert synced is True
    assert drive_sync_service.is_dirty() is False
    assert len(fake_client._debug_content(uploaded.id)) > 0
    assert app_settings_service.get_setting(app_db_session, "drive_db_last_pushed_at") is not None


def test_flush_now_recreates_db_file_if_deleted_directly_on_drive(app_db_session: Session) -> None:
    # Reproduces the scenario a user hit in production: they deleted the
    # whole root folder directly in Drive to test that it regenerates. Item
    # folders self-heal via get_or_create_folder on the next upload, but the
    # DB snapshot's fixed drive_db_file_id previously had no equivalent
    # recovery step and every sync failed forever after.
    fake_client = FakeDriveClient()
    root_id = fake_client.get_or_create_folder(folder_layout.ROOT_FOLDER_NAME)
    db_folder_id = fake_client.get_or_create_folder(folder_layout.DB_FOLDER_NAME, root_id)
    uploaded = fake_client.upload_file(
        local_path=drive_sync_service.get_settings().local_db_path,
        name="app.db",
        parent_id=db_folder_id,
        mime_type="application/x-sqlite3",
    )
    app_settings_service.set_setting(app_db_session, "drive_db_file_id", uploaded.id)

    # Simulate deleting the entire root folder by hand in Drive.
    del fake_client._files[uploaded.id]
    del fake_client._files[db_folder_id]
    del fake_client._files[root_id]

    drive_sync_service.mark_dirty()
    synced = drive_sync_service.flush_now(app_db_session, drive_client=fake_client)

    assert synced is True
    assert drive_sync_service.is_dirty() is False
    new_file_id = app_settings_service.get_setting(app_db_session, "drive_db_file_id")
    assert new_file_id != uploaded.id
    assert len(fake_client._debug_content(new_file_id)) > 0
    # The folder tree exists again too.
    assert fake_client.get_or_create_folder(folder_layout.ROOT_FOLDER_NAME) is not None


def test_flush_now_is_noop_when_not_dirty(app_db_session: Session) -> None:
    fake_client = FakeDriveClient()

    synced = drive_sync_service.flush_now(app_db_session, drive_client=fake_client)

    assert synced is False


def test_flush_now_skips_when_drive_db_file_id_missing(app_db_session: Session) -> None:
    drive_sync_service.mark_dirty()
    fake_client = FakeDriveClient()

    synced = drive_sync_service.flush_now(app_db_session, drive_client=fake_client)

    assert synced is False
    assert drive_sync_service.is_dirty() is True  # left dirty; nothing was pushed


def test_complete_first_run_setup_creates_fresh_database(configured_settings) -> None:
    assert drive_sync_service.needs_setup() is True
    fake_client = FakeDriveClient()

    drive_sync_service.complete_first_run_setup(_fake_credentials(), drive_client=fake_client)

    assert drive_sync_service.needs_setup() is False
    session_local = get_sessionmaker()
    with session_local() as db:
        assert oauth_service.is_connected(db)
        file_id = app_settings_service.get_setting(db, "drive_db_file_id")
        assert file_id is not None
        assert fake_client.get_metadata(file_id).name == "app.db"


def test_complete_first_run_setup_restores_existing_database(configured_settings) -> None:
    from app.db.migrate import run_migrations
    from app.db.session import reset_engine_for_tests

    configured_settings.data_dir.mkdir(parents=True, exist_ok=True)
    run_migrations()  # simulate a prior install that already has a DB

    fake_client = FakeDriveClient()
    folder_id = fake_client.get_or_create_folder("BOOTH管理")
    folder_id = fake_client.get_or_create_folder("_db", folder_id)
    uploaded = fake_client.upload_file(
        local_path=configured_settings.local_db_path,
        name="app.db",
        parent_id=folder_id,
        mime_type="application/x-sqlite3",
    )

    configured_settings.local_db_path.unlink()  # simulate volume loss
    reset_engine_for_tests()

    drive_sync_service.complete_first_run_setup(
        _fake_credentials(), drive_db_file_id=uploaded.id, drive_client=fake_client
    )

    assert configured_settings.local_db_path.exists()
    session_local = get_sessionmaker()
    with session_local() as db:
        assert oauth_service.is_connected(db)


def test_check_remote_drift_warns_when_drive_is_newer(
    app_db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    oauth_service.save_credentials(app_db_session, _fake_credentials())

    fake_client = FakeDriveClient()
    folder_id = fake_client.get_or_create_folder("BOOTH管理")
    folder_id = fake_client.get_or_create_folder("_db", folder_id)
    uploaded = fake_client.upload_file(
        local_path=drive_sync_service.get_settings().local_db_path,
        name="app.db",
        parent_id=folder_id,
        mime_type="application/x-sqlite3",
    )
    app_settings_service.set_setting(app_db_session, "drive_db_file_id", uploaded.id)
    stale_time = datetime.now(timezone.utc) - timedelta(hours=1)
    app_settings_service.set_setting(
        app_db_session, "drive_db_last_known_modified_time", stale_time.isoformat()
    )

    with caplog.at_level(logging.WARNING):
        drive_sync_service.check_remote_drift(app_db_session, drive_client=fake_client)

    assert any("looks newer" in record.getMessage() for record in caplog.records)
