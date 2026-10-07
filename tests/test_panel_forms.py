"""The Carrinho and Pós-venda forms, exactly as a browser renders and submits them.

The other panel tests post hand-built forms, which is how "Quantidade de mensagens"
(``cart_steps``) went missing from the page unnoticed: its name starts like the
per-message fields, the template filtered it out, and every real save then failed.
"""

from __future__ import annotations

from html.parser import HTMLParser

import pytest

from app.settings_store import SettingsStore

AUTH = ("admin", "panel-pw")
ORIGIN = {"Origin": "http://testserver"}
PAGES = [("/painel/carrinho", "cart"), ("/painel/pos-venda", "post")]


class _FormFields(HTMLParser):
    """What a browser would submit for the form whose action is ``action``."""

    def __init__(self, action: str) -> None:
        super().__init__()
        self.action = action
        self.inside = False
        self.textarea: str | None = None
        self.data: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.inside = a.get("action") == self.action
        elif self.inside and tag == "input" and a.get("name"):
            if a.get("type") == "checkbox" and "checked" not in a:
                return  # an unchecked box is simply absent from the body
            self.data[a["name"]] = a.get("value") or ""
        elif self.inside and tag == "textarea" and a.get("name"):
            self.textarea = a["name"]
            self.data[self.textarea] = ""

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False
        elif tag == "textarea":
            self.textarea = None

    def handle_data(self, data):
        if self.textarea:
            self.data[self.textarea] += data


def _rendered_form(client, path: str) -> dict[str, str]:
    page = client.get(path, auth=AUTH)
    assert page.status_code == 200
    parser = _FormFields(path)
    parser.feed(page.text)
    return parser.data


@pytest.mark.parametrize(("path", "group"), PAGES)
def test_every_setting_of_the_page_has_exactly_one_field(client, session, path, group):
    page = client.get(path, auth=AUTH).text
    for d in SettingsStore.group_definitions(group):
        assert page.count(f'name="{d.key}"') == 1, d.key


@pytest.mark.parametrize(("path", "group"), PAGES)
def test_the_form_saves_as_the_browser_submits_it(client, session, store, path, group):
    data = _rendered_form(client, path)
    if group == "cart":
        data["cart_coupon"] = "DESCONTO5"  # the default message uses {{coupon}}
        data["cart_steps"] = "2"
        data["cart_step2_template"] = "carrinho_lembrete_v1"
    else:
        data["post_steps"] = "2"
        data["post_step2_template"] = "pos_venda_acompanhamento_v1"
    r = client.post(path, auth=AUTH, data=data, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303, r.text[:500]
    store.refresh()
    steps = store.cart_steps if group == "cart" else store.post_steps
    assert steps == 2
