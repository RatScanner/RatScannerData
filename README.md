# RatScannerData

Automated release builder for the runtime data consumed by
[RatScanner](https://github.com/TarkovTracker-org/RatScanner).

This repository intentionally does **not** track generated item images, OCR models or
map metadata. Its workflow downloads authoritative upstream inputs, validates them,
converts transparent item base images into the PNG format RatEye expects, resolves map
artwork from `maps.json`, and publishes the result as a single `Data.zip` release asset.

Catalog entries that still point at tarkov.dev's generic unknown-item placeholder are
recorded in the manifest and omitted from template matching. Publishing the same
placeholder under many item ids would create ambiguous, incorrect scan results.

## Bundle contents

`Data.zip` preserves RatScanner's established package contract:

- `icons/{item-id}.png` — generated from tarkov.dev `baseImageLink` assets;
- `unknown.png` — generated from the tarkov.dev unknown-item base image;
- `maps.json` — from the maintained tarkov.dev web application;
- `traineddata/*.traineddata` — from `tesseract-ocr/tessdata_fast`;
- `maps/{map-id}.svg` — downloaded from the `svgPath` of each interactive `maps.json` entry;
- `banner/{map-id}.png` and `banner/default.png` — copied forward from `banner/`;
- `manifest.json` and `THIRD_PARTY_NOTICES.md` — provenance and checksums.

## Map artwork is driven by `maps.json`

Map SVGs are no longer carried forward wholesale. On each build the builder walks
`maps.json`, and for every entry with `projection: "interactive"` and an `svgPath` it
downloads that artwork and stores it as `maps/{map-id}.svg`.

The filename is the tarkov.dev **map id** for the artwork, which is resolved by
matching the entry's `normalizedName` against the map catalog
(`https://json.tarkov.dev/regular/maps`). An exact match wins; otherwise the shortest
longer name sharing the prefix is used, because `maps.json` has one entry per artwork
while the catalog has a separate map per variant — `ground-zero` is the artwork behind
`ground-zero`, `ground-zero-21` and `ground-zero-tutorial`. This mirrors how RatScanner
resolves maps at runtime, so the filenames keep matching the ids the client looks up.

Consequences of driving off `maps.json`:

- new maps appear automatically once `maps.json` references them;
- SVG artwork is refreshed whenever tarkov.dev updates it, with no commit needed.

## Why `banner/` is still tracked

`banner/{map-id}.png` has **no** public upstream endpoint — nothing serves the per-map
banner art — so `banner/` remains tracked and copied forward verbatim. Only banners
whose map id is present in the bundle are copied, which keeps the banner/SVG pairing
intact as `maps.json` changes. `banner/default.png` is the shared fallback for maps with
no art of their own and is always required.

`maps/` also stays tracked, but only as a **fallback**: a tracked SVG is used when
`maps.json` references a map whose artwork upstream will not serve, or when a map id
RatScanner exposes is no longer referenced by `maps.json`. The Labyrinth, Icebreaker
and Transits maps are in this category today. Every such file is listed under
`mapNotes` in the manifest with the reason it was used, and a build that cannot produce
at least eight map SVGs fails rather than shipping an incomplete map set.

To refresh the carried artwork, replace the files in `maps/` or `banner/` and the next
build picks them up — no code change required.

## Build locally

Requires Python 3.10 or later.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\ruff check scripts
.\.venv\Scripts\ruff format --check scripts
.\.venv\Scripts\python scripts\build_data.py
.\.venv\Scripts\python scripts\validate_archive.py build\release\Data.zip
```

Outputs are written under `build/` and remain untracked.

## Release automation

The `Build data bundle` workflow supports:

- `validate` — build and retain a workflow artifact without creating a release;
- `draft` — create a draft GitHub release for maintainer verification;
- `publish` — create the published `latest` release;
- a weekly schedule that publishes only when validated content changes.

The workflow also supports manual runs after major Escape from Tarkov patches.
Every build downloads from upstream once on the GitHub runner, allowing RatScanner
users to retain the existing single-request `Data.zip` setup.

## Source endpoints

| Data             | Source                                                         |
| ---------------- | -------------------------------------------------------------- |
| Item catalog     | `https://json.tarkov.dev/regular/items`                        |
| Item base images | Item `baseImageLink` values hosted on `assets.tarkov.dev`      |
| Map catalog      | `https://json.tarkov.dev/regular/maps`                         |
| Interactive maps | `the-hideout/tarkov-dev/src/data/maps.json`                    |
| OCR models       | `tesseract-ocr/tessdata_fast` release `4.1.0`                  |
| Map SVGs         | `svgPath` values in `maps.json`, hosted on `assets.tarkov.dev` |
| Map banners      | this repository (`banner/`), carried forward                   |

## Licensing

The builder code is MIT licensed. Generated archives contain third-party, game-derived
images that are **not** covered by this repository's MIT license. RatScanner and
TarkovTracker do not claim ownership of those images. All applicable rights remain with
Battlestate Games and their respective owners. RatScanner is not affiliated with or
endorsed by Battlestate Games or tarkov.dev.

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for sources and license details.
