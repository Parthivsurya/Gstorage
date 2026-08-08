"""Repository over the folders/files tables.

The index is the single source of truth for the unified UI: one virtual tree
whose leaves may physically live on any linked account. `parent_id IS NULL`
denotes the virtual root. Deleting a folder cascades to its subtree via the
schema's foreign keys (index rows only — real Drive deletion is Phase 5).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Folder:
    id: int
    name: str
    parent_id: int | None
    created_at: str


@dataclass(frozen=True)
class FileEntry:
    id: int
    name: str
    parent_id: int | None
    size_bytes: int
    home_account_id: int | None
    home_account_email: str | None
    home_account_status: str | None
    drive_file_id: str | None
    md5: str | None
    chunk_count: int
    chunk_unavailable: int
    created_at: str
    modified_at: str

    @property
    def is_chunked(self) -> bool:
        """True if the file is split into chunks across accounts (Phase B)."""
        return self.chunk_count > 0

    @property
    def is_available(self) -> bool:
        """True if the file's bytes can currently be reached.

        Chunked files need every chunk's account present and not revoked. Whole
        files need their single home account present and not revoked. Unavailable
        files can't be downloaded until the relevant account(s) are re-linked.
        """
        if self.is_chunked:
            return self.chunk_unavailable == 0
        return self.home_account_id is not None and self.home_account_status != "revoked"


@dataclass(frozen=True)
class Chunk:
    id: int
    file_id: int
    sequence: int
    account_id: int | None
    account_email: str | None
    account_status: str | None
    drive_file_id: str | None
    md5: str | None
    size_bytes: int

    @property
    def is_available(self) -> bool:
        return self.account_id is not None and self.account_status != "revoked"


def _parent_clause(parent_id: int | None) -> tuple[str, tuple]:
    """SQL fragment + params for matching a (possibly NULL) parent."""
    if parent_id is None:
        return "parent_id IS NULL", ()
    return "parent_id = ?", (parent_id,)


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so a user's search term is matched literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Shared projection for a FileEntry: joins the home account (for email/status)
# and computes chunk counts so chunked/unavailable files render correctly.
# Callers append a WHERE/ORDER clause. Kept in one place so the four file
# queries below stay in lockstep as columns evolve.
_FILE_SELECT = (
    "SELECT f.id, f.name, f.parent_id, f.size_bytes, f.home_account_id, "
    "a.email AS home_account_email, a.status AS home_account_status, "
    "f.drive_file_id, f.md5, "
    "(SELECT COUNT(*) FROM chunks c WHERE c.file_id = f.id) AS chunk_count, "
    "(SELECT COUNT(*) FROM chunks c LEFT JOIN accounts a2 ON a2.id = c.account_id "
    " WHERE c.file_id = f.id AND (c.account_id IS NULL OR a2.status = 'revoked')) "
    "AS chunk_unavailable, "
    "f.created_at, f.modified_at FROM files f "
    "LEFT JOIN accounts a ON a.id = f.home_account_id "
)


class IndexStore:
    def __init__(self, db) -> None:
        self.db = db

    # -- folders --------------------------------------------------------------

    def create_folder(self, name: str, parent_id: int | None = None) -> int:
        cur = self.db.conn.execute(
            "INSERT INTO folders(name, parent_id) VALUES(?, ?)", (name, parent_id)
        )
        self.db.conn.commit()
        return int(cur.lastrowid)

    def get_folder(self, folder_id: int) -> Folder | None:
        row = self.db.conn.execute(
            "SELECT id, name, parent_id, created_at FROM folders WHERE id = ?",
            (folder_id,),
        ).fetchone()
        return self._folder(row) if row else None

    def list_folders(self, parent_id: int | None) -> list[Folder]:
        clause, params = _parent_clause(parent_id)
        rows = self.db.conn.execute(
            f"SELECT id, name, parent_id, created_at FROM folders "
            f"WHERE {clause} ORDER BY name COLLATE NOCASE",
            params,
        ).fetchall()
        return [self._folder(r) for r in rows]

    def get_or_create_folder(self, name: str, parent_id: int | None = None) -> int:
        clause, params = _parent_clause(parent_id)
        row = self.db.conn.execute(
            f"SELECT id FROM folders WHERE name = ? AND {clause}",
            (name, *params),
        ).fetchone()
        if row is not None:
            return int(row["id"])
        return self.create_folder(name, parent_id)

    def rename_folder(self, folder_id: int, name: str) -> None:
        self.db.conn.execute(
            "UPDATE folders SET name = ? WHERE id = ?", (name, folder_id)
        )
        self.db.conn.commit()

    def delete_folder(self, folder_id: int) -> None:
        self.db.conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
        self.db.conn.commit()

    def folder_path(self, folder_id: int | None) -> list[Folder]:
        """Root-to-folder chain for breadcrumbs ([] at the virtual root)."""
        chain: list[Folder] = []
        current = folder_id
        while current is not None:
            folder = self.get_folder(current)
            if folder is None:
                break
            chain.append(folder)
            current = folder.parent_id
        chain.reverse()
        return chain

    # -- files ----------------------------------------------------------------

    def list_files(self, parent_id: int | None) -> list[FileEntry]:
        clause, params = _parent_clause(parent_id)
        rows = self.db.conn.execute(
            _FILE_SELECT + f"WHERE f.{clause} ORDER BY f.name COLLATE NOCASE",
            params,
        ).fetchall()
        return [self._file(r) for r in rows]

    def search_files(self, query: str) -> list[FileEntry]:
        """Every file whose name contains `query`, across the whole tree.

        A flat, account-spanning view: results ignore folder boundaries so the
        same term surfaces matches wherever they physically live. Case-insensitive
        substring match; LIKE wildcards in the term are matched literally.
        """
        query = query.strip()
        if not query:
            return []
        like = f"%{_escape_like(query)}%"
        rows = self.db.conn.execute(
            _FILE_SELECT
            + "WHERE f.name LIKE ? ESCAPE '\\' ORDER BY f.name COLLATE NOCASE",
            (like,),
        ).fetchall()
        return [self._file(r) for r in rows]

    def create_file(
        self,
        name: str,
        parent_id: int | None,
        size_bytes: int,
        home_account_id: int | None,
        drive_file_id: str | None,
        md5: str | None = None,
    ) -> int:
        cur = self.db.conn.execute(
            "INSERT INTO files(name, parent_id, size_bytes, home_account_id, "
            "drive_file_id, md5) VALUES(?, ?, ?, ?, ?, ?)",
            (name, parent_id, size_bytes, home_account_id, drive_file_id, md5),
        )
        self.db.conn.commit()
        return int(cur.lastrowid)

    def get_file(self, file_id: int) -> FileEntry | None:
        row = self.db.conn.execute(
            _FILE_SELECT + "WHERE f.id = ?",
            (file_id,),
        ).fetchone()
        return self._file(row) if row else None

    def list_files_for_account(self, account_id: int) -> list[FileEntry]:
        rows = self.db.conn.execute(
            _FILE_SELECT + "WHERE f.home_account_id = ?",
            (account_id,),
        ).fetchall()
        return [self._file(r) for r in rows]

    def count_files_on_account(self, account_id: int) -> int:
        """Files that would become unavailable if this account were removed.

        Covers whole files homed on the account plus chunked files with any
        chunk on it (removing the account orphans those chunks).
        """
        row = self.db.conn.execute(
            "SELECT COUNT(*) AS c FROM ("
            "  SELECT id FROM files WHERE home_account_id = ?"
            "  UNION"
            "  SELECT file_id FROM chunks WHERE account_id = ?"
            ")",
            (account_id, account_id),
        ).fetchone()
        return int(row["c"])

    def find_file_by_drive_id(
        self, account_id: int, drive_file_id: str
    ) -> FileEntry | None:
        row = self.db.conn.execute(
            _FILE_SELECT + "WHERE f.home_account_id = ? AND f.drive_file_id = ?",
            (account_id, drive_file_id),
        ).fetchone()
        return self._file(row) if row else None

    def descendant_files(self, folder_id: int) -> list[FileEntry]:
        """Every file within a folder and all its subfolders.

        Used by folder deletion, since a folder's files may live on different
        accounts and each real Drive file must be removed individually.
        """
        rows = self.db.conn.execute(
            "WITH RECURSIVE sub(id) AS ("
            "  SELECT ? "
            "  UNION ALL "
            "  SELECT f.id FROM folders f JOIN sub s ON f.parent_id = s.id"
            ") "
            + _FILE_SELECT
            + "WHERE f.parent_id IN (SELECT id FROM sub)",
            (folder_id,),
        ).fetchall()
        return [self._file(r) for r in rows]

    def rename_file(self, file_id: int, name: str) -> None:
        self.db.conn.execute(
            "UPDATE files SET name = ?, modified_at = datetime('now') WHERE id = ?",
            (name, file_id),
        )
        self.db.conn.commit()

    def delete_file(self, file_id: int) -> None:
        self.db.conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        self.db.conn.commit()

    # -- chunks ---------------------------------------------------------------

    def add_chunk(
        self,
        file_id: int,
        sequence: int,
        account_id: int,
        drive_file_id: str,
        md5: str,
        size_bytes: int,
    ) -> None:
        self.db.conn.execute(
            "INSERT INTO chunks(file_id, sequence, account_id, drive_file_id, "
            "md5, size_bytes) VALUES(?, ?, ?, ?, ?, ?)",
            (file_id, sequence, account_id, drive_file_id, md5, size_bytes),
        )
        self.db.conn.commit()

    def chunk_drive_ids(self, account_id: int) -> set[str]:
        """Drive file ids of all chunks stored on an account (for reconcile)."""
        rows = self.db.conn.execute(
            "SELECT drive_file_id FROM chunks "
            "WHERE account_id = ? AND drive_file_id IS NOT NULL",
            (account_id,),
        ).fetchall()
        return {r["drive_file_id"] for r in rows}

    def list_chunks(self, file_id: int) -> list[Chunk]:
        """Every chunk of a file, in sequence order, with account health."""
        rows = self.db.conn.execute(
            "SELECT c.id, c.file_id, c.sequence, c.account_id, "
            "a.email AS account_email, a.status AS account_status, "
            "c.drive_file_id, c.md5, c.size_bytes FROM chunks c "
            "LEFT JOIN accounts a ON a.id = c.account_id "
            "WHERE c.file_id = ? ORDER BY c.sequence",
            (file_id,),
        ).fetchall()
        return [
            Chunk(
                id=r["id"],
                file_id=r["file_id"],
                sequence=r["sequence"],
                account_id=r["account_id"],
                account_email=r["account_email"],
                account_status=r["account_status"],
                drive_file_id=r["drive_file_id"],
                md5=r["md5"],
                size_bytes=r["size_bytes"],
            )
            for r in rows
        ]

    # -- row mapping ----------------------------------------------------------

    @staticmethod
    def _folder(row) -> Folder:
        return Folder(
            id=row["id"],
            name=row["name"],
            parent_id=row["parent_id"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _file(row) -> FileEntry:
        return FileEntry(
            id=row["id"],
            name=row["name"],
            parent_id=row["parent_id"],
            size_bytes=row["size_bytes"],
            home_account_id=row["home_account_id"],
            home_account_email=row["home_account_email"],
            home_account_status=row["home_account_status"],
            drive_file_id=row["drive_file_id"],
            md5=row["md5"],
            chunk_count=row["chunk_count"],
            chunk_unavailable=row["chunk_unavailable"],
            created_at=row["created_at"],
            modified_at=row["modified_at"],
        )
