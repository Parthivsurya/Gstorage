# Gstore

A local-only desktop app that pools several Google accounts (e.g. 3 × 15 GB)
into one unified virtual drive. File data flows directly between your machine
and Google — never through any server.

See [docs/implementation-plan.md](docs/implementation-plan.md) for the
original plan and the reasoning behind the early decisions.

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

## Adding accounts

Launch `gstore` and click **Add Google account** for each account. The app opens
your browser, you consent with "Sign in with Google", and the account appears in
the list. Repeat for all accounts you want to pool. Tokens are encrypted at rest;
the encryption key lives in your OS keychain.

End users do **not** need a Google Cloud project — the app ships its own OAuth
Desktop client, so there is no console setup and no client JSON to download.

## Bundling the OAuth client (maintainers only)

The shipped client lives at `src/gstore/auth/client_config.json`. It is a Desktop
(PKCE) client, which has no confidential secret to protect, but the file is
**gitignored** so real credentials are not committed — inject it at build time:

```bash
# copy your Desktop-app client JSON into the package before packaging:
cp /path/to/client_secret.json src/gstore/auth/client_config.json
```

To point Gstore at a different Google Cloud project (development, or your own
client), override the bundled client with either:

```bash
export GSTORE_CLIENT_SECRETS=/path/to/client_secret.json
# or drop it at the app-data path:
cp /path/to/client_secret.json \
   "$HOME/Library/Application Support/Gstore/client_secret.json"
```

Resolution order: `GSTORE_CLIENT_SECRETS` → app-data `client_secret.json` →
bundled client. Requested scopes: `.../auth/drive.file` (manage Gstore's own
files) and `.../auth/drive` (full access, powering the live **Scan Drive now**
combined view plus download/trash of a selected file). The full `drive` scope is
**restricted**, so a production build needs Google's OAuth verification + CASA
security assessment, and every account must re-consent — accounts linked before
this change keep their older scopes: they still show only their Gstore files in
the scan and cannot delete non-Gstore files until re-linked (Storage tab →
re-link). Scan-deletes move files to Trash (recoverable), not a permanent erase.

### Google Cloud project setup (maintainers only)

To create or rotate the bundled client:

1. Create a project at <https://console.cloud.google.com> and enable the
   **Google Drive API**.
2. Configure the **OAuth consent screen** (External). Publish to **In
   production** so refresh tokens don't expire and there's no test-user cap.
3. Create an **OAuth client ID** of type **Desktop app** and download its JSON.
