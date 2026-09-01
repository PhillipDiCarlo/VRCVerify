# Build and publish the service images.
#
# Images are built for $TargetPlatform (linux/amd64 by default), not for the
# machine running this script. That is deliberate: this project is developed on
# both Windows (amd64) and an Apple Silicon Mac (arm64), while every deploy host
# is amd64. Building natively meant the architecture of a release depended on
# which laptop cut it, and an arm64 push left the homelab unable to start:
#
#   no matching manifest for linux/amd64 in the manifest list entries
#
# docker tag cannot correct that after the fact -- retagging an arm64 image just
# publishes an arm64 image under another name -- so the build and the push are
# one buildx step. Override for a one-off:
#
#   $env:TARGET_PLATFORM = "linux/amd64,linux/arm64"; .\tag_and_push_images.ps1

$ErrorActionPreference = "Stop"

# Run from the repo root whatever directory the caller is in.
Set-Location -Path $PSScriptRoot

$TargetPlatform = if ($env:TARGET_PLATFORM) { $env:TARGET_PLATFORM } else { "linux/amd64" }
$RegistryPrefix = if ($env:REGISTRY_PREFIX) { $env:REGISTRY_PREFIX } else { "italiandogs/vrcverify-" }

function Get-Dockerfile($imageName) {
    switch ($imageName) {
        "discord-bot"        { return "docker/Dockerfile-bot" }
        "vrc-online-checker" { return "docker/Dockerfile-online-checker" }
        "dashboard"          { return "docker/Dockerfile-dashboard" }
        "vrc-group-inviter"  { return "docker/Dockerfile-invite-worker" }
        "status-reporter"    { return "docker/Dockerfile-status-reporter" }
        default { throw "No Dockerfile known for image '$imageName'" }
    }
}

# Fail on the actual problem, once, instead of letting every docker call print
# its own socket error.
function Invoke-Preflight {
    docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Docker daemon is not running (or is unreachable). Start Docker and try again."
        exit 1
    }
    docker buildx version *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Error "docker buildx is required to build for $TargetPlatform but is not available."
        exit 1
    }
}

function Get-Version {
    $v = Read-Host "Please provide a version number"
    if ([string]::IsNullOrWhiteSpace($v)) {
        Write-Host "Version number is required. Exiting."
        exit 1
    }
    return $v
}

# Builds AND pushes in one step. Splitting them is what made cross-architecture
# releases impossible, and the exit code is checked -- an earlier version printed
# "Pushed" after failures, which is worse than printing nothing.
function Publish-Image($imageName, $version) {
    $dockerfile = Get-Dockerfile $imageName
    Write-Host "Building and pushing $imageName ($TargetPlatform) ..."
    docker buildx build `
        --platform $TargetPlatform `
        --file $dockerfile `
        --tag "$RegistryPrefix$imageName`:$version" `
        --tag "$RegistryPrefix$imageName`:latest" `
        --push `
        .
    if ($LASTEXITCODE -ne 0) {
        Write-Error "FAILED: $imageName was not published."
        exit 1
    }
    Write-Host "Published $imageName as $version and latest ($TargetPlatform)"
}

$menu = @"
1. Bot
2. VRC Online Checker
3. Dashboard (runs on the VPS, not the homelab)
4. VRChat Group Inviter
5. Status Reporter (homelab; reports to status.vrcverify.com)
6. All
0. Exit
"@

Write-Host "Select an option to build and publish:"
Write-Host $menu
$choice = Read-Host "Enter your choice (0-6)"

if ($choice -eq "0") {
    Write-Host "Exiting script."
    exit 0
}
if ($choice -notin @("1", "2", "3", "4", "5", "6")) {
    Write-Host "Invalid choice. Exiting script."
    exit 1
}

Invoke-Preflight
$version = Get-Version

switch ($choice) {
    "1" { Publish-Image "discord-bot" $version }
    "2" { Publish-Image "vrc-online-checker" $version }
    "3" { Publish-Image "dashboard" $version }
    "4" { Publish-Image "vrc-group-inviter" $version }
    "5" { Publish-Image "status-reporter" $version }
    "6" {
        Publish-Image "discord-bot" $version
        Publish-Image "vrc-online-checker" $version
        Publish-Image "dashboard" $version
        Publish-Image "vrc-group-inviter" $version
        Publish-Image "status-reporter" $version
    }
}

Write-Host "Done. Published for $TargetPlatform."
