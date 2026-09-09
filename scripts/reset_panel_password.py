"""Recovery: forget the panel password set in the panel.

Run this on the server when the operator loses the password he chose in
``/painel/senha``. It deletes the override row, after which the panel accepts
``PANEL_PASSWORD`` from ``/etc/pix-recovery/env`` again.

    sudo -u pixapp /opt/pix-recovery/.venv/bin/python -m scripts.reset_panel_password

Nothing else is touched: no orders, no jobs, no messages.
"""

from __future__ import annotations

import sys

from app.db import session_scope
from app.panel_password import clear_password


def main() -> int:
    with session_scope() as session:
        # session_scope() commits on clean exit; no explicit commit needed.
        removed = clear_password(session)
    if removed:
        print("Senha do painel apagada.")
        print("Agora vale a senha PANEL_PASSWORD do arquivo /etc/pix-recovery/env.")
    else:
        print("Nenhuma senha havia sido definida no painel.")
        print("A senha em uso já é a PANEL_PASSWORD do arquivo /etc/pix-recovery/env.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
