#!/usr/bin/env sh
#
# Assemble the publishable site into _site/.
#
# WHY THIS EXISTS
# ---------------
# Cloudflare Pages uploads the contents of the build output directory and
# nothing else. If you point it at the repository root, it publishes the
# WHOLE repo -- including engine/signals.py, engine/backtest.py and
# .github/workflows/. Anyone could then read the strategy at
#   https://<project>.pages.dev/engine/backtest.py
# which defeats the entire reason for making the repo private.
#
# So the output directory is an explicit allowlist. Files are published
# because they are named here, not because they happen to sit in the repo.
# Add a page, add it below.
#
# set -e means a missing file fails the build loudly. That is deliberate:
# a build that half-succeeds would publish a broken site, and a broken site
# is harder to notice than a failed build.

set -e

rm -rf _site
mkdir -p _site

# pages
cp index.html ticker.html alerts.html v2.html _site/

# shared css + js
cp -r assets _site/

# published data written by engine/signals.py and engine/backtest.py
cp -r data _site/

echo "--- publishing $(find _site -type f | wc -l) files:"
find _site -type f | sort | sed 's/^/    /'

# Fail loudly if anything private slipped in. Cheap insurance against a
# careless `cp -r . _site/` some future evening.
if find _site -name '*.py' -o -name '*.yml' -o -name '*.yaml' | grep -q .; then
  echo "ERROR: source or workflow files found in _site -- refusing to publish" >&2
  exit 1
fi
