# Contributing to blink2video

Thanks for your interest. blink2video is a hobby project maintained in my spare
time: contributions are welcome, and these few rules keep them manageable.

## Before you write code

- Small fixes (a typo, an obvious bug with a clear fix): open a pull request
  directly.
- Anything bigger: open an issue first, so we agree on the approach before any
  code is written.

## Pull requests

- Keep them small and focused: one problem per pull request.
- Add or update tests for what you change.
- The test suite must pass. It needs no Blink account: `tests.py` builds a
  fake installation with clips made by ffmpeg.

  ```bash
  python -m unittest discover -p "test_*.py"
  python tests.py
  ```

  The CI runs both on Windows, macOS and Linux. To set up Python, see
  "From source" under "Getting started" in the README.
- The code and its comments are mostly in French. Comments in English are
  fine: they will be harmonized when merging.

## Sensitive areas

Please open an issue and discuss before touching:

- the updater (`maj.py`), which downloads and runs a new version;
- Blink authentication and TLS, including the Live View relay (see
  `AGENTS.md`);
- the build and release workflows (`.github/workflows/`, `build.py`,
  `deploy.py`).

## Reviews

Reviews happen when I have time, so some patience may be needed.

## Licence

blink2video is released under the GNU GPL v3 (see `LICENSE`). By submitting a
contribution, you agree that it is distributed under the same licence.
