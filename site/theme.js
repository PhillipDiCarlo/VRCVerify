/* The apex site's theme toggle -- the only script on this site (issue #137).
 *
 * WHY THIS IS SAFE TO HAVE AT ALL, on a site whose whole argument is that it
 * has no dependencies: it is served from this origin, it is thirty lines, and
 * nothing on any page needs it to work. Blocked, failed or disabled, every
 * page still renders, still reads, and still gets the default theme. The one
 * thing that disappears is the picker, which is why the picker ships `hidden`
 * and is revealed here rather than sitting in the markup looking clickable.
 *
 * TWO PHASES, AND THE FIRST ONE HAS TO BE SYNCHRONOUS.
 *
 * `apply()` runs immediately, from a blocking <script> in <head>, before the
 * body is parsed. That is what makes a stored Light choice paint light on the
 * first frame instead of flashing dark and correcting itself. It costs a
 * parse-blocking file on every page load; the file is tiny, same-origin, and
 * the alternative is a visible flash on every navigation for anybody who
 * chose Light. The dashboard avoids the tradeoff by rendering the attribute
 * server-side -- see issue #123 -- which is not available here.
 *
 * The rest waits for the DOM, because it needs the <select> to exist.
 *
 * NO ATTRIBUTE MEANS DARK, which is the site's default and the floor in
 * style.css. So "dark" is stored and stamped explicitly rather than being
 * represented by absence: a reader who picks Dark on a light-OS machine has
 * made a choice, and it has to survive a page load the same way the other two
 * do.
 */
(function () {
  "use strict";

  var KEY = "vrcverify-theme";
  var VALID = ["dark", "light", "system"];
  var root = document.documentElement;

  /* localStorage throws rather than returning null in a few real
     configurations -- Safari's private mode historically, and any browser set
     to block site data. A theme picker is not worth breaking a legal page
     over, so every access is guarded and failure means "no stored choice". */
  function stored() {
    try {
      var value = window.localStorage.getItem(KEY);
      return VALID.indexOf(value) === -1 ? null : value;
    } catch (e) {
      return null;
    }
  }

  function remember(value) {
    try {
      window.localStorage.setItem(KEY, value);
    } catch (e) {
      /* The choice still applies to this page; it just will not outlive it. */
    }
  }

  function apply(value) {
    if (value) {
      root.setAttribute("data-theme", value);
    } else {
      root.removeAttribute("data-theme");
    }
  }

  apply(stored());

  function wire() {
    var picker = document.querySelector(".theme-picker");
    var select = document.getElementById("theme-select");
    if (!picker || !select) {
      return;
    }
    /* Reflect what is actually in force before showing the control, so it
       never claims a theme the page is not wearing. With nothing stored that
       is the default, Dark. */
    select.value = stored() || "dark";
    picker.hidden = false;
    select.addEventListener("change", function () {
      if (VALID.indexOf(select.value) === -1) {
        return;
      }
      apply(select.value);
      remember(select.value);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
