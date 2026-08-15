<#
.SYNOPSIS
    Tests SECURITY_AUDIT A-25: that Cloudflare's managed challenge does not
    cover POST /stripe/webhook, while still covering the paths that need it.

.DESCRIPTION
    A-25: a managed challenge covering the whole hostname blocks Stripe's
    server-to-server POST. Stripe sees 403, retries for three days, and
    NOTHING reaches the origin -- so nothing appears in the application logs
    either. A subscription completes at checkout and premium never switches on.

    WHAT A RED RESULT ON /stripe/webhook DOES AND DOES NOT PROVE. This script
    sends Stripe's User-Agent from wherever you run it, which is not Stripe's
    network. Cloudflare identifies verified bots by IP/ASN, not by the UA
    string, and its verified-bot list has included Stripe's webhook
    infrastructure -- so genuine Stripe deliveries may be exempt from a
    challenge that this probe still meets. The probe therefore errs pessimistic
    and CANNOT distinguish "Stripe would be blocked" from "Stripe would be
    waved through". That is deliberate, and it is A-25's own instruction: do
    not rely on the verified-bots list, make it explicit. Only a real
    test-mode delivery settles it.

    Two things this script exists to stop you getting wrong:

    1. THE CHALLENGE IS BOT-SCORE DRIVEN, NOT BLANKET. Measured 2026-08-15: a
       request with User-Agent "curl-test" reached the origin on /stripe/webhook
       while Stripe's own User-Agent was challenged on the same path, same
       second. So a probe with an arbitrary agent can come back clean while the
       skip rule does not exist at all, and you would be measuring "this client
       was never challenged" and recording it as "the rule works". Every probe
       here therefore sends STRIPE'S REAL USER-AGENT.

    2. "THE WEBHOOK PATH IS CLEAN" IS HALF A PASS. Turning the challenge off
       and stopping there also makes /stripe/webhook clean, by removing the
       protection A-25 never asked you to remove. So /login and /callback are
       probed and REQUIRED to still be challenged: they are the unauthenticated
       endpoints the challenge exists for. A run where the webhook path is clean
       and those two are also clean is a regression wearing a green tick.

    Run it BEFORE the change to confirm the baseline, and AFTER to confirm the
    fix. Expectations match the Bot Fight Mode replacement described below; if
    the edge configuration changes again, these change with it.

    This tests the EDGE only, and needs no Stripe credential and no
    STRIPE_ENABLED. With the switch off the origin answers /stripe/webhook with
    its own 404, which is a pass: a 404 from the app proves the bytes crossed
    the edge. Sending a real test-mode event is the separate, later step.

.PARAMETER Hostname
    Defaults to the production dashboard.

.EXAMPLE
    .\scripts\test_a25_webhook_skip.ps1
#>

[CmdletBinding()]
param(
    [string]$Hostname = "dashboard.vrcverify.com"
)

# Stripe's real webhook agent. Do not "simplify" this to curl's default -- see
# note 1 in the description; the whole test turns on sending an agent that the
# challenge actually fires on.
$StripeAgent = "Stripe/1.0 (+https://stripe.com/docs/webhooks)"

# path -> should this path be challenged once the fix is in place?
#
# These expectations are for the BOT-FIGHT-MODE REPLACEMENT model, adopted
# 2026-08-15. Security Events showed the challenge came from Bot Fight Mode,
# which is the free tier's zone-wide on/off toggle: it has no per-path
# exceptions, and a WAF custom rule with a Skip action does NOT apply to it
# (Skip can target *Super* Bot Fight Mode, which is a paid feature). So the
# skip rule A-25 prescribes was not available, and the fix inverts instead --
# Bot Fight Mode off, replaced by an explicit Managed Challenge on the only
# two paths SECURITY_AUDIT section 4.2 says it was earning its place on:
#
#   (http.host eq "dashboard.vrcverify.com"
#    and http.request.uri.path in {"/login" "/callback"})
#
# That satisfies A-25's "make it explicit" and leaves /stripe/webhook clean by
# construction rather than by exception -- there is no skip expression left to
# write too broadly.
#
# The assertions therefore changed shape. Under the old skip-rule model the
# decoy /stripe/webhook-x caught a `contains` expression; here it instead
# confirms the challenge is NOT applied outside the two named paths. `/` does
# the same job for the site root.
$Probes = [ordered]@{
    "/stripe/webhook"   = $false   # the A-25 goal: Stripe must reach the origin
    "/login"            = $true    # the control must survive the swap
    "/callback"         = $true    # the other unauthenticated path
    "/"                 = $false   # proves the rule is not hostname-wide
    "/stripe/webhook-x" = $false   # nothing outside the two paths is challenged
}

