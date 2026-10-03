# Third-party notices

`Data.zip` produced by this repository redistributes or references assets and data
that are **not** covered by this repository's MIT license. The builder code is MIT
licensed; the generated payload is not.

## RatScanner

The bundle exists solely to keep the runtime data contract of
[RatScanner](https://github.com/TarkovTracker-org/RatScanner) working. RatScanner is
not affiliated with or endorsed by Battlestate Games or tarkov.dev.

## Item base images and map assets

Item icons (`icons/*.png`, `unknown.png`), map artwork (`maps/*.svg`) and map banner
art (`banner/*.png`) are third-party, game-derived images obtained through
[tarkov.dev](https://tarkov.dev) and its asset CDN. RatScanner and TarkovTracker do
not claim ownership of those images. All applicable rights remain with Battlestate
Games and their respective owners.

## tarkov.dev

- Item catalog: `https://json.tarkov.dev/regular/items`
- Map catalog: `https://json.tarkov.dev/regular/maps`
- Interactive map definitions: `https://raw.githubusercontent.com/the-hideout/tarkov-dev/main/src/data/maps.json`
- Item images: `https://assets.tarkov.dev/...`

The `tarkov-dev` source is MIT licensed; its license text is bundled into
`Data.zip` as `licenses/tarkov-dev-MIT.txt`.

## Tesseract OCR language data

The `traineddata/*.traineddata` files are taken unmodified from
[`tesseract-ocr/tessdata_fast`](https://github.com/tesseract-ocr/tessdata_fast)
release `4.1.0`, which is licensed under the Apache License 2.0. The license text is
bundled into `Data.zip` as `licenses/tessdata-Apache-2.0.txt`.

## Map SVG and banner sources

`maps/{mapId}.svg` is downloaded on each build from the `svgPath` that `maps.json`
records for the matching interactive map entry, and `banner/{mapId}.png` is copied
forward from the tracked `banner/` folder because no public endpoint serves the
per-map banner art. Both are keyed by tarkov.dev map id.

Artwork that `maps.json` references but upstream does not serve — and any tracked
`maps/` file for a map id `maps.json` no longer references — is copied forward from
this repository instead. Each such file is listed under `mapNotes` in the generated
`manifest.json` with the reason it was used, and is refreshed by hand when upstream
artwork changes.
