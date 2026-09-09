"""Operator scripts.

Most of them use httpx + app.config only and never touch the database;
``purge_old_data.py`` is the exception (it is the retention job).

- subscribe_app.py     POST /{WABA_ID}/subscribed_apps  (run once after the webhook is verified)
- check_template.py    template status/category from the Graph API
- send_test.py         send one template or free-text message by hand
- simulate_kirvano.py  post a realistic Kirvano webhook to a local/remote URL
- purge_old_data.py    apply the 12-month retention promised on /privacidade

They are a package so `uv run python -m scripts.send_test` works from the repo
root; each module also inserts the repo root into sys.path so the plain
`uv run python scripts/send_test.py` form works too.
"""
