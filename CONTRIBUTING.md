# Contributing to scrobSter

Bug reports, fixes and ideas are welcome. Open an issue first for a large
change, so we can agree on the shape before you write it.

## Set up

You need Python 3.10 to 3.13, and ffmpeg on PATH to record real audio.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The Dockerfile and CI use Python 3.12. Python 3.13 removed `audioop`, which
pydub (under shazamio) imports, so `requirements.txt` installs `audioop-lts`
there through a `python_version >= "3.13"` marker. Nothing extra is needed.

Run it as in the README's Quick start:

```sh
.venv/bin/python -m scrobster
```

## Run the tests

```sh
.venv/bin/python test_scrobster.py
```

It prints log lines, then `ok`. Any failure stops it with an assertion error.

`test_scrobster.py` is a plain script, not a pytest suite: `main()` runs the
checks and each `check_*` function covers one area. pytest collects nothing
from it. Add a new check as a `check_*` function and call it from `main()`.

CI runs the same script on every pull request, then imports every module,
lints the Helm chart, builds the image for amd64 and 32-bit ARM, and installs
the chart into a kind cluster.

## Screenshots

The pictures in `docs/images` come from `scripts/screenshots.py`, which runs
the app on made-up accounts, rooms and plays. It never reads your `.env` or
database, and it replaces the audio devices, ffmpeg, Shazam, the services and
cover art, so it needs no hardware and refuses any connection that leaves the
machine. It needs Node.js with the `playwright` package and its Chromium, and
shrinks the pictures with `pngquant` and `optipng` when they are installed:

```sh
npm install playwright && npx playwright install chromium
.venv/bin/python scripts/screenshots.py
```

Run it again after a change to the page, and look at every picture before you
commit it. It takes about a minute, most of it a 12-second browser-mic clip.

## Dependencies

`requirements.txt` pins exact versions, so a rebuild of the same commit
installs the same packages. Above all, a rebuild must not pull a new shazamio
that breaks matching.

Do not loosen a pin or bump one by hand without a reason. Dependabot opens
weekly pull requests for the pins, the GitHub Actions and the Docker base
image, and CI tests each one. A move to a new Python is done by hand, together
with the `python-version` in `.github/workflows/ci.yml`.

## Pull requests

- **Keep the docs with the change.** Anything a user would notice, such as a
  setting, an API route or an MQTT entity, goes in the README or docs/ in the same pull
  request.
- **Do not bump the version** for ordinary work. `__version__` in
  `scrobster/__init__.py` is what cuts a release, so only a release changes it.
  See [Releasing](docs/releasing.md).
- **Match the surrounding code.** There is no formatter or linter to satisfy.
  Follow the style of the file you change, and explain why in comments, not
  what.
- **Keep secrets out.** Do not commit a `.env`, a database or a camera URL.
