"""Persistence for linked Google accounts.

Credentials are serialized, Fernet-encrypted (see :mod:`gstore.auth.keystore`),
and stored in the local SQLite index. On load they are refreshed if expired and
any rotated token is written back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from gstore.auth import keystore
from gstore.auth.oauth import SCOPES
from gstore.db import Database


@dataclass(frozen=True)
class Account:
    id: int
    email: str
    quota_limit_bytes: int | None
    quota_used_bytes: int | None
    quota_synced_at: str | None
    created_at: str


class AccountStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add_account(self, credentials: Credentials, email: str) -> int:
        """Store (or re-link) an account by email; returns its row id."""
        blob = keystore.encrypt(credentials.to_json().encode("utf-8"))
        self.db.conn.execute(
            "INSERT INTO accounts(email, token_ref, token_blob) VALUES(?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "token_ref = excluded.token_ref, token_blob = excluded.token_blob",
            (email, keystore.MASTER_KEY_NAME, blob),
        )
        self.db.conn.commit()
        row = self.db.conn.execute(
            "SELECT id FROM accounts WHERE email = ?", (email,)
        ).fetchone()
        return int(row["id"])

    def list_accounts(self) -> list[Account]:
        rows = self.db.conn.execute(
            "SELECT id, email, quota_limit_bytes, quota_used_bytes, "
            "quota_synced_at, created_at FROM accounts ORDER BY created_at, id"
        ).fetchall()
        return [
            Account(
                id=r["id"],
                email=r["email"],
                quota_limit_bytes=r["quota_limit_bytes"],
                quota_used_bytes=r["quota_used_bytes"],
                quota_synced_at=r["quota_synced_at"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def count(self) -> int:
        return int(
            self.db.conn.execute("SELECT count(*) c FROM accounts").fetchone()["c"]
        )

    def get_credentials(self, account_id: int) -> Credentials:
        """Load credentials, refreshing (and re-persisting) if expired."""
        row = self.db.conn.execute(
            "SELECT token_blob FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"No account with id {account_id}")
        if row["token_blob"] is None:
            raise ValueError(f"Account {account_id} has no stored credentials")

        info = json.loads(keystore.decrypt(row["token_blob"]).decode("utf-8"))
        credentials = Credentials.from_authorized_user_info(info, scopes=SCOPES)

        if not credentials.valid:
            credentials.refresh(Request())
            self._persist_credentials(account_id, credentials)

        return credentials

    def remove_account(self, account_id: int) -> None:
        self.db.conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        self.db.conn.commit()

    def _persist_credentials(self, account_id: int, credentials: Credentials) -> None:
        blob = keystore.encrypt(credentials.to_json().encode("utf-8"))
        self.db.conn.execute(
            "UPDATE accounts SET token_blob = ? WHERE id = ?", (blob, account_id)
        )
        self.db.conn.commit()
