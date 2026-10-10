#!/bin/sh
# Dry-runs the old sync.py and the new package on the same campaigns and
# shows any difference in what they would do. Writes nothing to the database.
#
#   set -a; . /etc/bfep-sync.env; set +a
#   ./deploy/compare-dry-run.sh /path/to/old/sync-folder 5
#
# Both runs fetch the same N pages, so this makes 2*N requests (about 7 s
# each). A donation arriving between the two runs shows up as a difference.
set -eu
OLD=${1:?usage: compare-dry-run.sh OLD_FOLDER [LIMIT]}
LIMIT=${2:-5}
NEW=$(cd "$(dirname "$0")/.." && pwd)
OLD_PY=${OLD_PYTHON:-python3}
NEW_PY=${NEW_PYTHON:-$NEW/.venv/bin/python}
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT

echo "old: $OLD/sync.py --limit $LIMIT"
(cd "$OLD" && "$OLD_PY" sync.py --limit "$LIMIT") >"$T/old.txt" 2>&1 || echo "(old exited $?)"
echo "new: python -m bfep_sync --limit $LIMIT --retries 0"
(cd "$NEW" && "$NEW_PY" -m bfep_sync --limit "$LIMIT" --retries 0) >"$T/new.txt" 2>&1 || echo "(new exited $?)"

if diff -u "$T/old.txt" "$T/new.txt"; then
  echo "SAME: both would do exactly this:"
  cat "$T/new.txt"
else
  echo "DIFFERENT: see the diff above (- old, + new)."
  exit 1
fi
