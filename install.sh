#!/usr/bin/env bash
# Install aws-cli-toolbox for the current user from a git ref (default: main),
# never from the working tree. Dry-run unless --execute.
#
#   1. snapshot the ref with `git archive` into a temp dir
#   2. console scripts, for this user only (no sudo, never the system Python):
#      non-editable `uv tool install` from the snapshot, or without uv, a
#      private venv under ~/.local/share/aws-cli-toolbox with pip
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
                  [--installer auto|uv|pip] [--index-url URL] [--system-certs]
  --execute       apply changes (default: dry-run)
  --status        show installed commit vs REF and exit (changes nothing)
  --ref REF       git ref to install (default: git config aws-toolbox.ref, else main)
  --repo DIR      repository to install from (default: this script's directory)
  --force         reinstall console scripts even if REF is already installed
  --installer X   uv (uv tool install), pip (private venv + pip), or auto:
                  uv if it's on PATH, else pip (default: auto)
  --index-url URL package index to install from, e.g. a company mirror of PyPI
                  (default: uv's own setting, else pip's configured index-url)
  --system-certs  trust the OS certificate store (for TLS-inspecting proxies)
EOF
}

execute=0 force=0 status=0 ref="" repo="" installer=auto index_url="" system_certs=0
while [ $# -gt 0 ]; do
  case "$1" in
    --execute|--apply) execute=1 ;;
    --force) force=1 ;;
    --status) status=1 ;;
    --ref) ref="${2:?--ref needs a value}"; shift ;;
    --repo) repo="${2:?--repo needs a value}"; shift ;;
    --installer) installer="${2:?--installer needs a value}"; shift ;;
    --index-url) index_url="${2:?--index-url needs a value}"; shift ;;
    --system-certs) system_certs=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done

case "$installer" in auto|uv|pip) ;; *) usage >&2; exit 2 ;; esac
if [ "$(id -u)" = 0 ]; then
  echo "ERROR: run this as your own user, not root or sudo; everything installs under \$HOME" >&2
  exit 1
fi

repo="$(cd "${repo:-$(dirname "$0")}" && pwd)"
ref="${ref:-$(git -C "$repo" config aws-toolbox.ref 2>/dev/null || echo main)}"
functions_dir="${FUNCTIONS_DIR:-$HOME/.functions.d}"
backup_dir="$functions_dir/.backup"
target="$functions_dir/aws-toolbox.functions"
state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/aws-cli-toolbox"
state_file="$state_dir/installed"
data_dir="${XDG_DATA_HOME:-$HOME/.local/share}/aws-cli-toolbox"
bin_dir="$HOME/.local/bin"
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

redact() { sed -E 's#://[^/@]*@#://***@#'; }  # hide user:token in index URLs

# First python3.x on PATH that is 3.11+ (the package's requires-python).
find_python() {
  local c
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
      command -v "$c"; return 0
    fi
  done
  return 1
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
if [ "$installer" = auto ]; then
  if command -v uv >/dev/null; then installer=uv; else installer=pip; fi
fi
if [ -n "${AWS_TOOLBOX_SKIP_UV:-}" ]; then
  say "skipping uv tool install (AWS_TOOLBOX_SKIP_UV set)"
elif [ "$installed_commit" = "$commit" ] && (( ! force )); then
  echo "    already at $short (use --force to reinstall)"
else
  py="$(find_python || true)"
  # uv doesn't read pip.conf, and pip doesn't read uv's settings: find the
  # index once and give it to both. Exported rather than passed as an
  # argument so a token in the URL isn't printed.
  if [ -z "$index_url" ]; then
    index_url="${UV_DEFAULT_INDEX:-${UV_INDEX_URL:-${PIP_INDEX_URL:-}}}"
  fi
  if [ -z "$index_url" ] && [ -n "$py" ]; then
    for key in global.index-url install.index-url; do
      index_url="$("$py" -m pip config get "$key" 2>/dev/null || true)"
      [ -n "$index_url" ] && break
    done
  fi
  if [ -n "$index_url" ]; then
    echo "    package index: $(echo "$index_url" | redact)"
    export UV_DEFAULT_INDEX="$index_url" PIP_INDEX_URL="$index_url"
  fi
  if (( system_certs )); then
    # UV_NATIVE_TLS for uv releases older than UV_SYSTEM_CERTS. The pip setting
    # is an environment variable so pip's build-dependency subprocess gets it too.
    export UV_SYSTEM_CERTS=1 UV_NATIVE_TLS=1
  fi

  # Switching installers: remove the other one's copy so the two don't fight
  # over ~/.local/bin.
  if [ "$installer" = uv ] && [ -d "$data_dir" ]; then
    say "removing the pip install in $data_dir (switching to uv)"
    run rm -rf "$data_dir"
  elif [ "$installer" = pip ] && command -v uv >/dev/null && uv tool list 2>/dev/null | grep -q '^aws-cli-toolbox '; then
    say "removing the uv tool install (switching to pip)"
    run uv tool uninstall aws-cli-toolbox
  fi

  if [ "$installer" = uv ]; then
    # Not editable: uv copies the package into its own tool env, so the
    # snapshot (and the repo) can change or vanish afterwards. --force
    # replaces commands left in ~/.local/bin by an earlier pip install.
    run uv tool install --reinstall --force ${py:+--python "$py"} "$snap"
  else
    if [ -z "$py" ]; then
      echo "ERROR: no Python 3.11+ on PATH (python3.13/3.12/3.11/python3); install one for your user, or install uv" >&2
      exit 1
    fi
    if ! "$py" -c 'import ensurepip, venv' 2>/dev/null; then
      echo "ERROR: $py can't create venvs (no ensurepip; e.g. Debian without python3-venv); install uv instead" >&2
      exit 1
    fi
    if (( system_certs )); then
      # pip 24.2+ trusts the OS store by default; 23.3+ needs the feature.
      case "$("$py" -c 'import ensurepip as e; v = tuple(int(x) for x in e.version().split(".")[:2]); print("new" if v >= (24, 2) else "flag" if v >= (23, 3) else "old")')" in
        flag) export PIP_USE_FEATURE=truststore ;;
        old) echo "WARNING: $py bundles a pip too old for --system-certs; set PIP_CERT to a CA bundle instead" >&2 ;;
      esac
    fi
    scripts="$(sed -n '/^\[project.scripts\]/,/^\[/s/^\([A-Za-z0-9_-]*\) *=.*/\1/p' "$snap/pyproject.toml")"
    if [ -z "$scripts" ]; then
      echo "ERROR: found no [project.scripts] in pyproject.toml" >&2
      exit 1
    fi
    # A new venv every time, then repoint `current` and the command links, so
    # a failed install leaves the previous one working.
    name="$short.$ts"
    venv="$data_dir/venvs/$name"
    run "$py" -m venv "$venv"
    run "$venv/bin/python" -m pip install --disable-pip-version-check "$snap"
    run ln -sfn "venvs/$name" "$data_dir/current"
    run mkdir -p "$bin_dir"
    for s in $scripts; do
      run ln -sf "$data_dir/current/bin/$s" "$bin_dir/$s"
    done
    if [ -d "$data_dir/venvs" ]; then
      for old in "$data_dir"/venvs/*; do
        [ "$old" = "$venv" ] || run rm -rf "$old"
      done
    fi
  fi
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
  *) echo "NOTE: add ~/.local/bin to PATH (where the commands are installed)" >&2 ;;
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
