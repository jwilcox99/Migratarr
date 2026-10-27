# Source this (bash) in every shell you use for the test:  . examples/local-test/activate.sh
# It points `migratarr` at the test kit only: the test install, data folder and key folder.
# Your real Migratarr settings, keys and clone are never read or written from this shell.

if [ -z "${MTEST_ROOT:-}" ]; then
    echo 'activate.sh: export MTEST_ROOT first, e.g. export MTEST_ROOT="$HOME/migratarr-test"' >&2
    return 1 2>/dev/null || exit 1
fi
if [ ! -f "$MTEST_ROOT/.migratarr-test-kit" ]; then
    echo "activate.sh: $MTEST_ROOT was not made by ./kit.py init" >&2
    return 1 2>/dev/null || exit 1
fi

# Drop any MIGRATARR_* or TMDB_TOKEN setting that could point at the real install.
for _mtest_var in $(compgen -v MIGRATARR_); do unset "$_mtest_var"; done
unset _mtest_var TMDB_TOKEN

export MIGRATARR_HOME="$MTEST_ROOT/migratarr"
export MIGRATARR_SECRETS_DIR="$MTEST_ROOT/secrets"
case ":$PATH:" in
    *":$MTEST_ROOT/venv/bin:"*) ;;
    *) export PATH="$MTEST_ROOT/venv/bin:$PATH" ;;
esac
case "${PS1:-}" in
    "(mtest) "*) ;;
    *) PS1="(mtest) ${PS1:-}" ;;
esac
hash -r
echo "Test Migratarr: data $MIGRATARR_HOME, keys $MIGRATARR_SECRETS_DIR, command $(command -v migratarr || echo 'not installed yet')"
