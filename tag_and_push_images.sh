#!/bin/bash
#
# Build and publish the service images.
#
# Images are built for TARGET_PLATFORM (linux/amd64 by default), not for the
# machine running this script. That is deliberate: this project is developed on
# both Windows (amd64) and an Apple Silicon Mac (arm64), while every deploy
# host is amd64. Building natively meant the architecture of a release depended
# on which laptop cut it, and an arm64 push left the homelab unable to start:
#
#   no matching manifest for linux/amd64 in the manifest list entries
#
# `docker tag` cannot correct that after the fact -- retagging an arm64 image
# just publishes an arm64 image under another name -- so the build and the push
# are one buildx step. Override for a one-off:
#
#   TARGET_PLATFORM=linux/amd64,linux/arm64 ./tag_and_push_images.sh
#
# Note that a non-native platform builds under emulation and is slow; the pip
# installs are the slow part.

set -euo pipefail

# Run from the repo root whatever directory the caller is in: the build context
# is the repo, and the Dockerfile paths below are relative to it.
cd "$(dirname "${BASH_SOURCE[0]}")"

TARGET_PLATFORM="${TARGET_PLATFORM:-linux/amd64}"
REGISTRY_PREFIX="${REGISTRY_PREFIX:-italiandogs/vrcverify-}"

# Which Dockerfile builds which image. A case rather than an associative array:
# macOS ships bash 3.2, where `declare -A` does not exist.
dockerfile_for() {
    case "$1" in
        discord-bot)        echo "docker/Dockerfile-bot" ;;
        vrc-online-checker) echo "docker/Dockerfile-online-checker" ;;
        dashboard)          echo "docker/Dockerfile-dashboard" ;;
        vrc-group-inviter)  echo "docker/Dockerfile-invite-worker" ;;
        *) echo "No Dockerfile known for image '$1'" >&2; return 1 ;;
    esac
}

# Fail on the actual problem, once, instead of letting every docker call below
# print its own socket error.
preflight() {
    if ! docker info >/dev/null 2>&1; then
        echo "Docker daemon is not running (or is unreachable). Start Docker and try again." >&2
        exit 1
    fi
    if ! docker buildx version >/dev/null 2>&1; then
        echo "docker buildx is required to build for $TARGET_PLATFORM but is not available." >&2
        exit 1
    fi
}

get_version() {
    read -r -p "Please provide a version number: " version
    if [[ -z "$version" ]]; then
        echo "Version number is required. Exiting."
        exit 1
    fi
}

# Builds AND pushes in one step. Splitting them is what made cross-architecture
# releases impossible, and every command here is checked -- an earlier version
# printed "Pushed" after failures, which is worse than not printing at all.
build_and_push() {
    local image_name="$1"
    local version="$2"
    local dockerfile
    dockerfile="$(dockerfile_for "$image_name")"

    echo "Building and pushing ${image_name} (${TARGET_PLATFORM}) ..."
    if ! docker buildx build \
        --platform "$TARGET_PLATFORM" \
        --file "$dockerfile" \
        --tag "${REGISTRY_PREFIX}${image_name}:${version}" \
        --tag "${REGISTRY_PREFIX}${image_name}:latest" \
        --push \
        . ; then
        echo "FAILED: ${image_name} was not published." >&2
        return 1
    fi
    echo "Published ${image_name} as ${version} and latest (${TARGET_PLATFORM})"
}

echo "Select an option to build and publish:"
echo "1. Bot"
echo "2. VRC Online Checker"
echo "3. Dashboard (runs on the VPS, not the homelab)"
echo "4. VRChat Group Inviter"
echo "5. All"
echo "0. Exit"
read -r -p "Enter your choice (0-5): " choice

if [[ "$choice" == "0" ]]; then
    echo "Exiting script."
    exit 0
fi

case "$choice" in
    1|2|3|4|5) ;;
    *) echo "Invalid choice. Exiting script."; exit 1 ;;
esac

preflight
get_version

case "$choice" in
    1) build_and_push "discord-bot" "$version" ;;
    2) build_and_push "vrc-online-checker" "$version" ;;
    3) build_and_push "dashboard" "$version" ;;
    4) build_and_push "vrc-group-inviter" "$version" ;;
    5)
        build_and_push "discord-bot" "$version"
        build_and_push "vrc-online-checker" "$version"
        build_and_push "dashboard" "$version"
        build_and_push "vrc-group-inviter" "$version"
        ;;
esac

echo "Done. Published for ${TARGET_PLATFORM}."
