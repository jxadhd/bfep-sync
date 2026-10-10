#!/usr/bin/env bash
# One-time setup of the "bfep" command on the server. Safe to run again.
#
#   git clone https://github.com/jxadhd/bfep-sync /opt/bfep-sync-src
#   /opt/bfep-sync-src/install.sh
#
# 1. Deploys this checkout to /opt/bfep-sync with "bfep sync deploy" (tests
#    first; the current copy is kept in /opt/bfep-sync.prev).
# 2. Links /usr/local/bin/bfep to /opt/bfep-sync/bin/bfep, so every later
#    "bfep sync deploy" also updates the bfep command itself.
# 3. Turns each old /usr/local/bin/bfep-<name> script that this repo now
#    provides into a one-line wrapper for "bfep <name>", after copying the
#    original to /usr/local/lib/bfep/replaced/.
set -euo pipefail

SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
LIVE="${LIVE:-/opt/bfep-sync}"
BIN="${BIN:-/usr/local/bin}"
KEEP="${KEEP_DIR:-/usr/local/lib/bfep/replaced}"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ "${EUID:-$(id -u)}" == "0" || -n "${BFEP_INSTALL_ANY_USER:-}" ]] || die "run as root"
[[ "$SRC" != "$LIVE" ]] || die "run this from the git checkout (e.g. /opt/bfep-sync-src), not from $LIVE"

branch="$(git -C "$SRC" rev-parse --abbrev-ref HEAD)"
[[ "$branch" != "HEAD" ]] || branch="$(git -C "$SRC" rev-parse HEAD)"
REPO="$SRC" LIVE="$LIVE" "$SRC/commands/sync" deploy "$branch"

ln -sfn "$LIVE/bin/bfep" "$BIN/bfep"
echo
echo "==> $BIN/bfep -> $LIVE/bin/bfep"

mkdir -p "$KEEP"
for cmd in "$LIVE"/commands/*; do
  name="$(basename "$cmd")"
  old="$BIN/bfep-$name"
  [[ -f "$old" && ! -L "$old" ]] || continue
  grep -q '^exec bfep ' "$old" && continue
  cp -p "$old" "$KEEP/bfep-$name"
  printf '#!/bin/sh\n# Moved to "bfep %s". The original is in %s.\nexec bfep %s "$@"\n' \
    "$name" "$KEEP/bfep-$name" "$name" >"$old"
  echo "==> bfep-$name now runs 'bfep $name' (original kept in $KEEP)"
done

echo
"$BIN/bfep" help
