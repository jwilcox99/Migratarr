#!/bin/sh
# Smoke test for a built image (CI: .github/workflows/docker.yml). Usage: docker/smoke-test.sh <image>
# Starts a container as PUID/PGID 1234 with an empty /config and checks what a new user meets first.
set -eu
IMAGE=$1
NAME=migratarr-smoke-$$
CONFIG=$(mktemp -d)
PORT=18765
# /config ends up owned by uid 1234, so the runner reads it with sudo (passwordless on CI runners).
AS_ROOT=$( [ "$(id -u)" = 0 ] && echo "" || echo sudo )
STAGE=start
stage() { STAGE=$*; echo "== $*"; }
fail() {
    FAILED=1
    echo "FAIL: $* (during: $STAGE)" >&2
    logs=$(docker logs --tail 25 "$NAME" 2>&1 || true)
    echo "$logs" >&2
    if [ -n "${GITHUB_ACTIONS:-}" ]; then  # annotations reach the API even when logs can't be read
        printf '::error title=smoke test::%s (during: %s)%%0A%s\n' "$*" "$STAGE" "$(printf '%s' "$logs" | sed 's/%/%25/g' | tr '\n' '\r' | sed 's/\r/%0A/g')"
    fi
    exit 1
}
cleanup() {
    rc=$?
    if [ "$rc" != 0 ] && [ -z "${FAILED:-}" ] && [ -n "${GITHUB_ACTIONS:-}" ]; then
        echo "::error title=smoke test::stopped unexpectedly (exit $rc) during: $STAGE"
    fi
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    $AS_ROOT rm -rf "$CONFIG" "$CONFIG.cookies" || true
}
trap cleanup EXIT

docker run -d --name "$NAME" -e PUID=1234 -e PGID=1234 -e TZ=America/Denver \
    -v "$CONFIG:/config" -p "127.0.0.1:$PORT:8765" "$IMAGE" >/dev/null

stage "waits until healthy"
for i in $(seq 1 30); do
    state=$(docker inspect -f '{{.State.Health.Status}}' "$NAME")
    [ "$state" = healthy ] && break
    [ "$i" = 30 ] && fail "not healthy after 60 s (state: $state)"
    sleep 2
done

stage "the first visit creates the login"
status=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "http://127.0.0.1:$PORT/")
case "$status" in "303 "*"/account/new"*) ;; *) fail "GET / gave '$status'";; esac
curl -sf "http://127.0.0.1:$PORT/account/new" | grep -q 'Create your login' || fail "no create-login page"
JAR="$CONFIG.cookies"
status=$(curl -s -o /dev/null -w '%{http_code}' -c "$JAR" -H "Origin: http://127.0.0.1:$PORT" \
    --data 'username=smoke&password=smoke-password&confirm=smoke-password&next=/' \
    "http://127.0.0.1:$PORT/account/new")
[ "$status" = 303 ] || fail "creating the login gave $status"

stage "setup offers what works in a container"
page=$(curl -sf -b "$JAR" "http://127.0.0.1:$PORT/setup/services") || fail "no setup page after signing in"
echo "$page" | grep -q 'value="http://radarr:7878"' || fail "Radarr URL default is not the container name"
echo "$page" | grep -q 'value="docker_config_xml"' && fail "setup offers reading keys through docker" || true
echo "$page" | grep -q 'Same media path as Migratarr' || fail "no container choice for Radarr and Sonarr"

stage "files in /config belong to PUID:PGID"
login=$($AS_ROOT find "$CONFIG" -name auth.json)
[ -n "$login" ] || fail "no auth.json in /config"
perms=$($AS_ROOT stat -c '%u:%g %a' "$login")
[ "$perms" = "1234:1234 600" ] || fail "auth.json is $perms"

stage "docker exec runs as PUID too"
docker exec "$NAME" migratarr --version | grep -q '^migratarr ' || fail "migratarr --version"
docker exec "$NAME" migratarr doctor --offline >/dev/null 2>&1 && fail "doctor passed with no settings" || true
docker exec "$NAME" migratarr password </dev/null >/dev/null 2>&1 && fail "password ran without a terminal" || true
root_owned=$($AS_ROOT find "$CONFIG" -user 0 | head -5)
[ -z "$root_owned" ] || fail "root-owned files in /config: $root_owned"

stage "the UI process runs as PUID"
uid=$(docker exec "$NAME" sh -c 'for d in /proc/[0-9]*; do grep -q "migratarr[.]cli" $d/cmdline 2>/dev/null && awk "/^Uid:/{print \$2}" $d/status && break; done')
[ "$uid" = 1234 ] || fail "UI runs as uid '$uid'"

stage "tools: ssh present, docker absent, TZ applied"
docker exec "$NAME" ssh -V >/dev/null 2>&1 || fail "no ssh client"
docker exec -u 1234:1234 "$NAME" ssh -G nas.invalid >/dev/null 2>&1 \
    || fail "ssh can't run as PUID: $(docker exec -u 1234:1234 "$NAME" ssh -G nas.invalid 2>&1 | head -1)"
docker exec "$NAME" sh -c 'command -v docker' >/dev/null 2>&1 && fail "docker CLI should not be in the image" || true
[ "$(docker exec "$NAME" date +%Z)" != UTC ] || fail "TZ not applied"

stage "restarts keep the login"
docker restart "$NAME" >/dev/null
for i in $(seq 1 30); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$NAME")" = healthy ] && break
    [ "$i" = 30 ] && fail "not healthy after restart"
    sleep 2
done
status=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "http://127.0.0.1:$PORT/")
case "$status" in "303 "*"/login"*) ;; *) fail "after restart GET / gave '$status' (login lost?)";; esac

stage "stops cleanly"
start=$(date +%s)
docker stop -t 20 "$NAME" >/dev/null
[ $(( $(date +%s) - start )) -lt 15 ] || fail "took more than 15 s to stop"
[ "$(docker inspect -f '{{.State.ExitCode}}' "$NAME")" -le 143 ] || fail "bad exit code"

echo "PASS: $IMAGE"
