"""Placement: which account(s) a file should live on.

Whole-file first (most-free-first among accounts with room) so one lost or
locked account can't corrupt files elsewhere. When a file fits no single
account, :func:`plan_chunk_placement` spreads it across several as fixed-size
chunks (Phase B).
"""

from __future__ import annotations

from dataclasses import dataclass

from gstore.auth.accounts import Account

# Fixed chunk size for splitting oversized files across accounts.
CHUNK_SIZE = 50 * 1024 * 1024  # 50 MB


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


@dataclass(frozen=True)
class ChunkPlan:
    """One chunk's placement: byte range in the local file + target account."""

    sequence: int
    offset: int
    length: int
    account_id: int


def chunk_ranges(size_bytes: int, chunk_size: int = CHUNK_SIZE) -> list[tuple[int, int, int]]:
    """Canonical (sequence, offset, length) split of a file — order-stable."""
    ranges: list[tuple[int, int, int]] = []
    offset = 0
    sequence = 0
    while offset < size_bytes:
        length = min(chunk_size, size_bytes - offset)
        ranges.append((sequence, offset, length))
        offset += length
        sequence += 1
    return ranges


def _assign(
    ranges: list[tuple[int, int, int]], accounts: list[Account]
) -> list[ChunkPlan] | None:
    """Greedy most-free-first assignment of chunk ranges to accounts, or None."""
    if not ranges:
        return []
    synced = [a for a in accounts if a.quota_synced_at is not None]
    if not synced:
        return None

    # Remaining capacity per account; unlimited accounts are effectively boundless.
    remaining: dict[int, float] = {}
    for account in synced:
        fb = free_bytes(account)
        remaining[account.id] = float("inf") if fb is None else float(fb)

    plans: list[ChunkPlan] = []
    for sequence, offset, length in ranges:
        candidate = max(
            (a for a in synced if remaining[a.id] >= length),
            key=lambda a: remaining[a.id],
            default=None,
        )
        if candidate is None:
            return None  # nowhere left with room for this chunk
        plans.append(ChunkPlan(sequence, offset, length, candidate.id))
        remaining[candidate.id] -= length
    return plans


def plan_chunk_placement(
    accounts: list[Account], size_bytes: int, chunk_size: int = CHUNK_SIZE
) -> list[ChunkPlan] | None:
    """Distribute a whole `size_bytes` file across accounts as chunks, or None."""
    return _assign(chunk_ranges(size_bytes, chunk_size), accounts)


def plan_missing_chunks(
    accounts: list[Account],
    size_bytes: int,
    completed_sequences: set[int],
    chunk_size: int = CHUNK_SIZE,
) -> list[ChunkPlan] | None:
    """Place only the chunks not already uploaded (for resume), or None.

    Returns [] when every chunk is already done. Planning only the missing
    ranges keeps placement honest: the completed chunks' space is already
    consumed and reflected in refreshed quotas.
    """
    missing = [r for r in chunk_ranges(size_bytes, chunk_size) if r[0] not in completed_sequences]
    return _assign(missing, accounts)
