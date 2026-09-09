"""Guards on the deploy scripts that cannot be exercised without a real VPS.

They are grep-level assertions on purpose: the failures they protect against are
one-line regressions that silently take production down (an unconditional vhost
rewrite that destroys TLS, a secrets file left behind on disk), and a broken deploy
is only discovered when Meta and Kirvano stop being able to reach the webhooks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
README = Path(__file__).resolve().parents[1] / "README.md"


@pytest.fixture(scope="module")
def setup_sh() -> str:
    return (DEPLOY / "setup_server.sh").read_text(encoding="utf-8")


def test_nginx_vhost_is_never_rewritten_over_certbots_edits(setup_sh: str):
    """Re-running the provisioner must not delete the port-443 server block.

    `certbot --nginx` edits the vhost IN PLACE. The script used to truncate and
    rewrite it unconditionally, then skip certbot because the certificate already
    existed — leaving the site HTTP-only and both webhooks dark. README section 9.2
    prescribes exactly this re-run as the recovery from Hostinger's "Reset SSH".
    """
    block = setup_sh.split('step "nginx"')[1].split('step "Unidades systemd"')[0]
    assert "grep -q 'listen 443'" in block
    # The rewrite must live inside the guard, not before it.
    guard_at = block.index("grep -q 'listen 443'")
    rewrite_at = block.index("nginx-pix-api.conf")
    assert guard_at < rewrite_at


def test_tls_step_reinstalls_when_the_vhost_lost_its_certificate(setup_sh: str):
    tls = setup_sh.split('step "Certificado TLS')[1]
    assert "--reinstall" in tls and "--keep-until-expiring" in tls
    # --keep-until-expiring means no new certificate is requested, so re-running the
    # script cannot burn the Let's Encrypt failure rate limit.
    assert "NGINX_HAS_TLS" in tls


def test_uploaded_secrets_file_is_consumed(setup_sh: str):
    """$ENV_SRC holds every secret at scp's default 0644 and is never hardened."""
    assert 'shred -u "$ENV_SRC"' in setup_sh
    assert 'rm -f "$ENV_SRC"' in setup_sh
    # Never delete the destination by accident.
    assert '[ "$ENV_SRC" != "$ENV_FILE" ]' in setup_sh
    assert "$ENV_SRC removido" in setup_sh
    # The operator has to know it must be re-uploaded on a rebuild.
    assert "consumido" in README.read_text(encoding="utf-8")


def test_setup_ends_with_a_verification_run(setup_sh: str):
    """A broken TLS state must be reported, not silent."""
    assert 'bash "$SCRIPT_DIR/check.sh"' in setup_sh


def test_retention_timer_is_installed_and_armed(setup_sh: str):
    """The 12-month limit the privacy page promises needs something that runs it."""
    assert (DEPLOY / "pix-retention.service").is_file()
    assert (DEPLOY / "pix-retention.timer").is_file()
    assert "pix-retention" in setup_sh
    assert "systemctl enable --now pix-retention.timer" in setup_sh

    unit = (DEPLOY / "pix-retention.service").read_text(encoding="utf-8")
    assert "scripts/purge_old_data.py" in unit
    assert "Type=oneshot" in unit
    timer = (DEPLOY / "pix-retention.timer").read_text(encoding="utf-8")
    assert "OnCalendar=" in timer and "Persistent=true" in timer
