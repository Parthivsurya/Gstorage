"""Storage-quota reads and aggregation.

`about.get` returns the account's total limit and usage in bytes. Usage spans
all Google services (Drive, Gmail, Photos), so `free = limit - usage` is the
real headroom available for new uploads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


@dataclass(frozen=True)
class StorageQuota:
    # None limit == unlimited storage (e.g. some Workspace plans).
    limit_bytes: int | None
    used_bytes: int

    @property
    def free_bytes(self) -> int | None:
        if self.limit_bytes is None:
            return None
        return max(0, self.limit_bytes - self.used_bytes)


@dataclass(frozen=True)
class CombinedStorage:
    total_limit: int | None  # None if any synced account is unlimited
    total_used: int
    total_free: int | None
    synced_count: int


def fetch_storage_quota(credentials: Credentials) -> StorageQuota:
    """Read one account's storage quota via Drive about.get."""
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    about = service.about().get(fields="storageQuota").execute()
    quota = about.get("storageQuota", {})
    limit = int(quota["limit"]) if quota.get("limit") is not None else None
    used = int(quota.get("usage", 0))
    return StorageQuota(limit_bytes=limit, used_bytes=used)


def aggregate(accounts: Iterable["AccountLike"]) -> CombinedStorage:
    """Sum stored quota snapshots into one pooled view.

    Only accounts that have been synced at least once (quota_synced_at set) are
    counted, so the total reflects real data rather than empty placeholders.
    """
    total_limit = 0
    total_used = 0
    unlimited = False
    synced = 0
    for account in accounts:
        if account.quota_synced_at is None:
            continue
        synced += 1
        total_used += account.quota_used_bytes or 0
        if account.quota_limit_bytes is None:
            unlimited = True
        else:
            total_limit += account.quota_limit_bytes

    return CombinedStorage(
        total_limit=None if unlimited else total_limit,
        total_used=total_used,
        total_free=None if unlimited else max(0, total_limit - total_used),
        synced_count=synced,
    )


class AccountLike:
    """Structural hint for :func:`aggregate` (see gstore.auth.accounts.Account)."""

    quota_limit_bytes: int | None
    quota_used_bytes: int | None
    quota_synced_at: str | None
