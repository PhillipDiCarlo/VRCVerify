#!/usr/bin/env bash
#
# Keep the Cloudflare tunnel up, whatever keeps stopping it.
#
# This is a mitigation, not a diagnosis. Something on this box stops the
# cloudflared container -- twice so far, cleanly, once at 02:42 local with
# nobody awake -- and Docker treats a clean stop as intent, so no `restart:`
# policy will undo it. Until diagnose_tunnel_stop.sh names the culprit, this
# caps the damage: worst case the site is dark for one timer interval instead
# of the 17h43m and 11h00m the first two outages ran to.
#
# It also produces the record we are missing. Every recovery is logged to the
# journal with a timestamp, so after a few days:
#
#   journalctl -t vrcverify-watchdog --since '3 days ago'
#
# tells you exactly how often this fires and at what times -- which may well
# identify the trigger on its own.
#
# Deliberately uses `docker start`, NOT `docker compose up -d`. The failure
# mode is "the container exists and is stopped", and `docker start` is the
# precise inverse of that. It also sidesteps the compose file's
# `${VRCVERIFY_VERSION:?}` guard, which would make a compose-based watchdog
# fail exactly when it is needed. Choosing the version is a deploy decision and
# a watchdog must never make it.
#
set -uo pipefail

CONTAINER="${CONTAINER:-vrcverify-dashboard-cloudflared-1}"
TAG="vrcverify-watchdog"

log() { logger -t "${TAG}" -- "$*"; echo "${TAG}: $*"; }

# Prove the daemon is reachable BEFORE interpreting anything it says. Without
# this, "I cannot talk to docker" is indistinguishable from "the container is
# gone" -- a far more alarming claim, and a false one. This watchdog spent its
# first five minutes installed making exactly that mistake: run as root under a
# system unit, it queried the rootful daemon at /var/run/docker.sock while the
# stack ran on the ROOTLESS daemon at /run/user/1000/docker.sock, and alerted
# every two minutes about a container that was up the whole time.
#
# A watchdog that cannot see the thing it guards must say so in those words.
# Reporting it as a missing container sends the next person hunting a deploy
# problem that does not exist.
if ! docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
	log "ERROR cannot reach the docker daemon (DOCKER_HOST=${DOCKER_HOST:-unset}, context=$(docker context show 2>/dev/null || echo unknown)) -- NOT protecting anything"
	exit 1
fi

state="$(docker inspect -f '{{.State.Status}}' "${CONTAINER}" 2>/dev/null)" || state="missing"

case "${state}" in
running)
	# Running is not the same as connected. If the connector is up but has
	# registered no connections, the hostname still serves error 1033 -- so ask
	# cloudflared's own metrics endpoint rather than trusting the process.
	ip="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' \
		"${CONTAINER}" 2>/dev/null | awk '{print $1}')"
	if [[ -n "${ip}" ]]; then
		ready="$(curl -s --max-time 5 "http://${ip}:20241/ready" 2>/dev/null)"
		# /ready reports readyConnections. Zero means registered with nothing.
		if [[ -n "${ready}" ]] && echo "${ready}" | grep -q '"readyConnections":0'; then
			log "ALERT container running but readyConnections=0; restarting"
			docker restart "${CONTAINER}" >/dev/null 2>&1 \
				&& log "restarted ok" || log "ERROR restart failed"
			exit 0
		fi
	fi
	# Healthy. Stay silent -- a watchdog that logs on every tick buries the
	# lines that matter.
	exit 0
	;;
missing)
	# The container is gone entirely, which `docker start` cannot fix. That
	# needs a real deploy, so say so loudly rather than failing quietly.
	log "ALERT container ${CONTAINER} does not exist -- run deploy_dashboard.sh"
	exit 1
	;;
*)
	log "ALERT container was '${state}' (site is serving error 1033); starting it"
	if docker start "${CONTAINER}" >/dev/null 2>&1; then
		log "started ok, was '${state}'"
	else
		log "ERROR docker start failed, container was '${state}'"
		exit 1
	fi
	;;
esac
