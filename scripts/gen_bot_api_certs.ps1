# Issues the certificates the bot API and the dashboard authenticate each other
# with (issue #65). A direct port of gen_bot_api_certs.sh — read that one for
# why a private CA exists at all, and for the rotation procedure. Keep the two
# in step if you change either.
#
# Needs openssl on PATH. Git for Windows ships one; if `openssl` is not found,
# add C:\Program Files\Git\usr\bin to PATH for this session.
#
# USAGE
#   .\scripts\gen_bot_api_certs.ps1 -Host 100.64.0.2
#   .\scripts\gen_bot_api_certs.ps1 -Host vrcverify-bot.tailnet-name.ts.net -OutDir .\certs

[CmdletBinding()]
param(
    # The name or tailnet IP the dashboard will connect to. Must match
    # BOT_API_BIND and the host in BOT_API_URL, or the handshake fails on
    # hostname verification.
    [Parameter(Mandatory = $true)]
    [string]$ApiHost,

    [string]$OutDir = ".\certs"
)

$ErrorActionPreference = "Stop"

# Git for Windows' openssl.exe is an MSYS program, so it rewrites any argument
# starting with "/" into a Windows path — "/CN=VRCVerify Internal CA" would
# arrive as "C:/Program Files/Git/CN=..." and be rejected as a malformed
# subject. This disables that rewriting for the child processes below.
$env:MSYS_NO_PATHCONV = "1"

if (-not (Get-Command openssl -ErrorAction SilentlyContinue)) {
    Write-Host "openssl was not found on PATH."
    Write-Host "Try: `$env:PATH += ';C:\Program Files\Git\usr\bin'"
    exit 1
}

# An IP needs an IP SAN, a name needs a DNS SAN. Getting this wrong is the
# single most common reason an otherwise correct mTLS setup refuses to connect.
if ($ApiHost -match '^\d+\.\d+\.\d+\.\d+$') {
    $san = "IP:$ApiHost"
} else {
    $san = "DNS:$ApiHost"
}

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }
Push-Location $OutDir

try {
    Write-Host "==> Certificate authority"
    if (Test-Path ca.key) {
        Write-Host "    ca.key exists; reusing it (rotating leaves only)."
    } else {
        openssl genrsa -out ca.key 4096
        openssl req -x509 -new -nodes -key ca.key -sha256 -days 3650 `
            -subj "/CN=VRCVerify Internal CA" -out ca.pem
    }

    Write-Host "==> Server certificate for the bot API ($san)"
    $serverExt = New-TemporaryFile
    @(
        "subjectAltName=$san",
        "extendedKeyUsage=serverAuth",
        "basicConstraints=CA:FALSE"
    ) | Set-Content -Path $serverExt -Encoding ascii

    openssl genrsa -out bot-api.key 4096
    openssl req -new -key bot-api.key -subj "/CN=$ApiHost" -out bot-api.csr
    openssl x509 -req -in bot-api.csr -CA ca.pem -CAkey ca.key -CAcreateserial `
        -out bot-api.pem -days 825 -sha256 -extfile $serverExt

    Write-Host "==> Client certificate for the dashboard"
    # The CN here is what BOT_API_CLIENT_CN pins on the bot side. Keep them equal.
    $clientExt = New-TemporaryFile
    @(
        "extendedKeyUsage=clientAuth",
        "basicConstraints=CA:FALSE"
    ) | Set-Content -Path $clientExt -Encoding ascii

    openssl genrsa -out dashboard.key 4096
    openssl req -new -key dashboard.key -subj "/CN=vrcverify-dashboard" -out dashboard.csr
    openssl x509 -req -in dashboard.csr -CA ca.pem -CAkey ca.key -CAcreateserial `
        -out dashboard.pem -days 825 -sha256 -extfile $clientExt

    Remove-Item bot-api.csr, dashboard.csr, $serverExt, $clientExt -Force

    Write-Host ""
    Write-Host "Done. Files are in $(Get-Location)"
    Write-Host ""
    Write-Host "Homelab (.env):"
    Write-Host "  BOT_API_CA=/certs/ca.pem"
    Write-Host "  BOT_API_CERT=/certs/bot-api.pem"
    Write-Host "  BOT_API_KEY=/certs/bot-api.key"
    Write-Host "  BOT_API_CLIENT_CN=vrcverify-dashboard"
    Write-Host ""
    Write-Host "VPS (.env):"
    Write-Host "  BOT_API_CA=/certs/ca.pem"
    Write-Host "  BOT_API_CLIENT_CERT=/certs/dashboard.pem"
    Write-Host "  BOT_API_CLIENT_KEY=/certs/dashboard.key"
    Write-Host ""
    Write-Host "Keep ca.key here. It is the only thing that can mint a client the"
    Write-Host "bot will trust, and neither host ever needs it."
    Write-Host ""
    Write-Host "The .key files carry no ACL restriction on Windows — this machine is"
    Write-Host "the CA host, not a deploy target. Move them over SSH, don't sync them."
    Write-Host ""
    Write-Host "Verify the pair before deploying:"
    Write-Host "  openssl verify -CAfile ca.pem bot-api.pem dashboard.pem"
}
finally {
    Pop-Location
}
