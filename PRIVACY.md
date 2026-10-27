# Gstore Privacy Policy

_Last updated: 28 October 2026 (Gstore 0.1.0)_

Gstore is a local-only desktop application that pools several of your own Google
accounts into one unified virtual drive. This policy explains what data Gstore
touches and where it goes.

## The short version

Gstore runs entirely on your device. Your file data flows **directly between
your machine and Google** and never passes through, nor is stored on, any server
operated by the developer. The developer has no backend and never receives your
files, your account contents, or your credentials.

## What Gstore accesses

When you click **Sign in with Google**, Gstore requests this Google Drive scope:

- `https://www.googleapis.com/auth/drive.file` — lets Gstore create, upload,
  download, and delete **its own files** across your accounts. It does not
  grant access to anything else in your Drive.

That is the only scope requested by default, and it is all the pooled storage,
uploads and downloads need.

### The optional full-Drive scope

- `https://www.googleapis.com/auth/drive` (full Drive access) — powers the
  local, combined **Scan Drive now** listing, which shows every file across
  your accounts including files Gstore did not create, lets you download any
  file you select, and lets you **move a selected file to Trash** on your
  request.

This scope is **opt-in and off by default.** Gstore asks for it only if you
enable it yourself, either with the `GSTORE_FULL_DRIVE` environment variable or
by creating a file named `full_drive.enabled` in Gstore's application-data
directory. Enabling it changes only the accounts you link **afterwards**: an
account already linked keeps whatever it granted until you re-link it.

Even with it enabled, Gstore only ever acts on files you explicitly pick, and a
delete from a scan moves the file to Google Drive's Trash (recoverable for
about 30 days), never a permanent erase.

All access is exercised locally from your own machine; nothing is sent to any
server operated by the developer. Gstore also reads each account's storage quota
(total / used / free space) for the combined storage view, and its email address
to label the account in the app.

## What Gstore stores, and where

Everything Gstore stores lives locally on your own computer, in your operating
system's application-data directory:

| | |
| --- | --- |
| macOS | `~/Library/Application Support/Gstore/` |
| Linux | `~/.local/share/Gstore/` |
| Windows | `%LOCALAPPDATA%\Gstore\` |

- **A local index** (SQLite database) of your folder structure and which account
  each file lives on. This is metadata only — it does not contain your file
  contents.
- **OAuth tokens** for each linked account, **encrypted at rest**. The
  encryption key is held in your operating system's keychain/credential store,
  never in plaintext on disk.
- **Transfer resume state**, so an interrupted upload or download can continue
  instead of starting over. This records the **full path on your computer** of
  the file being uploaded or the destination being downloaded to, along with its
  size, its modification time, and which pieces have already transferred. It is
  cleared when the transfer finishes.
- **A log file** (`gstore.log`, rotated at 2 MB and keeping three older copies).
  It records what the app did — transfers started and finished, chunk numbers,
  retries, and the text of any error — which includes **file names**. It does
  not contain file contents, tokens, or passwords. It is also echoed to the
  terminal if you launch Gstore from one. You can delete it at any time.

None of this is transmitted to the developer or to any third party.

### Files Gstore writes outside that directory

- **Partial downloads.** While a download is running, Gstore builds the file in
  a `.gstore-part` file **next to the destination you chose**, not in the
  application-data directory. It is renamed into place once the whole-file
  checksum passes, and removed if you cancel and then complete the download
  another way. If a download is abandoned, this file can remain on disk.
- **Index backups you create.** **Back up index…** on the Storage tab writes a
  copy of the index to a location you pick. That copy is the whole database,
  which includes the **encrypted** OAuth tokens. Because the encryption key
  stays in your operating system's keychain and is never written into the file,
  a backup moved to another machine cannot be used to access your accounts —
  but treat it as sensitive and store it somewhere you control.

## What Gstore does NOT do

- It does not run any server, analytics, telemetry, or tracking.
- It does not sell, share, or transmit your data to anyone.
- It does not read, list, or touch files in your Drive that it did not create —
  unless you have explicitly enabled the optional full-Drive scope described
  above, and then only for files you select.

## Update checks

Gstore is distributed as a direct download rather than through an app store,
so it asks GitHub once at startup whether a newer version has been released.

That request is a plain, unauthenticated `GET` to GitHub's public releases
endpoint for this project. It carries **no account information, no file names,
no email address, no tokens and no identifier of any kind** — only the IP
address that every HTTP request necessarily reveals, in this case to GitHub.
Nothing is downloaded or installed automatically; if a newer version exists
the app shows a notice, and the buttons on it only open your browser.

This is the only request Gstore makes to anywhere other than Google. You can
switch it off — set the `GSTORE_NO_UPDATE_CHECK` environment variable, or
create a file named `update_check.disabled` in Gstore's application-data
directory — and the app will then contact nothing but Google.

## Third-party services

Apart from the update check described above, Gstore communicates only with
**Google's APIs**, on your behalf, using the account(s) you link. Your use of those accounts is governed by
[Google's Privacy Policy](https://policies.google.com/privacy) and the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy).
Gstore's use of information received from Google APIs adheres to that policy,
including its Limited Use requirements.

## Removing access, and removing everything

You can revoke Gstore's access to any account at any time from your Google
Account's [Third-party access settings](https://myaccount.google.com/permissions).
You can also remove an account inside Gstore, which deletes its stored
(encrypted) tokens from your device.

To remove everything Gstore has written, delete:

1. the application-data directory listed above — index, tokens, resume state
   and log;
2. any leftover `.gstore-part` files beside download destinations you chose;
3. any index backups you exported yourself.

Removing those leaves nothing of Gstore's on your machine. Files already
uploaded stay in your Google accounts, where you can manage them with Drive
directly.

## Contact

Questions about this policy: **parthivsuryakb@gmail.com**
