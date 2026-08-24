/* Display preferences and the bar's menus. The second script in this app.
 *
 * TWO JOBS, AND THEY ARE RELATED
 * ------------------------------
 * 1. Menu dismissal, for every <details class="bar-menu"> in the header --
 *    the theme picker, the account menu, and #136's notification panel when it
 *    arrives. Written over the class rather than per menu, so a new menu gets
 *    outside-click and Escape by wearing it, and opening one closes the rest.
 * 2. The theme, applied without a navigation.
 *
 * WHY IT IS A SEPARATE FILE FROM app.js
 * -------------------------------------
 * app.js is documented as doing exactly one thing -- noticing you edited one
 * settings group and saved another -- and it is worth keeping that true. This
 * file is the other thing: the small set of preferences the browser can store
 * on its own, which today is the theme and, since #136, which changelog
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
 * three submit buttons posting to /prefs/theme and the account menu is a
 * <details> containing two forms. Both work completely. This file removes a
 * navigation from one and adds a nicety to the other; it does not make either
 * possible. If it fails to load, an open menu closes when you click its button
 * again rather than when you click away -- and nothing else changes.
 */
(function () {
  "use strict";

  // The one place cookie writing happens. #136 uses this too -- for which
  // entry the bell has been seen up to, and which cards have been dismissed --
  // which is why it takes a name rather than assuming the theme.
  //
  // `Secure` is conditional, not absent (#161). It used to be omitted outright,
  // with a comment claiming this only ran on the plain-HTTP loopback preview.
  // It does not: it runs on every instant theme change in production, so the
  // first time anyone switched theme, the server's Secure cookie was replaced
  // by a non-Secure one with a year to live. The reasoning was sound and the
  // placement was wrong -- the exception belongs to the preview, so it is
  // written as one. On https this now matches what the server sets; on
  // loopback the preview keeps working.
  var YEAR = 31536000;
  var SECURE = location.protocol === "https:" ? "; Secure" : "";

  function writeCookie(name, value) {
    var base = encodeURIComponent(name) + "=";
    if (value === null) {
      // Deleting has to carry the same attributes as writing, Secure
      // included: a browser matches on name, path and domain, but a cookie
      // set Secure is not overwritten by a non-Secure one of the same name.
      document.cookie = base + "; Max-Age=0; Path=/; SameSite=Lax" + SECURE;
      return;
    }
    document.cookie =
      base + encodeURIComponent(value) +
      "; Max-Age=" + YEAR + "; Path=/; SameSite=Lax" + SECURE;
  }

  // Must agree with THEMES and THEME_DEFAULT in app.py. If they ever disagree
  // the server wins on the next load, so the failure is a flicker rather than
  // a wrong page -- but they should not disagree.
  var THEMES = ["dark", "light", "system"];
  var DEFAULT = "dark";

  // --- every menu in the bar -------------------------------------------
  //
  // Dismissal is a property of being a menu, not of being the theme picker, so
  // it is written once over `.bar-menu` and the account menu and #136's
  // notification panel get it by wearing the class. It also means opening one
  // closes the other, which is what a row of menus is expected to do.
  function menus() {
    return document.querySelectorAll("details.bar-menu");
  }

  // Clicking outside an open menu closes it. `<details>` alone cannot do this,
  // and without it the only way to dismiss a menu without choosing is to click
  // its button again -- which reads as the control being stuck.
  //
  // Clicking one menu's button counts as outside the other, which is how
  // opening one closes the rest.
  document.addEventListener("click", function (event) {
    menus().forEach(function (menu) {
      if (menu.hasAttribute("open") && !menu.contains(event.target)) {
        menu.removeAttribute("open");
      }
    });
  });

  // Escape closes them, which is what every other menu on the web does, and
  // puts focus back on the button that opened it rather than dropping it to
  // the top of the document.
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    menus().forEach(function (menu) {
      if (!menu.hasAttribute("open")) return;
      menu.removeAttribute("open");
      var summary = menu.querySelector("summary");
      if (summary) summary.focus();
    });
  });

  // --- the bell's unread dot (#136) -------------------------------------
  //
  // MUST STAY ABOVE the theme block below, which early-returns out of this
  // IIFE when it finds no picker. Anything placed after it runs only on pages
  // that have one.
  //
  // The same bargain as everything else in this file: it makes the dot behave
  // the way a notification dot is expected to, it does not make the panel
  // work. With this blocked the panel still opens -- it is a <details> -- and
  // "Mark all as read" in its footer posts to /prefs/seen and clears the dot
  // the ordinary way. That button is not a fallback; it is there either way.
  //
  // No fetch, and not for want of trying: the CSP is `default-src 'none'`
  // with no `connect-src`, so an XHR from this page is blocked outright. The
  // cookie is the only channel a script has to tell the server something, and
  // that is exactly why SEEN_COOKIE is not httponly.
  var bell = document.querySelector("details.bell");
  if (bell) {
    bell.addEventListener("toggle", function () {
      if (!bell.hasAttribute("open")) return;

      var newest = bell.getAttribute("data-newest");
      if (!newest) return;
      writeCookie("vrcverify_seen", newest);

      // Drop the dot now rather than at the next navigation. Leaving it lit
      // over a panel the reader is looking at is the one state that makes the
      // indicator feel broken -- and the server has already been told, so the
      // page and the cookie do not disagree.
      var dot = bell.querySelector(".bell-dot");
      if (dot) dot.remove();

      // The summary's accessible name carries the same claim in words, so it
      // has to lose the sentence at the same moment the dot goes.
      var summary = bell.querySelector("summary");
      if (summary) summary.setAttribute("aria-label", "What's new");
    });
  }

  // --- the theme, made instant ------------------------------------------

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
})();
