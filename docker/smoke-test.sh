#!/bin/sh
# Smoke test for a built image (CI: .github/workflows/docker.yml). Usage: docker/smoke-test.sh <image>
# Starts a container as PUID/PGID 1234 with an empty /config and checks what a new user meets first.
set -eu
IMAGE=$1
NAME=migratarr-smoke-$$
CONFIG=$(mktemp -d)
PORT=18765
fail() { echo "FAIL: $*" >&2; docker logs "$NAME" >&2 || true; exit 1; }
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; sudo rm -rf "$CONFIG" 2>/dev/null || rm -rf "$CONFIG"; }
trap cleanup EXIT

docker run -d --name "$NAME" -e PUID=1234 -e PGID=1234 -e TZ=America/Denver \
    -v "$CONFIG:/config" -p "127.0.0.1:$PORT:8765" "$IMAGE" >/dev/null

echo "== waits until healthy"
for i in $(seq 1 30); do
    state=$(docker inspect -f '{{.State.Health.Status}}' "$NAME")
    [ "$state" = healthy ] && break
    [ "$i" = 30 ] && fail "not healthy after 60 s (state: $state)"
    sleep 2
done

echo "== the first visit creates the login"
status=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "http://127.0.0.1:$PORT/")
case "$status" in "303 "*"/account/new"*) ;; *) fail "GET / gave '$status'";; esac
curl -sf "http://127.0.0.1:$PORT/account/new" | grep -q 'Create your login' || fail "no create-login page"
status=$(curl -s -o /dev/null -w '%{http_code}' -H "Origin: http://127.0.0.1:$PORT" \
    --data 'username=smoke&password=smoke-password&confirm=smoke-password&next=/' \
    "http://127.0.0.1:$PORT/account/new")
[ "$status" = 303 ] || fail "creating the login gave $status"

echo "== files in /config belong to PUID:PGID"
login=$(find "$CONFIG" -name auth.json)
[ -n "$login" ] || fail "no auth.json in /config"
[ "$(stat -c '%u:%g %a' "$login")" = "1234:1234 600" ] || fail "auth.json is $(stat -c '%u:%g %a' "$login")"

echo "== docker exec runs as PUID too"
docker exec "$NAME" migratarr --version | grep -q '^migratarr ' || fail "migratarr --version"
docker exec "$NAME" migratarr doctor --offline >/dev/null 2>&1 && fail "doctor passed with no settings" || true
docker exec "$NAME" migratarr password </dev/null >/dev/null 2>&1 && fail "password ran without a terminal" || true
root_owned=$(find "$CONFIG" -user 0 | head -5)
[ -z "$root_owned" ] || fail "root-owned files in /config: $root_owned"

echo "== the UI process runs as PUID"
uid=$(docker exec "$NAME" sh -c 'for d in /proc/[0-9]*; do grep -q "migratarr.cli" $d/cmdline 2>/dev/null && awk "/^Uid:/{print \$2}" $d/status && break; done')
[ "$uid" = 1234 ] || fail "UI runs as uid '$uid'"

echo "== tools: ssh present, docker absent, TZ applied"
docker exec "$NAME" ssh -V >/dev/null 2>&1 || fail "no ssh client"
docker exec "$NAME" sh -c 'command -v docker' >/dev/null 2>&1 && fail "docker CLI should not be in the image" || true
[ "$(docker exec "$NAME" date +%Z)" != UTC ] || fail "TZ not applied"

echo "== restarts keep the login"
docker restart "$NAME" >/dev/null
for i in $(seq 1 30); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$NAME")" = healthy ] && break
    [ "$i" = 30 ] && fail "not healthy after restart"
    sleep 2
done
status=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "http://127.0.0.1:$PORT/")
case "$status" in "303 "*"/login"*) ;; *) fail "after restart GET / gave '$status' (login lost?)";; esac

echo "== stops cleanly"
start=$(date +%s)
docker stop -t 20 "$NAME" >/dev/null
[ $(( $(date +%s) - start )) -lt 15 ] || fail "took more than 15 s to stop"
[ "$(docker inspect -f '{{.State.ExitCode}}' "$NAME")" -le 143 ] || fail "bad exit code"

echo "PASS: $IMAGE"
