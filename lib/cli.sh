# shellcheck shell=bash
# Shared by the bfep commands: help text and "did you mean" hints for typos.
# Source it; it only defines functions.

# The comment block at the top of the running command, without the leading "# ".
show_help() { sed -n '2,/^[^#]/{/^#/s/^# \{0,1\}//p}' "$0"; }

# closest WORD CANDIDATE...: prints the candidate WORD was most likely meant to
# be, or nothing. A candidate "key|text" matches on key and prints text, so an
# option that is really a setting can point at it ("--dry-run|DRY_RUN=1 bfep
# deploy"). Case, leading dashes, "_" vs "-" and "=value" are ignored, so
# --aply, apply, DRY_RUN=1 and --dry_run all find their match.
closest() {
  command -v python3 >/dev/null || return 0
  python3 -I -c '
import difflib, sys
norm = lambda s: s.lower().lstrip("-").replace("_", "-").split("=")[0]
pairs = {}
for c in sys.argv[2:]:
    key, _, text = c.partition("|")
    pairs.setdefault(norm(key), text or key)
m = difflib.get_close_matches(norm(sys.argv[1]), list(pairs), 1, 0.6)
print(pairs[m[0]] if m else "")
' "$@" 2>/dev/null || true
}

# usage_error HELP-NAME MESSAGE [CANDIDATE...]: says what was wrong, suggests
# the closest candidate to the word in MESSAGE's first quotes (if any), then
# exits 2. HELP-NAME is what "bfep help" takes for this command.
usage_error() {
  local name="$1" msg="$2" word guess=""
  shift 2
  if [[ "$msg" =~ \'([^\']*)\' ]]; then
    word="${BASH_REMATCH[1]}"
    [[ $# -gt 0 ]] && guess="$(closest "$word" "$@")"
  fi
  {
    echo "bfep $name: $msg"
    [[ -n "$guess" ]] && echo "Did you mean: $guess"
    echo "Run 'bfep help $name' for the options."
  } >&2
  exit 2
}
