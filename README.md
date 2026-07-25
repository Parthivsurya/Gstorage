# Gstore

A local-only desktop app that pools several Google accounts (e.g. 3 × 15 GB)
into one unified virtual drive. File data flows directly between your machine
and Google — never through any server.

See [drive-aggregator-implementation-plan.md](drive-aggregator-implementation-plan.md)
for the full plan.

## Status

- **Phase 0 — Setup: complete.** App scaffold renders a window; local SQLite
  index created in the OS app-data directory.
- **Phase 1 — OAuth + multi-account login: complete.** PKCE loopback flow links
  N Google accounts; credentials are Fernet-encrypted at rest with the master
  key held in the OS keychain, and refreshed automatically on load.

## Requirements

- Python 3.11+
- macOS / Linux / Windows

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
```

## Run

```bash
gstore
# or
python -m gstore
```

The local index is created on first launch at the OS app-data path
(macOS: `~/Library/Application Support/Gstore/gstore.db`).

## Layout

```
src/gstore/
  app.py            entry point (opens DB, launches window)
  config.py         app-data paths
  db/
    database.py     SQLite connection + schema bootstrap
    schema.sql      index schema (accounts, folders, files, chunks)
  ui/
    main_window.py  Qt main window
```

## Google Cloud setup (required before Phase 1)

Phase 1 (OAuth login) needs a Google Cloud project. Manual steps:

1. Create a project at <https://console.cloud.google.com>.
2. Enable the **Google Drive API**.
3. Configure the **OAuth consent screen** (External, Testing mode — add your
   own Google accounts as test users).
4. Create an **OAuth client ID** of type **Desktop app**.
5. Download the client JSON. Requested scope:
   `https://www.googleapis.com/auth/drive.file`.

## Adding accounts (Phase 1)

Place the downloaded client JSON where Gstore looks for it:

```bash
# macOS default location:
cp ~/Downloads/client_secret_*.json \
   "$HOME/Library/Application Support/Gstore/client_secret.json"
# or point at it explicitly:
export GSTORE_CLIENT_SECRETS=/path/to/client_secret.json
```

Then launch `gstore` and click **Add Google account** for each account. The app
opens your browser, you consent, and the account appears in the list. Repeat for
all accounts you want to pool. Tokens are encrypted at rest; the encryption key
lives in your OS keychain.
