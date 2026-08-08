"""Fans a live Drive listing across every linked account, off the UI thread.

Merges each account's files into one combined result for the "Scan Drive now"
view. Per-account failures (revoked token, network) are collected rather than
aborting the whole scan, and accounts still on the narrow `drive.file` scope are
flagged so the UI can prompt a re-link for full visibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QThread, Signal

from gstore.auth.accounts import AccountStore
from gstore.db import Database
from gstore.drive.browse import DriveFile, can_modify, has_full_read, list_all_files


@dataclass(frozen=True)
class ScanRow:
    account_id: int
    account_email: str
    file: DriveFile


@dataclass
class ScanResult:
    rows: list[ScanRow] = field(default_factory=list)
    limited_accounts: list[str] = field(default_factory=list)  # only drive.file
    failures: list[tuple[str, str]] = field(default_factory=list)  # (email, msg)
    # Account ids whose token grants the full `drive` scope, so their scanned
    # files can be trashed. Read-only / drive.file accounts are excluded.
    writable_account_ids: set[int] = field(default_factory=set)


class DriveScanWorker(QThread):
    finished_scan = Signal(object)  # ScanResult

    def run(self) -> None:
        db = Database().open()
        accounts = AccountStore(db)
        result = ScanResult()
        try:
            for account in accounts.list_accounts():
                try:
                    credentials = accounts.get_credentials(account.id)
                    if not has_full_read(credentials):
                        result.limited_accounts.append(account.email)
                    if can_modify(credentials):
                        result.writable_account_ids.add(account.id)
                    files = list_all_files(credentials)
                except Exception as exc:
                    result.failures.append((account.email, str(exc)))
                    continue
                for drive_file in files:
                    if drive_file.is_folder:
                        continue  # a flat "what files exist" view, not a tree
                    result.rows.append(
                        ScanRow(account.id, account.email, drive_file)
                    )
            self.finished_scan.emit(result)
        finally:
            db.close()
