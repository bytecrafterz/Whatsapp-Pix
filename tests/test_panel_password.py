"""Panel password rotation — the operator must be able to change it without SSH."""

from __future__ import annotations

import pytest

from app.panel_password import (
    PasswordError,
    check_panel_password,
    clear_password,
    get_password_hash,
    hash_password,
    set_password,
    validate_new_password,
    verify_password,
)


def test_hash_roundtrip():
    encoded = hash_password("uma senha longa", iterations=1000)
    assert verify_password("uma senha longa", encoded)
    assert not verify_password("outra senha", encoded)


def test_hash_is_salted():
    """Two hashes of the same password must differ, or the salt is not doing its job."""
    assert hash_password("mesma senha", iterations=1000) != hash_password(
        "mesma senha", iterations=1000
    )


@pytest.mark.parametrize(
    "encoded",
    ["", "lixo", "pbkdf2_sha256$naoumnumero$aa$bb", "sha1$1000$aa$bb", "pbkdf2_sha256$1000$zz$yy"],
)
def test_corrupt_hash_never_raises(encoded):
    """A damaged settings row must fail closed, not 500 the login screen."""
    assert verify_password("qualquer", encoded) is False


def test_env_password_used_until_one_is_stored(session):
    assert get_password_hash(session) is None
    assert check_panel_password(session, "doenv", "doenv")
    assert not check_panel_password(session, "errada", "doenv")


def test_stored_password_replaces_env(session):
    set_password(session, "senhanovalonga")
    session.commit()
    assert get_password_hash(session) is not None
    assert check_panel_password(session, "senhanovalonga", "doenv")
    # The env password must stop working once an override exists, otherwise
    # rotating the password would not actually revoke the old one.
    assert not check_panel_password(session, "doenv", "doenv")


def test_set_password_twice_keeps_only_the_latest(session):
    set_password(session, "primeirasenha1")
    session.commit()
    set_password(session, "segundasenha12")
    session.commit()
    assert check_panel_password(session, "segundasenha12", None)
    assert not check_panel_password(session, "primeirasenha1", None)


def test_clear_password_restores_env(session):
    set_password(session, "senhanovalonga")
    session.commit()
    assert clear_password(session) is True
    session.commit()
    assert get_password_hash(session) is None
    assert check_panel_password(session, "doenv", "doenv")
    assert clear_password(session) is False


def test_no_password_anywhere_denies(session):
    assert not check_panel_password(session, "qualquer", None)
    assert not check_panel_password(session, "", "")


@pytest.mark.parametrize(
    ("new", "confirm", "fragment"),
    [
        ("", "", "Digite a nova senha"),
        ("senhaboa123", "outrasenha1", "confirmação"),
        ("curta", "curta", "10 caracteres"),
        (" comespaco123 ", " comespaco123 ", "espaço"),
    ],
)
def test_validate_rejects(new, confirm, fragment):
    with pytest.raises(PasswordError) as exc:
        validate_new_password(new, confirm)
    assert fragment in str(exc.value)


def test_validate_accepts_a_good_pair():
    validate_new_password("uma senha boa 123", "uma senha boa 123")
