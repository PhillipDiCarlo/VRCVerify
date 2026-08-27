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
 * The rest waits for the DOM, because it needs the placeholder to exist.
 *
 * IT BUILDS THE CONTROL RATHER THAN WIRING ONE (#195 phase 7). The picker used
 * to be a labelled <select> in the markup while the dashboard used an icon
 * opening a popover -- two affordances for the same three-way choice, met by
 * the same person within one click of each other. The dashboard's shape won
 * because it is the one that works with JavaScript off.
 *
 * Building it here rather than writing it into the six pages is not a
 * shortcut. The control cannot work without this file, so markup describing it
 * would describe something that may never exist -- and the header is
 * byte-identical across six hand-maintained pages, one of them generated, so
 * every line of it is a line to keep in step six times. The pages carry an
 * empty placeholder; everything else is here.
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

  /* ALL THREE WORDS ARE STAMPED AS THEMSELVES HERE, and that is the opposite
     of the dashboard, so do not copy a line of this across without reading
     both. There, "System" is the ABSENCE of the attribute, because its server
     always knows what to stamp before first paint. Here the absence is also
     what a reader sees before this file runs and forever with JavaScript off,
     so absence has to mean the default -- dark -- and System has to be an
     explicit `data-theme="system"` for the fourth cascade block to match.
     Mapping "system" to null here would silently pin every System reader to
     Dark on a light-OS machine. */
  apply(stored());

  /* Inline SVG for the same reason the dashboard uses it: the site's font has
     no sun, moon or monitor glyph, so a character would fall back to another
     family at another weight. Presentation attributes only -- there is no CSP
     on this host today, but the dashboard's forbids inline style and these
     icons are meant to be the same icons. */
  var NS = "http://www.w3.org/2000/svg";

  function svg(paths, cls, size) {
    var node = document.createElementNS(NS, "svg");
    node.setAttribute("class", cls);
    node.setAttribute("viewBox", "0 0 16 16");
    node.setAttribute("width", size);
    node.setAttribute("height", size);
    node.setAttribute("fill", "none");
    node.setAttribute("stroke", "currentColor");
    node.setAttribute("stroke-width", cls === "theme-tick" ? "2.2" : "1.6");
    node.setAttribute("stroke-linecap", "round");
    node.setAttribute("stroke-linejoin", "round");
    node.setAttribute("aria-hidden", "true");
    paths.forEach(function (spec) {
      var shape = document.createElementNS(NS, spec.tag);
      Object.keys(spec).forEach(function (key) {
        if (key !== "tag") {
          shape.setAttribute(key, spec[key]);
        }
      });
      node.appendChild(shape);
    });
    return node;
  }

  /* The same three paths the dashboard's theme_icon() macro draws. */
  var ICONS = {
    dark: [{ tag: "path", d: "M13.2 9.6A5.6 5.6 0 0 1 6.4 2.8a5.6 5.6 0 1 0 6.8 6.8z" }],
    light: [
      { tag: "circle", cx: "8", cy: "8", r: "3.1" },
      { tag: "path", d: "M8 1.4v1.5M8 13.1v1.5M2.3 8H.8M15.2 8h-1.5" +
          "M3.96 3.96 2.9 2.9M13.1 13.1l-1.06-1.06" +
          "M12.04 3.96 13.1 2.9M2.9 13.1l1.06-1.06" }
    ],
    system: [
      { tag: "rect", x: "1.6", y: "2.7", width: "12.8", height: "8.6", rx: "1.4" },
      { tag: "path", d: "M5.6 13.9h4.8" }
    ]
  };
  var TICK = [{ tag: "path", d: "M3 8.5l3.5 3.5L13 4.5" }];

  var OPTIONS = [
    ["dark", "Dark", "Always dark."],
    ["light", "Light", "Always light."],
    ["system", "System", "Matches the theme set on your device."]
  ];

  function build(host) {
    var current = stored() || "dark";

    var details = document.createElement("details");
    details.className = "theme-menu";

    var summary = document.createElement("summary");
    summary.className = "theme-button";
    summary.title = "Theme: " + current;
    summary.setAttribute(
      "aria-label", "Theme: " + current + ". Choose a different one."
    );
    summary.appendChild(svg(ICONS[current], "theme-mark", "16"));
    details.appendChild(summary);

    var panel = document.createElement("div");
    panel.className = "theme-panel";

    var label = document.createElement("p");
    label.className = "theme-panel-label";
    label.id = "theme-panel-label";
    label.textContent = "Appearance";
    panel.appendChild(label);

    var list = document.createElement("ul");
    list.setAttribute("aria-labelledby", "theme-panel-label");

    OPTIONS.forEach(function (option) {
      var key = option[0];
      var item = document.createElement("li");
      var button = document.createElement("button");
      button.type = "button";
      button.className = "theme-item" + (key === current ? " current" : "");
      if (key === current) {
        /* The accessible half of the tick: the class says "this one looks
           different", this says which is actually in force. */
        button.setAttribute("aria-current", "true");
      }
      button.appendChild(svg(ICONS[key], "theme-mark", "16"));

      var text = document.createElement("span");
      text.className = "theme-item-text";
      var name = document.createElement("span");
      name.className = "theme-item-label";
      name.textContent = option[1];
      var hint = document.createElement("span");
      hint.className = "theme-item-hint";
      hint.textContent = option[2];
      text.appendChild(name);
      text.appendChild(hint);
      button.appendChild(text);

      if (key === current) {
        button.appendChild(svg(TICK, "theme-tick", "13"));
      }

      button.addEventListener("click", function () {
        /* The word itself, including "system" -- see the note by the first
           apply() call. */
        apply(key);
        remember(key);
        /* Rebuilt rather than mutated, so the tick, the summary icon and both
           aria attributes cannot drift out of step with each other. */
        host.innerHTML = "";
        build(host);
      });

      item.appendChild(button);
      list.appendChild(item);
    });

    panel.appendChild(list);
    details.appendChild(panel);
    host.appendChild(details);
  }

  function wire() {
    var host = document.querySelector(".theme-picker");
    if (!host) {
      return;
    }
    build(host);
    host.hidden = false;
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
