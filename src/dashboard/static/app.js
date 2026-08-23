/* One of the two scripts in this application; prefs.js is the other.
 *
 * WHY THIS EXISTS AT ALL
 * ----------------------
 * The settings page carries six independent forms -- verification, member,
 * logging, panel, group, and the panel-post action -- each with its own Save
 * button.
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
 * It now says so on the page as well as in a prompt on the way out. #133 phase
 * 4 replaced the checkboxes with toggle switches, and a switch is a much
 * stronger promise than a checkbox: everywhere else on the web, flipping one
 * saves it. This one cannot -- there is no `connect-src`, so saving means
 * submitting the form. Marking the group that holds unsaved edits is what
 * keeps that promise honest, which makes it part of the switches rather than a
 * decoration on top of them.
 *
 * WHAT IT MAY NOT DO
 * ------------------
 * - No network. The CSP has no `connect-src`, so `fetch` and `XMLHttpRequest`
 *   are blocked by `default-src 'none'` -- deliberately. This file has no
 *   business talking to anything.
 * - No `innerHTML`, ever. Nothing here writes markup. It reads form state,
 *   sets one boolean, and toggles one class name that the stylesheet already
 *   knows about; the indicator it reveals is rendered by the template on every
 *   load. There is still no path from server data to the DOM through this
 *   file, which is what keeps it uninteresting to an attacker.
 * - No authority. It cannot permit or prevent a save. The worst a tampered
 *   copy achieves is failing to warn, or warning when it needn't.
 *
 * PROGRESSIVE ENHANCEMENT
 * -----------------------
 * With JavaScript off, blocked, or still downloading, the page behaves exactly
 * as it did before this file existed: the switches work, the forms save, and
 * there is no unsaved marker -- which is no worse than the checkboxes were.
 * Nothing here is required to render, to navigate, or to save. It is a
 * warning, not a mechanism.
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
    // The visible half. Same per-form state, said on the page instead of only
    // on the way out -- the `beforeunload` prompt below is unchanged and still
    // the last line of defence.
    form.classList.toggle("is-dirty", isDirty);
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
