# Multi-Account Drive Aggregator — Implementation Plan

A desktop app that pools several Google accounts (e.g. 3 × 15 GB) into one unified ~45 GB virtual drive. Everything runs on the user's machine; file data flows directly between the user and Google, never through a server of yours.

---

## Locked decisions

| Decision | Choice | Why |
|---|---|---|
| Runtime | **Desktop app, Python** | Clean FUSE bindings, first-class Google client, simple parallelism. Reversible: Electron/TS if you later want a polished GUI. |
| Server | **None** | Data flows user → Google directly. Zero hosting cost, lighter security review. |
| Auth | **OAuth "Desktop app" client + PKCE**, loopback on localhost | Standard serverless desktop flow; no client secret to hide. |
| Scope | **`drive.file`** | App only touches files it created — far lighter verification than full `drive` scope. Chunks count as app-created, so this is sufficient even with splitting. |
| Storage model (v1) | **Whole-file placement** | One lost/locked account can't corrupt other files; native Drive preview stays intact. |
| Storage model (v2) | **Chunking across accounts** | Needed only for single files larger than one account's free space. |
| Metadata index | **SQLite**, local | Folder tree via `parent_id` self-reference. B+ tree indexing gives fast path lookups; no graph DB needed. |
| Quota accounting | **In quota *units*, not request counts** | New projects (post-May 2026) use the unit model: list = 100, download = 200, etc. |

---

## Phase 0 — Setup (½ day)

- Scaffold the Python desktop app; confirm a window renders (PySide/PyQt, or a Tauri shell if you want web UI without a server).
- Create a local SQLite DB in the OS app-data directory; confirm read/write.
- Google Cloud Console: one project, enable the Drive API, create an OAuth client of type **Desktop app**, configure the consent screen (testing mode allows ~100 test users — plenty for dev).

**Milestone:** app launches, empty SQLite DB on disk, Google project ready.

---

## Phase 1 — OAuth + multi-account login (2–3 days)

The load-bearing phase. Get this right first.

- Implement the PKCE loopback flow: generate code verifier/challenge → open system browser to Google's consent URL → temporary `localhost` listener catches the redirect → exchange code for access + refresh tokens.
- Make "Add another account" repeat the flow, storing N token sets.
- Encrypt refresh tokens at rest; keep the encryption key in the OS keychain (`keyring`), never plaintext on disk.
- Request scope `https://www.googleapis.com/auth/drive.file`.

**Milestone:** user logs into 3 accounts; app holds and can refresh 3 token sets.

---

## Phase 2 — Combined storage view (2 days)

- For each account, call Drive `about.get` with `storageQuota` to read total / used / free bytes.
- Sum into one view: "45 GB total, 12 GB used, 33 GB free."
- Dashboard shows per-account usage + the combined total.

**Milestone:** dashboard shows real aggregated free space. This is where the idea first *feels* real.

---

## Phase 3 — Metadata index + unified listing (3–4 days)

SQLite schema (starting point):

- `accounts` — id, email, encrypted-token ref, last quota snapshot
- `folders` — id, name, `parent_id` (self-referential tree)
- `files` — virtual id, name, `parent_id`, size, timestamps, **home account id**, real Drive file id
- `chunks` — *reserved for Phase 6* (file id, sequence, account id, real Drive id, hash)

Build the file browser: query the index, render one unified tree regardless of which account each file physically lives on. The index is the source of truth for the UI.

**Milestone:** unified browser renders files across accounts as one drive.

---

## Phase 4 — Upload with smart placement (3–4 days)

The core write path — whole files, no chunking yet.

- Placement rule: pick the account with enough free space (simplest: most-free-first).
- Stream the file **directly from local disk to that account's Drive** via a resumable upload (Google's recommendation for >5 MB). No Flask hop, no full temp copy.
- Record the mapping in the index on success.
- Wrap every API call in a **unit-aware quota tracker** with exponential backoff on `403 User rate limit exceeded` / `429 Too many requests`. Budget in units (list 100, download 200, etc.), not request counts.
- UI: progress bar, clear failure state.

**Milestone:** drop a file in → uploads to best-fit account → appears in the unified view.

---

## Phase 5 — Download, delete & hardening (2–3 days)

- **Download:** look up in index → fetch from correct account → stream to disk.
- **Delete:** remove from Drive + index.
- **Hardening:** token expiry mid-transfer, revoked account, network drops.
- **Repair/re-sync:** reconcile the index against what actually exists in each Drive (in case someone edits a Drive directly outside your app).

**Milestone:** full create/read/delete lifecycle is reliable; index self-heals.

> **End of v1.** Phases 0–5 give a genuinely usable "combined Drive." ~2–3 focused weeks solo.

---

## Phase 6 — Chunking / splitting (v2)

Only once v1 is solid. This unlocks single files bigger than any one account's free space, at the cost of many more failure modes.

- Split large files into fixed-size blocks (e.g. 50 MB).
- Distribute chunks across accounts; parallelize with `asyncio` or `ThreadPoolExecutor`.
- **Hash each chunk** (store hash in `chunks`) and verify on reassembly — now one lost account can corrupt whole files, so integrity checks are mandatory.
- Reassemble in order on download, stream back to disk.
- **Fallback routing:** if an account fills up or its token dies mid-upload, reroute pending chunks to remaining accounts.
- **Orphan cleanup:** background job deletes incomplete chunks from failed uploads so they don't leak storage (and quota).

*(Optional here: content-aware compression before chunking — but skip compressing already-compressed media like video/zip; it burns CPU for near-zero gain.)*

---

## Phase 7 — FUSE mount (optional polish)

Mount the pool as a real local disk (the way rclone does), so the OS file manager treats it like any other drive. Nicest possible UX; Python has clean FUSE bindings. Purely additive once the core works.

---

## Verify against current Google docs before relying on these

These change and are worth confirming, not trusting from memory or this plan:

- **Verification requirements** for `drive.file` once you leave testing mode for public release (sensitive/restricted scopes can trigger a security assessment at scale).
- **Current Drive API quota model** for your project — new projects use quota units; confirm the per-project and per-user unit ceilings before finalizing your backoff math.
- **Terms of Service** on aggregating accounts, especially if this becomes a commercial product rather than personal use.

---

## Critical path

**Phase 1 → 2 → 3 → 4 → 5** = a working product. Everything else (chunking, FUSE, compression) is additive and should wait until that spine is stable.
