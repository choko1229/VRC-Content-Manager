from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.main import app


@pytest.fixture()
def client(app_db_session: Session):
    def override_get_db():
        yield app_db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_shop_create_without_csrf_token_is_rejected(client: TestClient) -> None:
    response = client.post("/fragments/shops", data={"name": "No CSRF Shop"})

    assert response.status_code == 403


def test_shop_create_with_csrf_token_from_page_succeeds(client: TestClient) -> None:
    page = client.get("/shops")
    assert page.status_code == 200
    token = client.cookies  # session cookie carries the token; read it back via the page's meta tag
    import re

    match = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert match is not None
    csrf_token = match.group(1)

    response = client.post(
        "/fragments/shops",
        data={"name": "Real Shop"},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 200
    assert "Real Shop" in response.text


def test_shop_create_with_wrong_csrf_token_is_rejected(client: TestClient) -> None:
    client.get("/shops")

    response = client.post(
        "/fragments/shops", data={"name": "Bad Token Shop"}, headers={"X-CSRF-Token": "wrong-token"}
    )

    assert response.status_code == 403


@pytest.fixture()
def client_with_login(app_db_session: Session):
    from app.config import get_instance_config, get_settings
    from app.core.instance_config import save as save_instance_config

    settings = get_settings()
    config = get_instance_config()
    config.app_login_password = "correct-horse-battery-staple"
    save_instance_config(settings.data_dir, config)

    def override_get_db():
        yield app_db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_protected_page_redirects_to_login_when_password_configured(client_with_login: TestClient) -> None:
    response = client_with_login.get("/shops", follow_redirects=False)

    assert response.status_code in (302, 307)
    assert "/login" in response.headers["location"]


def test_login_with_correct_password_grants_access(client_with_login: TestClient) -> None:
    import re

    login_page = client_with_login.get("/login")
    match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert match is not None

    response = client_with_login.post(
        "/login",
        data={"password": "correct-horse-battery-staple"},
        headers={"X-CSRF-Token": match.group(1)},
        follow_redirects=False,
    )
    assert response.status_code == 303

    shops_response = client_with_login.get("/shops")
    assert shops_response.status_code == 200


def test_login_with_wrong_password_does_not_grant_access(client_with_login: TestClient) -> None:
    import re

    login_page = client_with_login.get("/login")
    match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert match is not None

    client_with_login.post(
        "/login",
        data={"password": "wrong-password"},
        headers={"X-CSRF-Token": match.group(1)},
        follow_redirects=False,
    )

    shops_response = client_with_login.get("/shops", follow_redirects=False)
    assert shops_response.status_code in (302, 307)