function Get-ChallengeState {
    param([string]$Url)

    $headers = curl.exe -sS -D - -o NUL -X POST $Url `
        -H "User-Agent: $StripeAgent" `
        -H "Content-Type: application/json" `
        -d "{}" --max-time 20

    $joined = ($headers | Out-String)
    $status = ($headers | Select-String -Pattern '^HTTP/' | Select-Object -First 1).Line
    if ($status) { $status = $status.Trim() } else { $status = "(no status line)" }

    [pscustomobject]@{
        Status     = $status
        Challenged = $joined -match '(?im)^Cf-Mitigated:\s*challenge'
    }
}

Write-Host ""
Write-Host "A-25 -- Cloudflare challenge coverage on dashboard paths" -ForegroundColor Cyan
Write-Host "Host: $Hostname"
Write-Host "Agent: $StripeAgent"
Write-Host ""

$failures = @()

foreach ($path in $Probes.Keys) {
    $expectChallenge = $Probes[$path]
    $result = Get-ChallengeState -Url "https://$Hostname$path"

    $ok = ($result.Challenged -eq $expectChallenge)
    if (-not $ok) { $failures += $path }

    $observed = if ($result.Challenged) { "challenged" } else { "reached origin" }
    $wanted   = if ($expectChallenge)   { "challenged" } else { "reached origin" }
    $verdict  = if ($ok) { "ok  " } else { "FAIL" }
    $colour   = if ($ok) { "Green" } else { "Red" }

    Write-Host ("  {0}  {1,-20} {2,-24} want {3,-14} {4}" -f `
        $verdict, $path, $observed, $wanted, $result.Status) -ForegroundColor $colour
}

Write-Host ""
if ($failures.Count -eq 0) {
    Write-Host "PASS -- webhook path reaches the origin; /login and /callback still challenged." -ForegroundColor Green
    Write-Host ""
    Write-Host "CONFIRM WHAT IS CHALLENGING /login BEFORE TRUSTING THIS. A-14 also put a" -ForegroundColor Yellow
    Write-Host "rate limit on /login and /callback. If that rule's action is Managed" -ForegroundColor Yellow
    Write-Host "Challenge, a repeated run of this script trips it, and those two paths go" -ForegroundColor Yellow
    Write-Host "green whether or not the custom rule is deployed -- the same green, for the" -ForegroundColor Yellow
    Write-Host "wrong reason. Check Security -> Events and confirm the matching service is" -ForegroundColor Yellow
    Write-Host "Custom rules, not Rate limiting rules." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Next: send a real test-mode event from Stripe (A-25 is not done until an"
    Write-Host "actual delivery lands; this only proves the edge will let one through)."
    exit 0
}

Write-Host "FAIL -- $($failures.Count) of $($Probes.Count) probes wrong: $($failures -join ', ')" -ForegroundColor Red
Write-Host ""
if ($failures -contains "/stripe/webhook") {
    Write-Host "  /stripe/webhook is CHALLENGED. Either Bot Fight Mode is still on (check" -ForegroundColor Yellow
    Write-Host "  Security -> Bots), or the replacement rule's path set is wider than" -ForegroundColor Yellow
    Write-Host '  {"/login" "/callback"}. This is the A-25 failure itself.' -ForegroundColor Yellow
}

$lostControl = $failures | Where-Object { $_ -in @("/login", "/callback") }
if ($lostControl) {
    Write-Host "  $($lostControl -join ' and ') NOT challenged. Bot Fight Mode is off but the" -ForegroundColor Red
    Write-Host "  replacement rule is not doing its job -- check it is deployed, Active, and" -ForegroundColor Red
    Write-Host "  that the action is Managed Challenge. These two paths are unauthenticated;" -ForegroundColor Red
    Write-Host "  right now only the A-14 rate limit is in front of them." -ForegroundColor Red
}

$tooBroad = $failures | Where-Object { $_ -in @("/", "/stripe/webhook-x") }
if ($tooBroad) {
    Write-Host "  $($tooBroad -join ' and ') challenged, but should not be. Either Bot Fight" -ForegroundColor Yellow
    Write-Host "  Mode is still on, or the rule matches more than the two named paths. Use" -ForegroundColor Yellow
    Write-Host '    http.request.uri.path in {"/login" "/callback"}' -ForegroundColor Yellow
    Write-Host "  and not a contains/starts_with form." -ForegroundColor Yellow
}
exit 1
