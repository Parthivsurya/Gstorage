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
