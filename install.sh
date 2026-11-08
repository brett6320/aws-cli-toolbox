#!/usr/bin/env bash
# Install aws-cli-toolbox for the current user from a git ref (default: main),
# never from the working tree. Dry-run unless --execute.
#
#   1. snapshot the ref with `git archive` into a temp dir
#   2. console scripts: non-editable `uv tool install` from the snapshot
#   3. retire older ~/.functions.d/*.functions files the toolbox supersedes
#      (names in shell/replaces.txt)
#   4. copy shell/aws-toolbox.functions to ~/.functions.d/ (copied, not
#      symlinked: the rc loop uses `find -type f`, which skips links)
#   5. starter config at ~/.aws/toolbox.toml if none exists
#
# The installed copy is therefore unaffected by the checked-out branch or
# uncommitted work, and it changes only when you run this with --execute.
# There is deliberately no automatic sync; --status shows whether one is due. Every existing file that is replaced or retired is first
# copied to <dir>/.backup/<name>.<timestamp>.bak; restore commands are printed.
set -euo pipefail

usage() {
  cat <<'EOF'
usage: install.sh [--execute|--apply] [--status] [--ref REF] [--repo DIR] [--force]
  --execute     apply changes (default: dry-run)
  --status      show installed commit vs REF and exit (changes nothing)
  --ref REF     git ref to install (default: git config aws-toolbox.ref, else main)
  --repo DIR    repository to install from (default: this script's directory)
  --force       reinstall console scripts even if REF is already installed
EOF
}

execute=0 force=0 status=0 ref="" repo=""
while [ $# -gt 0 ]; do
  case "$1" in
    --execute|--apply) execute=1 ;;
    --force) force=1 ;;
    --status) status=1 ;;
    --ref) ref="${2:?--ref needs a value}"; shift ;;
    --repo) repo="${2:?--repo needs a value}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done

repo="$(cd "${repo:-$(dirname "$0")}" && pwd)"
ref="${ref:-$(git -C "$repo" config aws-toolbox.ref 2>/dev/null || echo main)}"
functions_dir="${FUNCTIONS_DIR:-$HOME/.functions.d}"
backup_dir="$functions_dir/.backup"
target="$functions_dir/aws-toolbox.functions"
state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/aws-cli-toolbox"
state_file="$state_dir/installed"
ts="$(date +%Y%m%d%H%M%S)"
restore=()

if (( execute )); then tag="[EXECUTE]"; else tag="[DRY-RUN]"; fi
say() { echo "$tag $*"; }
run() { say "$*"; if (( execute )); then "$@"; fi; }

# Back up FILE to the backup dir; remember how to restore it.
backup() {
  local file="$1" dir="${2:-$backup_dir}"
  local dest="$dir/$(basename "$1").$ts.bak"
  run mkdir -p "$dir"
  run cp -p "$file" "$dest"
  restore+=("cp -p '$dest' '$file'")
}

defined_functions() {  # names of shell functions defined in FILE
  sed -nE 's/^[[:space:]]*(function[[:space:]]+)?([A-Za-z0-9_][A-Za-z0-9_-]*)[[:space:]]*\(\).*/\2/p' "$1" | sort -u
}

# --- 0. resolve the ref ------------------------------------------------------
if ! commit="$(git -C "$repo" rev-parse --verify -q "$ref^{commit}")"; then
  echo "ERROR: $repo has no ref '$ref' (is it a git repo with that branch?)" >&2
  exit 1
fi
short="${commit:0:12}"
installed_commit=""
[ -f "$state_file" ] && installed_commit="$(cut -d' ' -f1 "$state_file")"

if (( status )); then
  if [ -z "$installed_commit" ]; then
    echo "not installed; $ref is at $short. Run: $0 --execute"
    exit 1
  elif [ "$installed_commit" = "$commit" ]; then
    echo "up to date: installed $short ($(cut -d' ' -f2- "$state_file"))"
  else
    echo "sync available: installed ${installed_commit:0:12}, $ref is at $short"
    git -C "$repo" log --oneline "$installed_commit..$commit" 2>/dev/null | sed 's/^/    /'
    echo "Run: $0 --execute"
    exit 3
  fi
  exit 0
