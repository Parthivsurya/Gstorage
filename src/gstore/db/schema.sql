-- Gstore local index schema.
-- The SQLite DB is the source of truth for the unified UI; each row maps a
-- virtual entry to where it physically lives across the linked Google accounts.

PRAGMA foreign_keys = ON;

-- Schema versioning so migrations stay ordered as phases land.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per linked Google account.
CREATE TABLE IF NOT EXISTS accounts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    email              TEXT NOT NULL UNIQUE,
    -- Identifier of the keychain-held encryption key used for token_blob
    -- (supports future key rotation). Populated in Phase 1.
    token_ref          TEXT,
    -- Google credentials (incl. refresh token) serialized then Fernet-encrypted
    -- with the master key held in the OS keychain. Encrypted at rest. Phase 1.
    token_blob         BLOB,
    -- Last storageQuota snapshot (bytes), refreshed via about.get. Phase 2.
    quota_limit_bytes  INTEGER,
    quota_used_bytes   INTEGER,
    quota_synced_at    TEXT,
    -- 'active' or 'revoked'. Set to 'revoked' when a token refresh fails so
    -- the UI can flag it and its files; re-linking resets it to 'active'.
    -- The row (and its files) is kept on revoke so re-link restores access.
    status             TEXT NOT NULL DEFAULT 'active',
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Self-referential folder tree. parent_id NULL == virtual root.
CREATE TABLE IF NOT EXISTS folders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    parent_id  INTEGER REFERENCES folders(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_id);

-- One row per virtual file. A whole file lives on exactly one "home" account
-- in v1; chunking (Phase 6) uses the chunks table instead.
CREATE TABLE IF NOT EXISTS files (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    parent_id         INTEGER REFERENCES folders(id) ON DELETE CASCADE,
    size_bytes        INTEGER NOT NULL DEFAULT 0,
    home_account_id   INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    -- Real Drive file id on the home account (NULL until upload succeeds).
    drive_file_id     TEXT,
    -- MD5 of the content, verified against Drive's md5Checksum at upload time.
    -- Lets reconcile detect silent corruption and download confirm integrity.
    md5               TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    modified_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_files_parent ON files(parent_id);
CREATE INDEX IF NOT EXISTS idx_files_home ON files(home_account_id);

-- In-flight resumable upload sessions, so an interrupted upload resumes from
-- where it stopped instead of restarting the whole transfer. Keyed to a local
-- file's identity (path + size + mtime); any change invalidates the session.
-- A row is written once the session URI is known and dropped on completion.
CREATE TABLE IF NOT EXISTS uploads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    local_path    TEXT NOT NULL UNIQUE,
    size_bytes    INTEGER NOT NULL,
    mtime_ns      INTEGER NOT NULL,
    resumable_uri TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Chunks already uploaded for an in-flight chunked upload, so an interrupted
-- upload resumes from the first missing chunk instead of restarting. Keyed to
-- the local file's identity (path + size + mtime); rows are cleared once the
-- upload completes (chunks then live in `chunks`). Orphan cleanup in reconcile
-- treats these drive files as protected while pending.
CREATE TABLE IF NOT EXISTS pending_chunks (
    local_path    TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    mtime_ns      INTEGER NOT NULL,
    sequence      INTEGER NOT NULL,
    account_id    INTEGER,
    drive_file_id TEXT NOT NULL,
    md5           TEXT NOT NULL,
    chunk_size    INTEGER NOT NULL DEFAULT 0,
    -- Whole-file MD5, cached on the first attempt so a resume does not have to
    -- re-read the entire file just to recompute a digest it already knows.
    file_md5      TEXT,
    PRIMARY KEY (local_path, sequence)
);

-- Pieces of a chunked file (Phase B), distributed across accounts when a file
-- is too big for any single account's free space. A file is "chunked" when it
-- has rows here; such files carry NULL home_account_id/drive_file_id, and the
-- whole-file checksum lives in files.md5 for end-to-end verification.
CREATE TABLE IF NOT EXISTS chunks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    sequence      INTEGER NOT NULL,
    account_id    INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    drive_file_id TEXT,
    md5           TEXT,
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(file_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);

-- Chunks of a chunked download already written into the output file.
--
-- Parallel workers complete out of order, so the output file's length no
-- longer says how far a download got — it is preallocated to full size from
-- the start and filled in at offsets. This table is the resume state instead.
-- Keyed to the destination path plus the file's identity, so pointing a new
-- download at the same path starts clean.
CREATE TABLE IF NOT EXISTS download_progress (
    dest_path   TEXT NOT NULL,
    file_id     INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,
    sequence    INTEGER NOT NULL,
    size_bytes  INTEGER NOT NULL,
    PRIMARY KEY (dest_path, sequence)
);

CREATE INDEX IF NOT EXISTS idx_download_progress_dest
    ON download_progress(dest_path, file_id, total_bytes);

-- One row per file that currently has a public link. Keyed by file so a file
-- has exactly one current link; re-sharing replaces the row rather than
-- leaving a second manifest nobody can withdraw.
--
-- `link` holds the key that decrypts the manifest, in the clear. That is a
-- deliberate trade: without it "copy the link again" would have to re-share,
-- minting a new key and orphaning the old manifest on Drive. What it exposes
-- is access to files the user has already chosen to make public, and it is
-- recorded in the privacy policy. Account tokens remain encrypted.
CREATE TABLE IF NOT EXISTS shares (
    file_id             INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    manifest_file_id    TEXT NOT NULL,
    manifest_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    link                TEXT NOT NULL,
    part_count          INTEGER NOT NULL DEFAULT 0,
    shared_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
