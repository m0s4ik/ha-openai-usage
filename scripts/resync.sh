#!/usr/bin/env bash
# Resync with a newer official openai_conversation integration.
#
#   scripts/resync.sh <home-assistant-version>      e.g. scripts/resync.sh 2026.12.0
#
# 1. Downloads the Home Assistant wheel of that version from PyPI (it contains
#    the integration with compiled translations) and the integration's tests
#    from the home-assistant/core tag (sparse clone).
# 2. Replaces upstream/ with them and shows what changed upstream.
# 3. Regenerates custom_components/openai_usage and tests/ with scripts/sync.py,
#    which re-applies every "# openai_usage:" hook and fails, naming the hook,
#    if an anchor moved.
# 4. Updates the version pins (requirements_test.txt, hacs.json).
#
# Then: run the tests (python -m pytest; --snapshot-update if only snapshots
# changed), check tests/ha_helpers.py against the core helpers it copies,
# bump VERSION, commit.
set -euo pipefail

VER="${1:?usage: scripts/resync.sh <home-assistant-version>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cd "$ROOT"

echo "==> Home Assistant $VER wheel"
URL="$(curl -fsSL "https://pypi.org/pypi/homeassistant/$VER/json" | python3 -c '
import json, sys
print(next(u["url"] for u in json.load(sys.stdin)["urls"] if u["packagetype"] == "bdist_wheel"))')"
curl -fsSL -o "$TMP/ha.whl" "$URL"
python3 - "$TMP/ha.whl" "$TMP/whl" <<'EOF'
import sys, zipfile
prefix = "homeassistant/components/openai_conversation/"
with zipfile.ZipFile(sys.argv[1]) as wheel:
    for name in wheel.namelist():
        if name.startswith(prefix):
            wheel.extract(name, sys.argv[2])
EOF
SRC="$TMP/whl/homeassistant/components/openai_conversation"

echo "==> home-assistant/core $VER tests"
git -c advice.detachedHead=false clone -q --depth 1 --filter=blob:none --sparse -b "$VER" \
  https://github.com/home-assistant/core "$TMP/core"
git -C "$TMP/core" sparse-checkout set tests/components/openai_conversation \
  tests/components/conversation tests/components/tts
TESTS="$TMP/core/tests/components/openai_conversation"

echo "==> upstream/"
rm -rf upstream/openai_conversation upstream/tests
mkdir -p upstream/openai_conversation/translations upstream/tests/snapshots
cp "$SRC"/*.py "$SRC"/manifest.json upstream/openai_conversation/
cp "$SRC"/translations/en.json "$SRC"/translations/it.json upstream/openai_conversation/translations/
cp "$TESTS"/*.py upstream/tests/
cp "$TESTS"/snapshots/*.ambr upstream/tests/snapshots/
echo "$VER" > upstream/VERSION

echo
echo "==> Upstream changes since the last sync:"
git add -N upstream >/dev/null 2>&1 || true
git --no-pager diff --stat -- upstream/ || true

echo
echo "==> Regenerating the integration and the tests"
python3 scripts/sync.py
cp upstream/tests/snapshots/*.ambr tests/snapshots/

echo "==> Version pins"
OPENAI="$(python3 -c 'import json; print(json.load(open("upstream/openai_conversation/manifest.json"))["requirements"][0])')"
sed -i -e "s/^homeassistant==.*/homeassistant==$VER/" \
  -e "s/^pytest-homeassistant==.*/pytest-homeassistant==$VER/" \
  -e "s/^openai[=<>~].*/$OPENAI/" requirements_test.txt
python3 - "$VER" <<'EOF'
import json, sys
data = json.load(open("hacs.json"))
data["homeassistant"] = sys.argv[1]
open("hacs.json", "w").write(json.dumps(data, indent=2) + "\n")
EOF

echo
echo "==> Core test helpers copied in tests/ha_helpers.py (compare by hand if they changed):"
for f in tests/components/conversation/__init__.py tests/components/tts/common.py; do
  echo "    $f ($(git -C "$TMP/core" log -1 --format=%h -- "$f" 2>/dev/null || echo '?'))"
done
echo
echo "Next: python -m pytest  (requirements of conversation/assist_pipeline/tts in"
echo "requirements_test.txt may need the versions of $VER), bump VERSION, commit."
