/* The only JavaScript in this application.
 *
 * WHY THIS EXISTS AT ALL
 * ----------------------
 * The settings page carries five independent forms -- verification, member,
 * logging, panel, and the panel-post action -- each with its own Save button.
 * Editing a field in one group and then clicking Save in another submits only
 * the second group, and the first group's edits are silently discarded. The
 * page reloads showing the old values, with no error, because nothing went
 * wrong: the browser did exactly what it was asked.
 *
 * That is a real way to lose work, and no amount of CSS can detect it. Every
 * other candidate for a script here (client-side validation, a colour preview,
 * copy buttons, filtering) was either already handled natively or would
 * duplicate a rule the bot owns, so this file does one thing.
 *
 * WHAT IT MAY NOT DO
 * ------------------
 * - No network. The CSP has no `connect-src`, so `fetch` and `XMLHttpRequest`
 *   are blocked by `default-src 'none'` -- deliberately. This file has no
 *   business talking to anything.
 * - No `innerHTML`, ever. Nothing here writes markup; it reads form state and
 *   sets one boolean. There is no path from server data to the DOM through
 *   this file, which is what keeps it uninteresting to an attacker.
 * - No authority. It cannot permit or prevent a save. The worst a tampered
 *   copy achieves is failing to warn, or warning when it needn't.
 *
 * PROGRESSIVE ENHANCEMENT
 * -----------------------
 * With JavaScript off, blocked, or still downloading, the page behaves exactly
 * as it did before this file existed. Nothing here is required to render, to
 * navigate, or to save. It is a warning, not a mechanism.
 */
(function () {
  "use strict";

  var forms = document.querySelectorAll("form[data-guard]");
  if (!forms.length) return;

  // Tracked per form, not as one page-wide flag. A single flag looks correct
  // and defeats the entire point: saving group B would clear the flag group A
  // set, so the one sequence this file exists to catch -- edit one group, save
  // another -- would pass silently, which is the behaviour without any script
  // at all.
  var dirty = [];

  function setDirty(form, isDirty) {
    var at = dirty.indexOf(form);
    if (isDirty && at === -1) dirty.push(form);
    if (!isDirty && at !== -1) dirty.splice(at, 1);
  }

  Array.prototype.forEach.call(forms, function (form) {
    function mark() {
      setDirty(form, true);
    }

    // `input` covers typing; `change` covers selects, checkboxes and the
    // colour picker, which do not fire `input` consistently across the range
    // of browsers this has to work in.
    form.addEventListener("input", mark);
    form.addEventListener("change", mark);

    // Only *this* form stops being dirty. The submit is about to navigate, so
    // if another group still holds unsaved edits the warning below is exactly
    // right -- that navigation is what would discard them.
    form.addEventListener("submit", function () {
      setDirty(form, false);
    });
  });

  window.addEventListener("beforeunload", function (event) {
    if (!dirty.length) return;
    // The browser shows its own wording; ours is ignored by every current
    // engine. Both lines are still required for the prompt to appear at all.
    event.preventDefault();
    event.returnValue = "";
  });
})();
