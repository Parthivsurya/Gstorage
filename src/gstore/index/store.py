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
    drive_file_id: str | None
    created_at: str
    modified_at: str


def _parent_clause(parent_id: int | None) -> tuple[str, tuple]:
    """SQL fragment + params for matching a (possibly NULL) parent."""
    if parent_id is None:
        return "parent_id IS NULL", ()
    return "parent_id = ?", (parent_id,)


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
            "SELECT f.id, f.name, f.parent_id, f.size_bytes, f.home_account_id, "
            "a.email AS home_account_email, f.drive_file_id, f.created_at, "
            "f.modified_at FROM files f "
            "LEFT JOIN accounts a ON a.id = f.home_account_id "
            f"WHERE f.{clause} ORDER BY f.name COLLATE NOCASE",
            params,
        ).fetchall()
        return [self._file(r) for r in rows]

    def create_file(
        self,
        name: str,
        parent_id: int | None,
        size_bytes: int,
        home_account_id: int | None,
        drive_file_id: str | None,
    ) -> int:
        cur = self.db.conn.execute(
            "INSERT INTO files(name, parent_id, size_bytes, home_account_id, "
            "drive_file_id) VALUES(?, ?, ?, ?, ?)",
            (name, parent_id, size_bytes, home_account_id, drive_file_id),
        )
        self.db.conn.commit()
        return int(cur.lastrowid)

    def get_file(self, file_id: int) -> FileEntry | None:
        row = self.db.conn.execute(
            "SELECT f.id, f.name, f.parent_id, f.size_bytes, f.home_account_id, "
            "a.email AS home_account_email, f.drive_file_id, f.created_at, "
            "f.modified_at FROM files f "
            "LEFT JOIN accounts a ON a.id = f.home_account_id "
            "WHERE f.id = ?",
            (file_id,),
        ).fetchone()
        return self._file(row) if row else None

    def list_files_for_account(self, account_id: int) -> list[FileEntry]:
        rows = self.db.conn.execute(
            "SELECT f.id, f.name, f.parent_id, f.size_bytes, f.home_account_id, "
            "a.email AS home_account_email, f.drive_file_id, f.created_at, "
            "f.modified_at FROM files f "
            "LEFT JOIN accounts a ON a.id = f.home_account_id "
            "WHERE f.home_account_id = ?",
            (account_id,),
        ).fetchall()
        return [self._file(r) for r in rows]

    def find_file_by_drive_id(
        self, account_id: int, drive_file_id: str
    ) -> FileEntry | None:
        row = self.db.conn.execute(
            "SELECT f.id, f.name, f.parent_id, f.size_bytes, f.home_account_id, "
            "a.email AS home_account_email, f.drive_file_id, f.created_at, "
            "f.modified_at FROM files f "
            "LEFT JOIN accounts a ON a.id = f.home_account_id "
            "WHERE f.home_account_id = ? AND f.drive_file_id = ?",
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
            "SELECT f.id, f.name, f.parent_id, f.size_bytes, f.home_account_id, "
            "a.email AS home_account_email, f.drive_file_id, f.created_at, "
            "f.modified_at FROM files f "
            "LEFT JOIN accounts a ON a.id = f.home_account_id "
            "WHERE f.parent_id IN (SELECT id FROM sub)",
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
            drive_file_id=row["drive_file_id"],
            created_at=row["created_at"],
            modified_at=row["modified_at"],
        )
