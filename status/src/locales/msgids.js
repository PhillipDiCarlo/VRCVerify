/**
 * Every string the public page can say, in English. This file is the .pot.
 *
 * IT IS NOT WRITTEN BY HAND, which is the whole point. Two thirds of these are
 * read straight out of the modules that own them -- the component rows out of
 * config.js, the verdict sentences out of logic.js, the state words and the
 * freshness notes out of render.js -- so adding a sixth component or a fifth
 * dependency adds its msgids here by itself, and `every catalog covers every
 * msgid` in ../../test/i18n.test.js goes red until somebody translates them.
 * A hand-copied list would simply be a second thing to forget.
 *
 * The remainder are the literals passed to `t(...)` in render.js. Those cannot
 * be derived, so the same test re-reads render.js and fails if it finds one
 * that is not listed below. Between the two, a string cannot reach the page
 * without reaching the catalogs.
 *
 * NOTHING IMPORTS THIS AT RUNTIME. The Worker needs the catalogs, not the
 * inventory, and importing render.js from inside locales/ would close a cycle
 * (render -> i18n -> locales/index -> render). Only the test reads this.
 */

import { COMPONENTS, UPSTREAMS } from "../config.js";
import { HEADLINES } from "../logic.js";
import {
  FRESHNESS_HEADLINE,
  FRESHNESS_WARNING,
  STATE_LABEL,
  UPDATE_STATUS_LABELS,
} from "../render.js";

/**
 * Messages that count something.
 *
 * Listed apart because their catalog entries are OBJECTS keyed by plural
 * category rather than strings, and the completeness test has to know which
 * shape to demand. The key is the English singular, exactly as gettext uses
 * msgid rather than msgid_plural for the lookup.
 */
export const PLURAL_MSGIDS = [
  "%{count} day",
  "%{count} hour",
  "%{count} minute",
  "%{count} day ago",
  "%{count} open incident",
  "%{count} update",
  "%{count} earlier update",
  "%{percent}% uptime over the last %{days} days, with %{count} day affected",
];

/** Messages with one form. */
export const SINGULAR_MSGIDS = [
  // Chrome.
  "VRCVerify Status",
  "Whether VRCVerify's verification, bot, invites, dashboard and website are working, " +
    "and whether the services they depend on are.",
  "Home",
  "Dashboard",

  // The hero.
  ...Object.values(HEADLINES),
  ...Object.values(FRESHNESS_HEADLINE),
  "No check has completed yet.",
  "Checked %{ago} ago, at %{time}.",

  // The rows.
  ...Object.values(STATE_LABEL),
  ...COMPONENTS.flatMap((component) => [component.name, component.description]),
  "for %{duration}",
  "No history yet",
  "%{percent}% uptime over the last %{days} days, with no incidents",

  // The history strip's tooltips and its axis.
  "%{day}: maintenance all day",
  "%{day}: no data",
  "%{day}: %{percent}% up",
  " (%{duration} of maintenance not counted)",
  "Today",

  // Durations, for anything under a minute. The rest are plurals.
  "less than a minute",

  // Dependencies. The four company NAMES are absent on purpose: they are
  // proper nouns, they are what is written on the page being linked to, and a
  // reader looking for Discord is looking for the word Discord.
  "Services we depend on",
  ...UPSTREAMS.map((upstream) => upstream.why),
  "%{company}'s own status page",
  "Read from each company's own status feed. VRCVerify cannot fix these, and " +
    "when one of them is down our rows will usually follow.",

  // Incidents. The four update words come from render.js rather than from
  // `UPDATE_STATUSES` in logic.js: those are the enum the admin form posts and
  // the database stores, and what a reader sees is a separate, capitalized
  // set. "Resolved" appears once and is shared by the pill and the update
  // word, deliberately. See UPDATE_STATUS_LABEL.
  "Recent incidents",
  "Ongoing for %{duration}. Started %{time}.",
  "Resolved after %{duration}. Started %{time}.",
  ...UPDATE_STATUS_LABELS,
  "Nothing has gone wrong in the last %{days} days. Anything that does is " +
    "written up here, and stays.",

  // The page explaining itself.
  ...Object.values(FRESHNESS_WARNING),
  "Times are UTC. Everything is checked once a minute, and a problem has to show " +
    "up twice in a row before it is published here, so a fault takes about two " +
    "minutes to appear. Verification, the Discord bot and group invites report in " +
    "on their own schedule rather than being reached directly, which can take " +
    "about four. Recoveries are published as soon as they are seen. This page runs " +
    "on Cloudflare, separately from everything it reports on, so that it stays up " +
    "when they do not. Machine readable: %{link}.",

  // The footer.
  "What's new",
  "Terms of Service",
  "Privacy Policy",
  "Refund Policy",
  "Contact",
  "VRCVerify is operated by Esatto Technologies, United States.",
  "Not affiliated with, endorsed by, or sponsored by VRChat Inc. or Discord Inc.",
];

export const MSGIDS = [...SINGULAR_MSGIDS, ...PLURAL_MSGIDS];
