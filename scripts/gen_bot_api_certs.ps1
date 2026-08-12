# Issues the certificates the bot API and the dashboard authenticate each other
# with (issue #65). A direct port of gen_bot_api_certs.sh — read that one for
# why a private CA exists at all, and for BOTH rotation procedures: the
# certificates this script issues, and BOT_API_TOKEN_SIGNING_KEY, which it does
# not issue but which is the other half of the same trust chain. Keep the two
# scripts in step if you change either.
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
    # A CA with no keyUsage is accepted by openssl, by curl, and by the bot's own
    # server context -- and rejected by the dashboard, because Python 3.13 turned
    # on ssl.VERIFY_X509_STRICT by default in create_default_context(). Under RFC
    # 5280 strict checking a CA must say keyCertSign, so an extension-free CA
    # fails with "CA cert does not include key usage extension", client side only.
    $caExts = @(
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        "-addext", "subjectKeyIdentifier=hash"
    )

    Write-Host "==> Certificate authority"
    $issueCa = $true
    if ((Test-Path ca.key) -and (Test-Path ca.pem)) {
        # Prints nothing and exits 0 when the extension is absent, which is
        # precisely the case being detected.
        $caKeyUsage = openssl x509 -in ca.pem -noout -ext keyUsage
        if ($caKeyUsage -match "Certificate Sign") {
            Write-Host "    ca.key exists; reusing it (rotating leaves only)."
            $issueCa = $false
        } else {
            # Re-signing with the SAME key and the SAME subject: the CA
            # certificate only publishes a public key and a set of extensions,
            # so every certificate the old one signed still verifies against the
            # new one. Nothing needs reissuing and the hosts update in either
            # order.
            Write-Host "    ca.pem predates the keyUsage fix; re-issuing it from the existing key."
        }
    }
    if ($issueCa) {
        if (-not (Test-Path ca.key)) { openssl genrsa -out ca.key 4096 }
        openssl req -x509 -new -nodes -key ca.key -sha256 -days 3650 `
            -subj "/CN=VRCVerify Internal CA" @caExts -out ca.pem
    }

    Write-Host "==> Server certificate for the bot API ($san)"
    $serverExt = New-TemporaryFile
    @(
        "subjectAltName=$san",
        "extendedKeyUsage=serverAuth",
        "basicConstraints=critical,CA:FALSE",
        "keyUsage=critical,digitalSignature,keyEncipherment",
        "subjectKeyIdentifier=hash",
        "authorityKeyIdentifier=keyid:always"
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
        "basicConstraints=critical,CA:FALSE",
        "keyUsage=critical,digitalSignature",
        "subjectKeyIdentifier=hash",
        "authorityKeyIdentifier=keyid:always"
    ) | Set-Content -Path $clientExt -Encoding ascii

    openssl genrsa -out dashboard.key 4096
    openssl req -new -key dashboard.key -subj "/CN=vrcverify-dashboard" -out dashboard.csr
    openssl x509 -req -in dashboard.csr -CA ca.pem -CAkey ca.key -CAcreateserial `
        -out dashboard.pem -days 825 -sha256 -extfile $clientExt

    Remove-Item bot-api.csr, dashboard.csr, $serverExt, $clientExt -Force

    # Run the strict check here rather than suggesting it, because the lenient
    # one is worse than no check at all: `openssl verify` passes on a chain the
    # dashboard will reject, so it reads as proof the certificates are good. The
    # failure it hides surfaces later as a TLS error at request time, on the box
    # where the CA key isn't, with no matching log line on the bot.
    Write-Host ""
    Write-Host "==> Verifying the chain the way Python will"
    openssl verify -x509_strict -CAfile ca.pem bot-api.pem dashboard.pem
    if ($LASTEXITCODE -ne 0) { throw "Strict verification failed; do not deploy these." }

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
}
finally {
    Pop-Location
}
