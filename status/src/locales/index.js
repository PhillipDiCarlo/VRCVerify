/**
 * The eleven catalogs, bundled into the Worker at deploy time.
 *
 * STATIC IMPORTS, NOT `import()`. A dynamic import here would be a network
 * round trip in the middle of rendering the page that says whether the network
 * is working, and it would fail in exactly the minute this page exists for.
 * Wrangler inlines all eleven into one script instead; they are a few tens of
 * kilobytes against a 3MB budget, and nothing is fetched at request time.
 *
 * ENGLISH IS ABSENT AND MUST STAY ABSENT. Its catalog is the msgids
 * themselves, so `CATALOGS.en` is undefined, every lookup misses, and every
 * message answers with its own key. A locales/en.js would be a file of entries
 * translating English into the same English, with each one a chance to drift.
 * Same rule as `translations/` in the dashboard, which also has no en_US.
 */

import ar from "./ar.js";
import bn from "./bn.js";
import de from "./de.js";
import esES from "./es-ES.js";
import hiIN from "./hi-IN.js";
import ja from "./ja.js";
import nl from "./nl.js";
import paIN from "./pa-IN.js";
import ptBR from "./pt-BR.js";
import ru from "./ru.js";
import zhCN from "./zh-CN.js";

export const CATALOGS = {
  ar,
  bn,
  de,
  "es-ES": esES,
  "hi-IN": hiIN,
  ja,
  nl,
  "pa-IN": paIN,
  "pt-BR": ptBR,
  ru,
  "zh-CN": zhCN,
};
