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
- **Phase 2 — Combined storage view: complete.** Dashboard aggregates each
  account's Drive `about.get` storageQuota into one pooled total (free / used /
  limit) with per-account usage bars; snapshots cached in the index and
  refreshed on a worker thread.
- **Phase 3 — Metadata index + unified listing: complete.** Tabbed UI (Files /
  Storage). The Files tab renders one virtual folder tree from the index with
  breadcrumb navigation; create/rename/delete folders, and files display with
  size and their home account.
- **Phase 4 — Upload with smart placement: complete.** Upload a file → it picks
  the account with the most free space that fits → streams straight to that
  account's Drive via a resumable upload (progress bar) → records it in the
  index so it appears in the browser. Every Drive call is wrapped in
  exponential-backoff retry for rate-limit / server errors.
- **Phase 5 — Download, delete & hardening: complete. End of v1.** Download a
  file back to disk from its home account; delete files/folders from both Drive
  and the index (folder deletes span accounts, with partial-failure handling);
  revoked/expired accounts surface a clear re-link message; and **Repair /
  re-sync** reconciles the index with each Drive — pruning files deleted
  elsewhere and recovering app files missing from the index.

**v1 is feature-complete (Phases 0–5): a usable combined Drive.** Phase 6
(chunking for single files larger than one account) and Phase 7 (FUSE mount)
are additive.

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
