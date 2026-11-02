#!/bin/sh
# Render docs/quickstart/quick-start.html to QUICKSTART.pdf at the repo root with
# Edge. Run shoot.py first, against a running server, to refresh the screenshots.
cd "$(dirname "$0")/../.." || exit 1
EDGE="/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
SRC="file:///$(cygpath -m "$PWD/docs/quickstart/quick-start.html")"
PROF=$(mktemp -d)
# Printed into the temp folder first: Edge cannot write over a PDF that a viewer
# has open, and says so only on stderr.
"$EDGE" --headless=new --disable-gpu --user-data-dir="$(cygpath -w "$PROF")" --no-pdf-header-footer \
  --print-to-pdf="$(cygpath -w "$PROF/out.pdf")" "$SRC" 2>/dev/null
[ -s "$PROF/out.pdf" ] || { echo "Edge produced no PDF"; exit 1; }
mv -f "$PROF/out.pdf" QUICKSTART.pdf 2>/dev/null \
  || { echo "QUICKSTART.pdf is open in another program: close it and re-run"; exit 1; }
python -c 'import re; d = open("QUICKSTART.pdf", "rb").read(); print("pages:", len(re.findall(rb"/Type\s*/Page[^s]", d)))'
