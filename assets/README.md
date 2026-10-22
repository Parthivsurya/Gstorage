# Brand assets

| File | Role | Tracked |
|---|---|---|
| `gstore_logo_source.png` | Authored artwork, 1254×1254 RGBA. The master — keep it. | yes |
| `gstore_logo_1024.png` | 1024×1024 master, hand-derived from the source. **Input to `make_icon.py`.** | yes |
| `gstore_logo_120.png` | 120×120, for the Google OAuth consent screen (must stay ≤ 1 MB). | yes |
| `Gstore.icns` / `Gstore.ico` | App icons. Generated — gitignored. | no |
| `screenshots/*.png` | README screenshots. Generated, but **tracked** — the README and the Pages site both load them. | yes |

## Regenerating the icons

```sh
.venv/bin/python packaging/make_icon.py
```

Reads `gstore_logo_1024.png` and writes both `Gstore.icns` and `Gstore.ico`.
The release workflow runs this on every runner before PyInstaller, which is
why the two icon files are not tracked.

There is no script that regenerates `gstore_logo_1024.png` from
`gstore_logo_source.png` — that step was done by hand. `packaging/make_logo.py`
used to draw an *earlier, programmatic* mark and was removed; recover it from
git history if you ever want it.

## Regenerating the screenshots

```sh
.venv/bin/python packaging/make_screenshots.py
oxipng -o max --strip safe -a assets/screenshots/*.png
```

Re-run after any UI change that alters the Files or Storage tab. The script
drives the real widgets against a throwaway index seeded with fake
`@example.com` accounts — never your own database — so no real address, token
or filename can reach a published image.

It renders with the host's native widget style and light/dark setting, so the
output reflects the machine it ran on. The committed images are macOS dark
mode. `GSTORE_SHOT_OFFSCREEN=1` renders headless instead, at the cost of Qt's
generic theme.

All logo PNGs are losslessly optimised with `oxipng -o max --strip safe`.
Re-run that after replacing any of them.
