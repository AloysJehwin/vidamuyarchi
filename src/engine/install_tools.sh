#!/usr/bin/env bash
# Installs the open-source verification/OSINT tools the engine uses, into ./bin and ./tools (no sudo).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS="$HERE/tools"; BIN="$HERE/bin"
mkdir -p "$TOOLS" "$BIN" "$HERE/osint" "$HERE/inbox"

echo "== reacher (check-if-email-exists) =="
if [ ! -x "$BIN/check_if_email_exists" ]; then
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) PAT="x86_64-unknown-linux-gnu" ;;
    Linux-aarch64) PAT="aarch64-unknown-linux-gnu" ;;
    Darwin-*) PAT="x86_64-apple-darwin" ;;
    *) PAT="x86_64-unknown-linux-gnu" ;;
  esac
  URL=$(curl -fsSL https://api.github.com/repos/reacherhq/check-if-email-exists/releases/latest | python3 -c "import json,sys; a=[x['browser_download_url'] for x in json.load(sys.stdin)['assets'] if '$PAT' in x['name'] and 'check_if_email_exists' in x['name'] and not x['name'].endswith('.sha256')]; print(a[0] if a else '')")
  if [ -n "$URL" ]; then
    TMP=$(mktemp -d); curl -fsSL "$URL" -o "$TMP/reacher.pkg"
    case "$URL" in
      *.tar.gz) tar -xzf "$TMP/reacher.pkg" -C "$TMP" ;;
      *.zip) python3 -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$TMP/reacher.pkg" "$TMP" ;;
      *) cp "$TMP/reacher.pkg" "$TMP/check_if_email_exists" ;;
    esac
    F=$(find "$TMP" -type f -name 'check_if_email_exists*' ! -name '*.pkg' | head -1)
    install -m 755 "$F" "$BIN/check_if_email_exists"; rm -rf "$TMP"
    echo "installed $($BIN/check_if_email_exists --version 2>/dev/null || echo check_if_email_exists)"
  else
    echo "!! no release asset for $PAT; download manually from https://github.com/reacherhq/check-if-email-exists/releases into $BIN/check_if_email_exists"
  fi
else
  echo "already installed: $($BIN/check_if_email_exists --version 2>/dev/null || echo present)"
fi

echo "== uv (self-contained Python toolchains, no sudo) =="
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
uv --version

echo "== emailfinder (Python 3.12 venv) =="
[ -x "$TOOLS/venv/bin/python" ] || uv venv --python 3.12 "$TOOLS/venv" -q
uv pip install -q --python "$TOOLS/venv/bin/python" emailfinder || echo "!! emailfinder install failed"
[ -x "$TOOLS/venv/bin/emailfinder" ] && ln -sf "$TOOLS/venv/bin/emailfinder" "$BIN/emailfinder" && echo "linked emailfinder" || echo "!! emailfinder not available"

echo "== theHarvester (git main needs Python >= 3.14) =="
[ -x "$TOOLS/venv-th/bin/python" ] || uv venv --python 3.14 "$TOOLS/venv-th" -q
uv pip install -q --python "$TOOLS/venv-th/bin/python" "git+https://github.com/laramies/theHarvester.git" || echo "!! theHarvester install failed"
[ -x "$TOOLS/venv-th/bin/theHarvester" ] && ln -sf "$TOOLS/venv-th/bin/theHarvester" "$BIN/theHarvester" && echo "linked theHarvester" || echo "!! theHarvester not available"

echo "== summary =="
ls -la "$BIN"
