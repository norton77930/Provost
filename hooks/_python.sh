# Sourced by hooks/runtime and hooks/native-event; not an entrypoint.
# Resolves a Python >= 3.11 for the adaptive runtime with as few process
# spawns as possible: on Windows Git Bash every spawn (and even a failing
# `command -v` PATH scan) costs about a second.
#
# provost_find_python fills the PROVOST_PY array with the command to run, or
# returns 1 after printing the "requires Python 3.11+" message. Order:
#   1. $PROVOST_PYTHON when executable (no probe, no cache)
#   2. the cached interpreter path in ${PROVOST_HOME:-~/.claude/provost}/cache/python
#   3. python3, python, then py -3, each version-probed; the first python3/python
#      that qualifies is written to the cache (best effort).
# An interpreter inside the current working directory tree (a project venv on
# PATH) is used for that run only: never cached, and a cached one is skipped,
# so one repo's venv is not pinned for every other repo. No spawns: string checks
# against $PWD, confirmed with `builtin cd -P` in this shell when a path looks outside.

# Directory of this file (== hooks/) and the scripts/ module root, derived with
# parameter expansion only. Handles C:/..., /c/... and relative invocation.
_provost_here="${BASH_SOURCE[0]//\\//}"
case "$_provost_here" in
    */*) _provost_here="${_provost_here%/*}" ;;
    *) _provost_here="." ;;
esac
case "$_provost_here" in
    /* | [A-Za-z]:/*) ;;
    *) _provost_here="${PWD}/${_provost_here}" ;;
esac
_provost_here="${_provost_here%/.}"
if [[ "$_provost_here" == */hooks ]]; then
    _provost_module_root="${_provost_here%/hooks}/scripts"
else
    _provost_module_root="${_provost_here}/../scripts"
fi
if [[ "$_provost_module_root" == /* ]]; then
    # POSIX form: Windows Python needs a native path. Git Bash drive paths
    # (/c/...) convert by expansion; anything else goes through cygpath.
    _provost_native=""
    if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* ]] &&
        [[ "$_provost_module_root" =~ ^/([A-Za-z])/(.*)$ ]]; then
        _provost_native="${BASH_REMATCH[1]}:/${BASH_REMATCH[2]}"
        [[ -d "$_provost_native" ]] || _provost_native=""
    fi
    if [[ -z "$_provost_native" ]] && command -v cygpath >/dev/null 2>&1; then
        _provost_native="$(cygpath -w "$_provost_module_root")" || _provost_native=""
    fi
    [[ -n "$_provost_native" ]] && _provost_module_root="$_provost_native"
    unset _provost_native
fi
export PYTHONPATH="$_provost_module_root"
# Hooks run with the project as cwd; keep `python -m` from importing its modules (3.11+).
export PYTHONSAFEPATH=1
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUTF8=1
unset _provost_here _provost_module_root

PROVOST_PY=()

# True when $1 lies inside the current working directory tree. No spawn: compares
# against $PWD and, under Git Bash, its drive form (/c/x == C:/x, case-insensitive);
# a path that looks outside is compared again in physical form.
_provost_in_cwd() {
    local path="${1//\\//}" dir="${PWD%/}" native="" restore="" rc=1
    if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* ]]; then
        [[ "$dir" =~ ^/([A-Za-z])(/.*)?$ ]] && native="${BASH_REMATCH[1]}:${BASH_REMATCH[2]}"
        shopt -q nocasematch || { shopt -s nocasematch; restore=1; }
    fi
    if [[ -n "$dir" && "$path" == "$dir"/* ]] || [[ -n "$native" && "$path" == "$native"/* ]]; then
        rc=0
    elif ! _provost_physically_outside "$path"; then
        rc=0
    fi
    [[ -z "$restore" ]] || shopt -u nocasematch
    return "$rc"
}

# Succeeds only when $1's directory provably lies outside $PWD once both are
# resolved by `builtin cd -P` in this shell (no spawn): symlinks (macOS /var is
# /private/var) and, under Git Bash, 8.3 short names (RUNNER~1) and mounts (/tmp)
# become one spelling. The cwd, PWD and OLDPWD are put back. Fail-safe: anything
# that does not resolve counts as inside, so it is never cached.
_provost_physically_outside() {
    local here="$PWD" parent="${1%/*}" had_old="${OLDPWD+1}" old="${OLDPWD:-}" CDPATH="" home="" target=""
    [[ "$here" != / ]] || return 0 # as above: nothing counts as inside a cwd of /
    [[ "$1" == */* && -n "$here" ]] || return 1
    [[ -n "$parent" ]] || parent=/
    # `cd -- -` would jump to OLDPWD and print it: a parent named "-" is a directory.
    [[ "$parent" != - ]] || parent=./-
    if builtin cd -P -- "$here" 2>/dev/null; then
        home="${PWD%/}"
        if builtin cd -P -- "$parent" 2>/dev/null; then
            target="$PWD"
            if ! builtin cd -P -- "${home:-/}" 2>/dev/null; then
                # Unresolved (fail-safe: inside); still try to put the cwd back.
                target=""
                builtin cd -- "$here" 2>/dev/null || true
            fi
        fi
    fi
    # Every path back out restores PWD and OLDPWD, even when the cwd could not be.
    PWD="$here"
    if [[ -n "$had_old" ]]; then OLDPWD="$old"; else unset OLDPWD; fi
    [[ -n "$target" ]] || return 1
    [[ -n "$home" && ( "$target" == "$home" || "$target" == "$home"/* ) ]] && return 1
    return 0
}

provost_find_python() {
    PROVOST_PY=()
    local cache_file="${PROVOST_HOME:-${HOME:-}/.claude/provost}/cache/python"
    local cached="" candidate resolved
    if [[ -n "${PROVOST_PYTHON:-}" && -f "$PROVOST_PYTHON" && -x "$PROVOST_PYTHON" ]]; then
        PROVOST_PY=("$PROVOST_PYTHON")
        return 0
    fi
    if [[ -r "$cache_file" ]]; then
        IFS= read -r cached < "$cache_file" 2>/dev/null || true
        cached="${cached%$'\r'}"
        if [[ -n "$cached" && -f "$cached" && -x "$cached" ]] && ! _provost_in_cwd "$cached"; then
            PROVOST_PY=("$cached")
            return 0
        fi
    fi
    for candidate in python3 python; do
        resolved="$(type -P "$candidate" 2>/dev/null)" || continue
        [[ -n "$resolved" ]] || continue
        if "$resolved" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
            PROVOST_PY=("$resolved")
            if ! _provost_in_cwd "$resolved"; then
                { mkdir -p "${cache_file%/*}" && printf '%s\n' "$resolved" > "$cache_file"; } 2>/dev/null || true
            fi
            return 0
        fi
    done
    if command -v py >/dev/null 2>&1 &&
        py -3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
        PROVOST_PY=(py -3)
        return 0
    fi
    printf '%s\n' 'Provost adaptive runtime requires Python 3.11+; nothing was saved.' >&2
    return 1
}
