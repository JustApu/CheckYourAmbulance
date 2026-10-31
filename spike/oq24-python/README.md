# OQ-24 spike, Python candidate ([issue #15](https://github.com/JustApu/CheckYourAmbulance-archive/issues/15)) — THROWAWAY prototypes

**Not application code.** (The stack decision is in SPEC section 17.5; this directory is only the evidence.) **The WebSocket consumer here is deliberately unauthenticated; do not copy it.** Same rules as [`../oq24/README.md`](../oq24/README.md): synthetic data,
private socket-only database, evidence for [`docs/spikes/oq24-architecture-spike.md`](../../docs/spikes/oq24-architecture-spike.md).
Reuses the schema in `../oq24/sql/schema.sql` so both language candidates face the identical database.

## Contents

| Path | Purpose |
|---|---|
| `spikepy/db.py`, `spikepy/check.py` | Raw-driver helpers and the check-recording algorithm (mysqlclient) |
| `spikepy/dj.py`, `models.py`, `orm_check.py` | Standalone Django configuration, unmanaged models, the same algorithm through the Django ORM |
| `spikepy/urls.py`, `asgi.py`, `serve.py` | Web-layer probes (CSRF, cookies, SSE, WebSocket via Channels, Daphne on loopback) |
| `tests/` | One test module per criterion (stdlib `unittest`) |

## How to run

```sh
python3 -m venv local/pyvenv   # local/ is gitignored
local/pyvenv/bin/pip install Django mysqlclient PyJWT cryptography channels daphne
# start a private MariaDB (see ../oq24/README.md) and export SPIKE_SOCK, SPIKE_CLIENT, SPIKE_DUMP, SPIKE_BINLOG, SPIKE_BASE
cd spike/oq24-python && ../../local/pyvenv/bin/python -m unittest discover -s tests -t .
```

Package versions tested: Python 3.13.16, Django 6.1.2, mysqlclient 2.3.0, PyJWT 2.15.1, Channels 4.3.2, Daphne 4.2.3, cryptography 50.0.2.
