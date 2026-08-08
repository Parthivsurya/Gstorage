# Gstore Privacy Policy

_Last updated: 31 July 2026_

Gstore is a local-only desktop application that pools several of your own Google
accounts into one unified virtual drive. This policy explains what data Gstore
touches and where it goes.

## The short version

Gstore runs entirely on your device. Your file data flows **directly between
your machine and Google** and never passes through, nor is stored on, any server
operated by the developer. The developer has no backend and never receives your
files, your account contents, or your credentials.

## What Gstore accesses

When you click **Sign in with Google**, Gstore requests these Google Drive
scopes:

- `https://www.googleapis.com/auth/drive.file` — lets Gstore create, upload,
  download, and delete its own files across your accounts.
- `https://www.googleapis.com/auth/drive` (full Drive access) — powers the local,
  combined **Scan Drive now** listing (seeing every file across your accounts),
  lets you download any file you select, and lets you **move a selected file to
  Trash** on your request. Gstore only ever acts on files you explicitly pick;
  scan-deletes move files to Google Drive's Trash (recoverable ~30 days), never a
  permanent erase.

All access is exercised locally from your own machine; nothing is sent to any
server operated by the developer. Gstore also reads each account's storage quota
(total / used / free space) for the combined storage view, and its email address
to label the account in the app.

## What Gstore stores, and where

Everything Gstore stores lives locally on your own computer, in your operating
system's application-data directory (for example, on macOS:
`~/Library/Application Support/Gstore`):

- **A local index** (SQLite database) of your folder structure and which account
  each file lives on. This is metadata only — it does not contain your file
  contents.
- **OAuth tokens** for each linked account, **encrypted at rest**. The
  encryption key is held in your operating system's keychain/credential store,
  never in plaintext on disk.

None of this is transmitted to the developer or to any third party. Uninstalling
the app and deleting its application-data directory removes it all.

## What Gstore does NOT do

- It does not run any server, analytics, telemetry, or tracking.
- It does not sell, share, or transmit your data to anyone.
- It does not access files in your Drive that it did not create.

## Third-party services

Gstore communicates only with **Google's APIs**, on your behalf, using the
account(s) you link. Your use of those accounts is governed by
[Google's Privacy Policy](https://policies.google.com/privacy) and the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy).
Gstore's use of information received from Google APIs adheres to that policy,
including its Limited Use requirements.

## Removing access

You can revoke Gstore's access to any account at any time from your Google
Account's [Third-party access settings](https://myaccount.google.com/permissions).
You can also remove an account inside Gstore, which deletes its stored (encrypted)
tokens from your device.

## Contact

Questions about this policy: **parthivsuryakb@gmail.com**
