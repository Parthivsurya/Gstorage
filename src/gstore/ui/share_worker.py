"""Granting and withdrawing a public link, off the UI thread.

Sharing a 20 GB file is 409 permission changes plus a manifest upload, so it
is seconds of work at best and cannot run on the UI thread. Both workers open
their own Database: a SQLite connection belongs to the thread that made it,
and reaching back into the main thread's one raises ProgrammingError.

Recording the share is part of the job, not the caller's follow-up. If the
permissions are granted and the row is never written, the manifest is live on
Drive with nothing left that knows its id, and the share can never be
withdrawn.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from google.auth.exceptions import RefreshError

from gstore.applog import get as get_logger
from gstore.auth.accounts import AccountStore
from gstore.db import Database
from gstore.index import IndexStore
from gstore.index.shares import Shares
from gstore.share.publish import Cancelled, NotShareable, share_file, unshare_file

log = get_logger(__name__)


class _SharingThread(QThread):
    # done, total — piece counts, which stay small; byte counts would need qint64.
    progress = Signal(int, int)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, file_id: int, parent=None) -> None:
        super().__init__(parent)
        self.file_id = file_id
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def should_stop(self) -> bool:
        return self._stop


class ShareWorker(_SharingThread):
    """Make every piece of a file public and produce a link."""

    # link, manifest id, manifest account id, piece count
    succeeded = Signal(str, str, int, int)

    def run(self) -> None:
        db = Database().open()
        try:
            index = IndexStore(db)
            entry = index.get_file(self.file_id)
            if entry is None:
                self.failed.emit("This file is no longer in the index.")
                return

            accounts = AccountStore(db)
            try:
                result = share_file(
                    index,
                    entry,
                    accounts.get_credentials,
                    should_stop=self.should_stop,
                    on_progress=lambda done, total: self.progress.emit(done, total),
                )
            except Cancelled:
                # Pieces already made public stay public: the row below is
                # never written, so nothing records them. Withdraw by sharing
                # again and then stopping the share, which walks every piece.
                self.cancelled.emit()
                return
            except NotShareable as exc:
                self.failed.emit(str(exc))
                return
            except RefreshError:
                self.failed.emit(
                    "An account's access has expired. Re-link it on the "
                    "Storage tab and try again."
                )
                return

            Shares(db).record(
                self.file_id,
                manifest_file_id=result.manifest_file_id,
                manifest_account_id=result.manifest_account_id,
                link=result.link,
                part_count=result.part_count,
            )
            self.succeeded.emit(
                result.link,
                result.manifest_file_id,
                result.manifest_account_id,
                result.part_count,
            )
        except Exception as exc:  # noqa: BLE001 - a worker must not kill the app
            log.exception("sharing file %s failed", self.file_id)
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            db.close()


class UnshareWorker(_SharingThread):
    """Withdraw a link: delete the manifest, then unshare every piece."""

    succeeded = Signal()

    def run(self) -> None:
        db = Database().open()
        try:
            index = IndexStore(db)
            shares = Shares(db)
            entry = index.get_file(self.file_id)
            record = shares.get(self.file_id)

            if entry is None:
                # The file is gone from the index but the share may not be;
                # drop the row so it stops being offered.
                shares.clear(self.file_id)
                self.succeeded.emit()
                return

            accounts = AccountStore(db)
            try:
                unshare_file(
                    index,
                    entry,
                    accounts.get_credentials,
                    manifest_file_id=record.manifest_file_id if record else None,
                    manifest_account_id=record.manifest_account_id if record else None,
                    should_stop=self.should_stop,
                    on_progress=lambda done, total: self.progress.emit(done, total),
                )
            except Cancelled:
                # Stopping halfway leaves some pieces public, so the record
                # stays: it is the only thing that can finish the job later.
                self.cancelled.emit()
                return
            except RefreshError:
                self.failed.emit(
                    "An account's access has expired, so its pieces are still "
                    "shared. Re-link it on the Storage tab and stop sharing again."
                )
                return

            shares.clear(self.file_id)
            self.succeeded.emit()
        except Exception as exc:  # noqa: BLE001
            log.exception("withdrawing the share for %s failed", self.file_id)
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            db.close()
