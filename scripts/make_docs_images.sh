#!/usr/bin/env bash
# Regenerate every terminal image in the README from a real jitter run, so the
# screenshots can never drift from what the tool actually prints.
#
#   ./scripts/make_docs_images.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$here/docs/images"

cd "$here"

# The headline image: a test that only passes because another test ran first.
FORCE_COLOR=1 COLUMNS=84 python3 -m jitter --root "$work" run \
  -n 10 --screen 5 --verify 8 --seed 3 --name state -- \
  python3 -m pytest examples/flaky/test_flaky.py -k state -q \
  > "$work/study.ansi" 2>/dev/null || true

# Trim the captured-output block; the verdict is the story.
python3 - "$work/study.ansi" <<'PY' > "$work/trimmed.ansi"
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
print(re.split(r"── what it printed when it failed", text)[0].rstrip())
PY

sed "s|$work|~|g" "$work/trimmed.ansi" \
  | python3 "$here/scripts/ansi_to_svg.py" "$here/docs/images/study.svg" \
      --title "jitter run -- pytest examples/flaky/test_flaky.py -k state"

# The second image: the frozen world, used as a CI gate.
FORCE_COLOR=1 COLUMNS=84 python3 -m jitter --root "$work" freeze -n 5 -- \
  python3 -m pytest examples/flaky/test_flaky.py -q \
  > "$work/freeze.ansi" 2>/dev/null || true
sed "s|$work|~|g" "$work/freeze.ansi" \
  | python3 "$here/scripts/ansi_to_svg.py" "$here/docs/images/freeze.svg" \
      --title "jitter freeze -- pytest examples/flaky/test_flaky.py"

echo "wrote docs/images/study.svg and docs/images/freeze.svg"
