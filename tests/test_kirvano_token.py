"""Kirvano token acceptance (enforce mode: header + body; log mode: header-name capture)."""

from sqlalchemy import select

from app.kirvano import auth_debug_info, check_token, extract_token
from app.models import WebhookEvent
from tests.conftest import dumps, kirvano_payload


def test_extract_token_sources():
    assert extract_token({"X-Kirvano-Token": "abc"}, {}) == ("abc", "header:x-kirvano-token")
    assert extract_token({"Authorization": "Bearer abc"}, {}) == ("abc", "header:authorization")
    assert extract_token({"authorization": "abc"}, {}) == ("abc", "header:authorization")
    assert extract_token({}, {"token": "abc"}) == ("abc", "body:token")
    assert extract_token({}, {"security_token": "abc"}) == ("abc", "body:security_token")
    assert extract_token({}, {}) == (None, "none")


def test_check_token_modes(settings, monkeypatch):
    assert check_token(settings, {"x-token": "kirvano-test-token"}, {}).accepted
    assert not check_token(settings, {"x-token": "nope"}, {}).accepted
    assert check_token(settings, {}, {}).reason == "missing"
    monkeypatch.setattr(settings, "kirvano_token_mode", "log")
    assert check_token(settings, {}, {}).accepted
    monkeypatch.setattr(settings, "kirvano_token_mode", "enforce")
    monkeypatch.setattr(settings, "kirvano_webhook_token", None)
    assert check_token(settings, {"x-token": "x"}, {}).reason == "no_token_configured"


def test_enforce_mode_accepts_header_token(client):
    r = client.post(
        "/webhooks/kirvano",
        content=dumps(kirvano_payload()),
        headers={"x-kirvano-token": "kirvano-test-token"},
    )
    assert r.status_code == 200 and r.json()["outcome"] == "processed"


def test_enforce_mode_accepts_bearer_and_other_headers(client):
    r = client.post(
        "/webhooks/kirvano",
        content=dumps(kirvano_payload(sale_id="B1")),
        headers={"Authorization": "Bearer kirvano-test-token"},
    )
    assert r.status_code == 200 and r.json()["outcome"] == "processed"
    r = client.post(
        "/webhooks/kirvano",
        content=dumps(kirvano_payload(sale_id="B2")),
        headers={"security-token": "kirvano-test-token"},
    )
    assert r.status_code == 200 and r.json()["outcome"] == "processed"


def test_enforce_mode_accepts_body_token(client):
    payload = kirvano_payload(sale_id="C1", extra={"token": "kirvano-test-token"})
    r = client.post("/webhooks/kirvano", json=payload)
    assert r.status_code == 200 and r.json()["outcome"] == "processed"


def test_enforce_mode_rejects_wrong_or_missing_token(client, session):
    r = client.post(
        "/webhooks/kirvano", json=kirvano_payload(), headers={"x-kirvano-token": "wrong"}
    )
    assert r.status_code == 401
    r = client.post("/webhooks/kirvano", json=kirvano_payload())
    assert r.status_code == 401
    assert session.execute(select(WebhookEvent)).scalar_one_or_none() is None


def test_invalid_json_returns_200(client):
    r = client.post(
        "/webhooks/kirvano", content=b"nope", headers={"x-kirvano-token": "kirvano-test-token"}
    )
    assert r.status_code == 200 and r.json()["outcome"] == "invalid_json"


def test_log_mode_records_header_names_never_values(settings, engine, monkeypatch):
    from fastapi.testclient import TestClient

    from app import db
    from app.main import create_app

    monkeypatch.setattr(settings, "kirvano_token_mode", "log")
    with TestClient(create_app(settings)) as c:
        r = c.post(
            "/webhooks/kirvano",
            json=kirvano_payload(extra={"token": "SECRET-VALUE"}),
            headers={"X-Kirvano-Token": "SECRET-HEADER"},
        )
        assert r.status_code == 200 and r.json()["outcome"] == "processed"
    with db.session_scope() as s:
        evt = s.execute(select(WebhookEvent)).scalar_one()
        assert evt.auth_debug is not None
        assert "x-kirvano-token" in evt.auth_debug["header_names"]
        assert evt.auth_debug["token_headers_present"] == ["x-kirvano-token"]
        assert evt.auth_debug["token_body_fields_present"] == ["token"]
        assert "SECRET-HEADER" not in str(evt.auth_debug) and "SECRET-HEADER" not in str(
            evt.headers_meta
        )
        # The body transport is one of the two Kirvano may use, and the stored payload
        # is rendered RAW on the panel's Eventos page (and lives in every backup):
        # the shared secret must never survive into it.
        import json as _json

        assert "token" not in evt.payload
        assert "SECRET-VALUE" not in _json.dumps(evt.payload)
        # Acceptance still worked — check_token reads the ORIGINAL body, not the
        # redacted copy.
        assert evt.auth_debug["token_body_fields_present"] == ["token"]


def test_body_token_is_redacted_in_enforce_mode(settings, engine):
    """Same protection when the token is the real one and enforce mode accepts it."""
    import json as _json

    from fastapi.testclient import TestClient

    from app import db
    from app.main import create_app

    with TestClient(create_app(settings)) as c:
        r = c.post(
            "/webhooks/kirvano",
            json=kirvano_payload(
                extra={"token": "kirvano-test-token", "security_token": "kirvano-test-token"}
            ),
        )
        assert r.status_code == 200 and r.json()["outcome"] == "processed"
    with db.session_scope() as s:
        evt = s.execute(select(WebhookEvent)).scalar_one()
        assert "kirvano-test-token" not in _json.dumps(evt.payload)
        assert "token" not in evt.payload and "security_token" not in evt.payload
        # Everything else is still stored verbatim.
        assert evt.payload["sale_id"] == "D2RP8RQ7"


def test_auth_debug_info_shape():
    info = auth_debug_info(
        {"Content-Type": "application/json", "Authorization": "Bearer x"}, {"security_token": "y"}
    )
    assert info == {
        "header_names": ["authorization", "content-type"],
        "token_headers_present": ["authorization"],
        "token_body_fields_present": ["security_token"],
    }
