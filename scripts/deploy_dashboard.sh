#!/usr/bin/env bash
#
# Deploy the dashboard + tunnel on the VPS, then PROVE the site is serving.
#
# Run this instead of typing `docker compose` by hand. It exists because of a
# specific outage: on 2026-08-15 an interactive session left the cloudflared
# container stopped (SIGTERM, exit 0). The dashboard container kept running,
# the VPS stayed up, the tailnet stayed up -- and dashboard.vrcverify.com
# served Cloudflare error 1033 for 17 hours 43 minutes until someone happened
# to visit it. Nothing was broken. Nothing was going to fix it either.
#
# The lesson is not "be more careful with docker". It is that a deploy which
# ends at "the command returned 0" has verified nothing: `docker compose up -d`
# reports success for a stack whose front door is shut. So this script ends by
# making a real HTTPS request to the public hostname, over the real internet,
# through Cloudflare and the tunnel. That request is the deploy's exit code.
#
# Usage, from the compose directory on the VPS (~/vrcverify-dashboard):
#   ./deploy_dashboard.sh 2.6.0
#   VRCVERIFY_VERSION=2.6.0 ./deploy_dashboard.sh
#
set -euo pipefail

DASHBOARD_HOST="${DASHBOARD_HOST:-dashboard.vrcverify.com}"
HEALTH_URL="https://${DASHBOARD_HOST}/healthz"
DEADLINE_SECS="${DEADLINE_SECS:-90}"

VERSION="${1:-${VRCVERIFY_VERSION:-}}"

# Fail here, before touching a running stack. The compose file's
# `${VRCVERIFY_VERSION:?}` guard would also catch this, but it fires DURING
# `up`, which on a `down`-then-`up` sequence means everything is already
# stopped and nothing gets started -- the exact shape of an accidental outage.
if [[ -z "${VERSION}" ]]; then
	echo "error: no version given." >&2
	echo "usage: $0 <version>   (e.g. $0 2.6.0)" >&2
	echo "Must be a tag actually pushed to the registry. Never 'latest'." >&2
	exit 2
fi

if [[ "${VERSION}" == "latest" ]]; then
	echo "error: refusing to deploy 'latest'. Name a real version tag." >&2
	exit 2
fi

if [[ ! -f docker-compose.yml ]]; then
	echo "error: no docker-compose.yml here. Run this from the compose" >&2
	echo "directory on the VPS (~/vrcverify-dashboard)." >&2
	exit 2
fi

echo "==> deploying dashboard ${VERSION} to ${DASHBOARD_HOST}"

# `up -d` and not `restart`: it starts services that are merely STOPPED, which
# is precisely the state the 2026-08-15 outage was stuck in. Never pair this
# with a bare `down` or `stop` -- if you need to recreate, let `up` do it.
VRCVERIFY_VERSION="${VERSION}" docker compose up -d

echo "==> container state"
docker compose ps

# Both services, not just the one being upgraded. cloudflared is easy to forget
# precisely because it is never the thing you were deploying.
for svc in dashboard cloudflared; do
	cid="$(docker compose ps -q "${svc}" || true)"
	if [[ -z "${cid}" ]]; then
		echo "FAIL: service '${svc}' has no container." >&2
		exit 1
	fi
	state="$(docker inspect -f '{{.State.Status}}' "${cid}")"
	if [[ "${state}" != "running" ]]; then
		echo "FAIL: service '${svc}' is '${state}', not running." >&2
		docker compose logs --tail=50 "${svc}" >&2
		exit 1
	fi
done

# The check that actually matters. Everything above can pass while the site is
# dark; this cannot. A 200 here means DNS, the Cloudflare edge, the tunnel
# registration and the Flask app are all genuinely in the path -- error 1033 is
# not reachable from a passing run of this loop.
echo "==> waiting for ${HEALTH_URL} (up to ${DEADLINE_SECS}s)"
deadline=$((SECONDS + DEADLINE_SECS))
code=000
while ((SECONDS < deadline)); do
	code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "${HEALTH_URL}" || echo 000)"
	if [[ "${code}" == "200" ]]; then
		echo "==> OK: ${HEALTH_URL} returned 200. Site is serving."
		exit 0
	fi
	printf '    still %s ...\n' "${code}"
	sleep 3
done

# Failed. Say which half is broken rather than making the next person guess:
# 530/1033 means the connector never registered, anything else means the
# request reached Cloudflare and died closer to the app.
echo "FAIL: ${HEALTH_URL} last returned ${code} after ${DEADLINE_SECS}s." >&2
if [[ "${code}" == "530" ]]; then
	echo "530 is Cloudflare error 1033: the tunnel has no registered" >&2
	echo "connections. Look at cloudflared, not at the dashboard." >&2
fi
echo "--- cloudflared ---" >&2
docker compose logs --tail=40 cloudflared >&2
echo "--- dashboard ---" >&2
docker compose logs --tail=40 dashboard >&2
exit 1