fi

snap="$(mktemp -d "${TMPDIR:-/tmp}/aws-cli-toolbox.XXXXXX")"
trap 'rm -rf "$snap"' EXIT
git -C "$repo" archive "$commit" | tar -x -C "$snap"
echo "==> Installing $ref @ $short from $repo (snapshot, not the working tree)"

# --- 1. console scripts ------------------------------------------------------
echo "==> Console scripts"
if [ -n "${AWS_TOOLBOX_SKIP_UV:-}" ]; then
  say "skipping uv tool install (AWS_TOOLBOX_SKIP_UV set)"
elif [ "$installed_commit" = "$commit" ] && (( ! force )); then
  echo "    already at $short (use --force to reinstall)"
else
  # Not editable: uv copies the package into its own tool env, so the
  # snapshot (and the repo) can change or vanish afterwards.
  run uv tool install --reinstall "$snap"
fi

# --- 2. retire superseded functions files ------------------------------------
echo "==> Superseded functions in $functions_dir"
replaced="$(grep -vE '^[[:space:]]*(#|$)' "$snap/shell/replaces.txt" | sort -u)"
blocked=0
if [ -d "$functions_dir" ]; then
  while IFS= read -r file; do
    [ "$file" = "$target" ] && continue
    defs="$(defined_functions "$file")"
    [ -n "$defs" ] || continue
    overlap="$(comm -12 <(echo "$defs") <(echo "$replaced"))"
    [ -n "$overlap" ] || continue
    others="$(comm -23 <(echo "$defs") <(echo "$replaced"))"
    if [ -n "$others" ]; then
      echo "ERROR: $file mixes superseded functions with others; move these out by hand:" >&2
      echo "$overlap" | sed 's/^/    superseded: /' >&2
      echo "$others" | sed 's/^/    keep:       /' >&2
      blocked=1
      continue
    fi
    say "retiring $file (all of: $(echo $defs))"
    backup "$file"
    run rm "$file"
  done < <(find "$functions_dir" -type f -name '*.functions' -not -path "$backup_dir/*")
fi
if (( blocked )); then
  echo "Shell functions not changed; resolve the files above and re-run." >&2
  exit 1
fi

# --- 3. toolbox functions file -----------------------------------------------
echo "==> $target"
new_target=0
[ -f "$target" ] || new_target=1
if [ -f "$target" ] && cmp -s "$snap/shell/aws-toolbox.functions" "$target"; then
  echo "    already up to date"
else
  [ -f "$target" ] && backup "$target"
  run mkdir -p "$functions_dir"
  run install -m 0644 "$snap/shell/aws-toolbox.functions" "$target"
fi

# --- 4. starter config -------------------------------------------------------
config="${AWS_TOOLBOX_CONFIG:-$HOME/.aws/toolbox.toml}"
echo "==> $config"
if [ -f "$config" ]; then
  echo "    exists; leaving it alone"
else
  say "write starter config from $ref"
  if (( execute )); then
    mkdir -p "$(dirname "$config")"
    install -m 0600 "$snap/src/aws_toolbox/toolbox.example.toml" "$config"
  fi
fi

# --- 5. record what is installed ---------------------------------------------
if (( execute )); then
  mkdir -p "$state_dir"
  echo "$commit $ref $(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$state_file"
fi

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "NOTE: add ~/.local/bin to PATH (uv tool's install location)" >&2 ;;
esac

if (( ${#restore[@]} )); then
  echo "==> Rollback: to undo the file changes above, run:"
  printf '    %s\n' "${restore[@]}"
fi
if (( execute && new_target )); then
  echo "    (new file; remove with: rm '$target')"
fi
if (( execute )); then
  echo "==> Installed $ref @ $short. Open a new shell (or: source $target)."
else
  echo "==> Dry-run only. Re-run with --execute to apply."
fi
