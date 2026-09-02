/* The header's status dot (issue #170).
 *
 * The second script on this site, and it holds to what the first one's comment
 * promised: served from this origin, small, and needed by nothing. Blocked,
 * failed or disabled, the pill is still a link to the status page with a
 * neutral dot, which is the whole of the feature for a reader without it.
 * That is why the pill ships visible, unlike the theme picker, which ships
 * hidden because a theme control that cannot work is worse than none.
 *
 * THE ONE RULE IT INHERITS: never green from missing data.
 *
 * The status page refuses to draw green from a reading it did not take, and a
 * dot on another origin quoting that page must refuse the same way or it is a
 * prettier version of the failure the whole project exists to avoid. So the
 * dot starts neutral and only a completed, fresh, understood reading moves it.
 * Every other path -- no fetch, a 500, a timeout, stale data, a shape this
 * code does not recognise -- leaves it exactly as it shipped.
 *
 * WHY THIS IS ALLOWED TO TALK TO ANOTHER ORIGIN AT ALL, on a site whose tests
 * say nothing is loaded from a third party: status.vrcverify.com is ours, the
 * request carries no credentials and no identifier, and it is made after the
 * page has already rendered completely. Nobody else learns anything and
 * nothing here waits for it. `tests/test_site.py` pins the one host this file
 * is allowed to name, so a second one cannot arrive quietly.
 */
(function () {
  "use strict";

  var ENDPOINT = "https://status.vrcverify.com/api/status.json";

  // Worst wins, in the status page's own order. `unknown` sits below
  // `degraded` there for a reason worth keeping here: "we could not check" is
  // a weaker claim than "we checked and it was bad", so one unreadable row
  // must not repaint a dot that five good rows have already earned.
  var SEVERITY = ["down", "degraded", "unknown", "up"];

  // What the mark says out loud. The colour is not the message: a reader who
  // cannot tell the green from the amber gets the same sentence either way.
  var SPOKEN = {
    up: "All systems operational",
    degraded: "Some services are degraded",
    down: "Some services are down",
  };

  // The status page's own glyphs, at its own proportions, so the mark here and
  // the mark one click away are the same mark. Copied rather than imported
  // because there is nothing to import from: that page renders on a Worker.
  //
  // THE SHAPE IS NOT DECORATION. --ok and --down sit 0.01 apart in relative
  // luminance, so as two coloured dots the working and down states are
  // identical to a red-green colourblind reader. The tick and the cross are
  // what actually distinguishes them.
  var GLYPH = {
    up: '<path d="M6.2 10.4l2.6 2.6 5.2-5.6"/><circle cx="10" cy="10" r="8.25"/>',
    degraded: '<path d="M10 5v6"/><path d="M10 14.5v.5"/><circle cx="10" cy="10" r="8.25"/>',
    down: '<path d="M6.5 6.5l7 7"/><path d="M13.5 6.5l-7 7"/><circle cx="10" cy="10" r="8.25"/>',
  };

  function worst(states) {
    for (var i = 0; i < SEVERITY.length; i++) {
      if (states.indexOf(SEVERITY[i]) !== -1) return SEVERITY[i];
    }
    return "unknown";
  }

  function paint(pill, state) {
    if (!SPOKEN[state]) return; // unknown included: leave the shipped dot alone.
    pill.setAttribute("data-state", state);
    var glyph = pill.querySelector(".status-glyph");
    // A fixed string from the constant above, never anything from the
    // response, so there is nothing here for a compromised endpoint to write.
    if (glyph) glyph.innerHTML = GLYPH[state];
    var said = pill.querySelector(".status-said");
    if (said) said.textContent = ". " + SPOKEN[state];
  }

  function read(body) {
    // A payload that is not the shape this was written against says nothing
    // about the services, so it is treated as no reading rather than as bad
    // news. The endpoint is versionless and may grow fields; it may also be a
    // captive portal answering 200 with a login page.
    if (!body || !Array.isArray(body.services) || body.services.length === 0) return null;
    // FRESHNESS IS THE READING'S EXPIRY DATE. The status page draws every row
    // as unknown when its own data is stale, because a checker that stopped an
    // hour ago is a photograph and not a measurement. A dot that stayed green
    // through that would contradict the page it links to, one click apart.
    if (body.freshness !== "fresh") return null;
    var states = [];
    for (var i = 0; i < body.services.length; i++) {
      var state = body.services[i] && body.services[i].state;
      if (typeof state !== "string") return null;
      states.push(state);
    }
    return worst(states);
  }

  function start() {
    var pill = document.querySelector(".status-pill");
    if (!pill || typeof fetch !== "function") return;

    fetch(ENDPOINT, {
      // No cookies, no credentials, nothing that could identify this reader to
      // the other origin. It is a public document and this is a public read.
      credentials: "omit",
      // The dot is decoration on a page that has already rendered. It is not
      // worth holding a connection open for, and a hanging request must not
      // become this site's slowest resource.
      // `typeof` rather than a plain property read: where AbortSignal does not
      // exist at all, `AbortSignal.timeout` is a ReferenceError rather than
      // undefined, and it would be thrown from inside this object literal
      // before fetch is ever called.
      signal:
        typeof AbortSignal !== "undefined" && AbortSignal.timeout
          ? AbortSignal.timeout(4000)
          : undefined,
    })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (body) {
        var state = read(body);
        if (state) paint(pill, state);
      })
      .catch(function () {
        // Deliberately silent, and deliberately doing nothing else. The status
        // page being unreachable is not news this site is qualified to break,
        // and the neutral dot already says everything true: not known.
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
