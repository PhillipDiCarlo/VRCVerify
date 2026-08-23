/* Display preferences, made instant. The second script in this application.
 *
 * WHY IT IS A SEPARATE FILE FROM app.js
 * -------------------------------------
 * app.js is documented as doing exactly one thing -- noticing you edited one
 * settings group and saved another -- and it is worth keeping that true. This
 * file is the other thing: the small set of preferences the browser can store
 * on its own, which today is the theme and in #136 will be which changelog
 * entries have been seen.
 *
 * WHAT IT MAY NOT DO
 * ------------------
 * - No network. The CSP has no `connect-src`, so `fetch` and `XMLHttpRequest`
 *   are blocked by `default-src 'none'`. That is not an obstacle here, it is
 *   the design: a preference the page can write itself needs no server.
 * - No `innerHTML`, ever. Nothing here writes markup.
 * - No authority. Every value it writes is checked again on the server before
 *   it can reach a page, and the worst a tampered copy achieves is a colour
 *   the reader did not ask for on their own screen.
 *
 * PROGRESSIVE ENHANCEMENT
 * -----------------------
 * With JavaScript off, blocked, or still downloading, the theme picker is
 * three submit buttons posting to /prefs/theme, and it works completely. This
 * file removes the navigation, not the capability. If it fails to load, the
 * only difference is a page reload the reader would not have noticed anyway.
 */
(function () {
  "use strict";

  // The one place cookie writing happens. #136 uses this too, which is why it
  // takes a name rather than assuming the theme.
  //
  // No `Secure` here even though the server sets it: this only ever runs on a
  // page already served over HTTPS in production, and adding it would make the
  // preview on plain-HTTP loopback silently stop working -- which is exactly
  // the bug that made phase 3 look broken on first click.
  var YEAR = 31536000;

  function writeCookie(name, value) {
    var base = encodeURIComponent(name) + "=";
    if (value === null) {
      document.cookie = base + "; Max-Age=0; Path=/; SameSite=Lax";
      return;
    }
    document.cookie =
      base + encodeURIComponent(value) +
      "; Max-Age=" + YEAR + "; Path=/; SameSite=Lax";
  }

  // Must agree with THEMES and THEME_DEFAULT in app.py. If they ever disagree
  // the server wins on the next load, so the failure is a flicker rather than
  // a wrong page -- but they should not disagree.
  var THEMES = ["dark", "light", "system"];
  var DEFAULT = "dark";

  var picker = document.querySelector("details.theme");
  if (!picker) return;

  var form = picker.querySelector("form");
  if (!form) return;

  form.addEventListener("click", function (event) {
    var button = event.target.closest("button[name='theme']");
    if (!button) return;

    var chosen = button.value;
    if (THEMES.indexOf(chosen) === -1) return; // let the server deal with it

    // Only now is the navigation unnecessary.
    event.preventDefault();

    // "System" is the ABSENCE of the attribute, not a value of it -- the
    // stylesheet's third block is :root:not([data-theme]). Setting it to
    // "system" would match none of the three and pin the page to the light
    // floor. Same rule as theme_attr() in app.py.
    if (chosen === "system") {
      document.documentElement.removeAttribute("data-theme");
    } else {
      document.documentElement.setAttribute("data-theme", chosen);
    }

    // Dark is what no cookie already means, so choosing it clears rather than
    // stores. Mirrors set_theme_preference exactly; two ways of saying the
    // same thing is a thing to keep in agreement forever.
    writeCookie("vrcverify_theme", chosen === DEFAULT ? null : chosen);

    // The tick and the summary icon are server-rendered, so they are now one
    // navigation out of date. Rather than rebuild that markup here -- which
    // would mean this file generating DOM, the thing it is not allowed to do
    // -- the menu simply closes, and the next page load renders it correctly.
    picker.removeAttribute("open");
  });

  // Clicking outside an open menu closes it. `<details>` alone cannot do this,
  // and without it the only way to dismiss the menu without choosing is to
  // click the button again -- which reads as the control being stuck.
  document.addEventListener("click", function (event) {
    if (picker.hasAttribute("open") && !picker.contains(event.target)) {
      picker.removeAttribute("open");
    }
  });

  // Escape closes it, which is what every other menu on the web does.
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && picker.hasAttribute("open")) {
      picker.removeAttribute("open");
      var summary = picker.querySelector("summary");
      if (summary) summary.focus();
    }
  });
})();
