"""Whole-file placement (v1).

Picks which account a file should live on. Rule: most-free-first among accounts
that have room. Whole-file placement means one lost or locked account can't
corrupt files that live elsewhere. Chunking across accounts arrives in Phase 6.
"""

from __future__ import annotations

from gstore.auth.accounts import Account


def free_bytes(account: Account) -> int | None:
    """Free space from the last quota snapshot; None == unlimited."""
    if account.quota_limit_bytes is None:
        return None
    return max(0, account.quota_limit_bytes - (account.quota_used_bytes or 0))


def choose_account(accounts: list[Account], size_bytes: int) -> Account | None:
    """Return the best account to hold a `size_bytes` file, or None if none fit.

    Only accounts with a quota snapshot are considered. Unlimited accounts win
    outright; otherwise the account with the most free space that still fits.
    """
    synced = [a for a in accounts if a.quota_synced_at is not None]

    for account in synced:  # unlimited storage fits anything
        if account.quota_limit_bytes is None:
            return account

    fitting = [a for a in synced if (free_bytes(a) or 0) >= size_bytes]
    if not fitting:
        return None
    return max(fitting, key=lambda a: free_bytes(a) or 0)
