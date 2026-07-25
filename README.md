# Gstore

A local-only desktop app that pools several Google accounts (e.g. 3 × 15 GB)
into one unified virtual drive. File data flows directly between your machine
and Google — never through any server.

See [drive-aggregator-implementation-plan.md](drive-aggregator-implementation-plan.md)
for the full plan.

## Status

**Phase 0 — Setup: complete.** App scaffold renders a window; local SQLite
index is created in the OS app-data directory.

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
5. Download the client JSON; it will be wired into the login flow in Phase 1.
   Requested scope: `https://www.googleapis.com/auth/drive.file`.
