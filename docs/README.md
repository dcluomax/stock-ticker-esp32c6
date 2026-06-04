# docs/ assets

- `preview.jpg` — the image shown at the top of the main README and on the wiki.

## Want it to stand out? Use a real photo.

A real photo of the device running on a desk attracts far more interest than a
rendered mockup. To swap it in:

1. Take a landscape photo of the board showing the ticker screen (good lighting,
   plain background works best).
2. Resize to roughly **640×344** (or any 320×172-ish aspect) and save as
   `docs/preview.jpg`.
3. Commit and push:
   ```bash
   git add docs/preview.jpg
   git commit -m "Use real device photo in README"
   git push
   ```

The README and wiki both reference this same file, so they update together.
