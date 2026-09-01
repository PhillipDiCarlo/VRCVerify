/**
 * Cloudflare Access, verified here rather than assumed (issue #170 phase 5).
 *
 * WHY THE WORKER CHECKS A TOKEN THAT THE EDGE ALREADY CHECKED
 *
 * Access sits in front of this hostname and stops unauthenticated requests
 * before they reach the script. That is the control. This is the second one,
 * and it exists because the first is a setting in a dashboard: it can be
 * scoped to the wrong path, disabled by somebody tidying up, or silently not
 * cover a route added later. None of that is visible in a diff, and the
 * failure is not an error page -- it is a world-writable form that posts
 * announcements to a page people trust. Somebody would find it.
 *
 * So the route requires a valid, unexpired, correctly-audienced Access JWT,
 * verified against the team's published keys. Belt and braces, where the
 * braces are the ones in version control.
 *
 * IF IT IS NOT CONFIGURED, THE ROUTE DOES NOT EXIST. With ACCESS_AUD unset
 * /admin is a 404 rather than an open door. A deploy that has not been given
 * an Access policy has no admin surface at all, which is the correct default
 * for a feature whose whole risk is being reachable.
 */

/**
 * The team's signing keys, cached for the lifetime of the isolate.
 *
 * Cloudflare rotates these, so the cache has a short life and a miss simply
 * fetches again. Not cached across isolates on purpose: a KV round trip to
 * save an occasional fetch would add a dependency to the path whose entire job
 * is refusing people.
 */
let keyCache = { at: 0, keys: null, team: null };

async function fetchKeys(teamDomain) {
  const now = Date.now();
  if (keyCache.keys && keyCache.team === teamDomain && now - keyCache.at < 3600_000) {
    return keyCache.keys;
  }
  const response = await fetch(`https://${teamDomain}/cdn-cgi/access/certs`, {
    signal: AbortSignal.timeout(8000),
  });
  if (!response.ok) throw new Error(`access certs: HTTP ${response.status}`);
  const body = await response.json();
  const keys = body?.keys ?? [];
  keyCache = { at: now, keys, team: teamDomain };
  return keys;
}

function base64UrlToBytes(value) {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(padded + "=".repeat((4 - (padded.length % 4)) % 4));
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

function decodeSegment(segment) {
  return JSON.parse(new TextDecoder().decode(base64UrlToBytes(segment)));
}

/**
 * Returns the caller's email on success, or null. Never throws, and never
 * explains: the distinction between "expired" and "wrong audience" is
 * interesting to an operator and useful to an attacker, so it goes to the log
 * and not to the response.
 */
export async function verifyAccessToken(token, { teamDomain, audience, now }) {
  if (!token || !teamDomain || !audience) return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;

  try {
    const header = decodeSegment(parts[0]);
    const payload = decodeSegment(parts[1]);

    // Checked BEFORE the signature, because these are the cheap ones and
    // because `alg: none` must never reach a verifier.
    if (header.alg !== "RS256") return null;
    if (payload.iss !== `https://${teamDomain}`) return null;
    const audiences = Array.isArray(payload.aud) ? payload.aud : [payload.aud];
    if (!audiences.includes(audience)) return null;
    if (typeof payload.exp !== "number" || payload.exp <= now) return null;
    // A token issued in the future is a clock problem or a forgery attempt;
    // either way it is not one to act on. Sixty seconds of tolerance.
    if (typeof payload.iat === "number" && payload.iat > now + 60) return null;

    const keys = await fetchKeys(teamDomain);
    const jwk = keys.find((candidate) => candidate.kid === header.kid);
    if (!jwk) return null;

    const key = await crypto.subtle.importKey(
      "jwk",
      jwk,
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
      false,
      ["verify"],
    );
    const valid = await crypto.subtle.verify(
      "RSASSA-PKCS1-v1_5",
      key,
      base64UrlToBytes(parts[2]),
      new TextEncoder().encode(`${parts[0]}.${parts[1]}`),
    );
    if (!valid) return null;

    return payload.email ?? "unknown";
  } catch (error) {
    console.log(`access token rejected: ${error?.name}: ${error?.message}`);
    return null;
  }
}
