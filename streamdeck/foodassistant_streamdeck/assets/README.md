# Stream Deck key icons

These assets let the controller draw the same Bootstrap Icons glyphs on the
deck that the web UI uses for each action (see `actions.ACTION_ICONS`).

## Files

- `bootstrap-icons.ttf` - the Bootstrap Icons glyph font, used by Pillow to
  rasterise icons onto key faces.
- `bootstrap-icons.json` - the glyph-name to Unicode-codepoint table shipped
  by Bootstrap Icons. The renderer looks up the codepoint by name and draws the
  matching glyph.

Both files are optional. If either is missing, `render.render_key` falls back
to a text-only key, so the deck still works without the font binary present.

## Source and version

- Bootstrap Icons v1.13.1 (matches the vendored web CSS at
  `service/app/static/vendor/bootstrap-icons.min.css`).
- Project: https://github.com/twbs/icons
- Font (woff2, convert to ttf):
  https://github.com/twbs/icons/raw/v1.13.1/font/fonts/bootstrap-icons.woff2
- Codepoint map (drop in as-is):
  https://github.com/twbs/icons/raw/v1.13.1/font/bootstrap-icons.json

Pillow cannot read woff2 reliably across versions, so this directory ships a
TrueType build instead. To regenerate the TTF from the official woff2:

```bash
pip install fonttools brotli
python - <<'PY'
from fontTools.ttLib import TTFont
f = TTFont("bootstrap-icons.woff2")
f.flavor = None
f.save("bootstrap-icons.ttf")
PY
```

## License

Bootstrap Icons is released under the MIT License. See `LICENSE` in this
directory for the full text. Copyright 2019-2024 The Bootstrap Authors.
