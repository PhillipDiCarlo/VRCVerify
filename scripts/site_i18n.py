"""Find every translatable sentence in the apex site's HTML, by byte offset.

WHY OFFSETS AND NOT A PARSE-AND-REWRITE
---------------------------------------
The six pages under `site/` are hand-written and heavily commented: the
reasoning for the status dot, for why Terms left the nav, for what `home` on
`<body>` unlocks. Parsing them into a tree and serialising it back would
produce valid HTML and throw all of that away, and the diff would be
unreviewable.

So nothing here rewrites a document. This module reports WHERE each
translatable span sits, as a `(start, end)` pair into the original string, and
the generator splices replacements in from the right so earlier offsets stay
valid. Everything not spliced -- comments, indentation, attribute order --
comes out byte for byte. `test_the_splice_is_lossless` pins that.

WHY A BLOCK IS THE UNIT, NOT A TEXT NODE
----------------------------------------
The first version of this extracted text nodes. 61% of them turned out to sit
next to inline markup, so a translator was handed things like

    "— including how to ask for your data, or ask us to delete it"

severed from the `<a>` it follows, and `&#39;` split "What's new" into "What"
and "s new". Nothing correct can be built from that: the languages this site
is being translated into do not keep English's word order, so a sentence
cut at its links is a sentence that cannot be reassembled.

The unit is therefore the innermost BLOCK element, and the inline elements
inside it survive into the msgid as numbered placeholders:

    You must comply with the <0>Discord Terms of Service</0>.

A translator moves `<0>...</0>` wherever their language puts it. The
generator restores the original tag, with its href and its classes, from the
position of the placeholder. So a URL is never exposed to translation and
never can be broken by it.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

# Text inside these is never prose.
#
# `time` IS HERE AND NOT IN INLINE, which is worth the sentence. A `<time>`
# holds a machine value rendered for people, and `gen_site_locales.localize_dates`
# already rewrites it per locale from the `datetime` attribute. Leaving it
# translatable made the date part of the MSGID, so the changelog badge came out
# as `<0>New</0> <1>Sep 13, 2026</1>` -- a different msgid for every entry, and
# eleven newly untranslated strings every time one shipped. As a placeholder it
# is one msgid forever, and a translator cannot reach the date to break it.
OPAQUE = {"script", "style", "svg", "code", "pre", "time"}

# The innermost one of these that holds words is one translatable unit.
BLOCKS = {
    "p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th", "dt", "dd",
    "figcaption", "summary", "caption", "title", "button", "label", "legend",
    "blockquote", "option",
}

# Kept inside the msgid as <0>...</0>. Anything else ends the span.
INLINE = {"a", "strong", "em", "b", "i", "span", "abbr", "small", "sup", "sub", "br"}

# Attributes holding a sentence somebody reads or hears.
TRANSLATABLE_ATTRS = ("alt", "title", "aria-label")

HAS_WORDS = re.compile(r"[A-Za-z]{2}")

# Strings that are never translated even when they are the whole of a block.
# Each is a proper noun, an address, or a machine format.
NEVER = re.compile(
    r"""^(?:
        VRCVerify | VRChat | Discord | Stripe | Cloudflare | Premium
      | [\w.+-]+@[\w.-]+                                  # an email address
      | https?://\S+                                      # a URL
      | [A-Z][a-z]{2}\ \d{1,2},\ \d{4}                     # Sep 10, 2026
    )$""",
    re.X,
)


def _line_starts(source: str) -> list[int]:
    starts, at = [0], 0
    for line in source.splitlines(keepends=True):
        at += len(line)
        starts.append(at)
    return starts


class _Collector(HTMLParser):
    """Records the inner span of every innermost block that holds words."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self._lines = _line_starts(source)
        # (tag, inner_start, had_nested_block, saw_words)
        self.open: list[list] = []
        self.opaque = 0
        self.no_translate = 0
        self.found: list[tuple[int, int, str, str]] = []

    def _offset(self) -> int:
        line, col = self.getpos()
        return self._lines[line - 1] + col

    def _end_of_starttag(self) -> int:
        return self._offset() + len(self.get_starttag_text() or "")

    # -- tags ------------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        # INSIDE AN OPAQUE ELEMENT, NOTHING COUNTS. The status pill is
        # `<a><svg><circle/></svg><span>Status</span></a>`, and `circle` is
        # neither block nor inline, so without this it marked the enclosing
        # `<a>` as containing a nested block and made the whole pill
        # untranslatable. The svg was already exempt; its children were not.
        if self.opaque:
            if tag in OPAQUE:
                self.opaque += 1
            return
        d = dict(attrs)
        self._attrs(d, tag)
        if d.get("translate") == "no":
            self.no_translate += 1
        if tag in OPAQUE:
            self.opaque += 1
        if tag in BLOCKS:
            for frame in self.open:
                frame[2] = True  # an ancestor now contains a block
            self.open.append([tag, self._end_of_starttag(), False, False])
        elif tag in INLINE and tag != "br" and not self.open:
            # A BARE INLINE ELEMENT IS ITS OWN UNIT. The header nav is
            # `<nav><a href="/changelog">What&#39;s new</a></nav>`: there is no
            # block around it, so without this the entire chrome -- every nav
            # link, on all six pages -- is silently untranslatable. Found by
            # diffing this extractor's coverage against a plain text-node walk,
            # which is the only way a miss like that is visible: nothing fails,
            # the string simply never appears in the catalog.
            self.open.append([tag, self._end_of_starttag(), False, False])
        elif self.open and tag not in INLINE and tag not in OPAQUE and tag != "br":
            # A non-inline, non-block child (a <div>, a <ul>) also means the
            # enclosing block is not a single sentence.
            #
            # OPAQUE IS EXEMPT, which two real misses paid for. The status
            # pill is `<a><svg>...</svg><span>Status</span></a>` and a privacy
            # row is `<td>Your VRChat user ID (<code>usr_...</code>)</td>`;
            # treating the svg and the code as nesting made both whole units
            # untranslatable, silently. They are placeholders: the translation
            # decides where they go and can never alter what is inside them.
            self.open[-1][2] = True

    def handle_startendtag(self, tag, attrs):
        self._attrs(dict(attrs), tag)

    def handle_endtag(self, tag):
        if tag in OPAQUE and self.opaque:
            self.opaque -= 1
            return
        if self.opaque:
            return
        if tag in BLOCKS or tag in INLINE:
            for i in range(len(self.open) - 1, -1, -1):
                if self.open[i][0] == tag:
                    frame = self.open.pop(i)
                    self._close(frame)
                    break
        if self.no_translate and tag not in INLINE:
            self.no_translate = max(0, self.no_translate - 1)

    def _close(self, frame) -> None:
        tag, start, nested, words = frame
        if nested or not words:
            return
        end = self._offset()
        inner = self.source[start:end]
        msgid = to_msgid(inner)
        if not msgid or not HAS_WORDS.search(msgid) or NEVER.match(msgid):
            return
        self.found.append((start, end, "block", inner))

    def _attrs(self, d: dict, tag: str) -> None:
        raw = self.get_starttag_text() or ""
        base = self._offset()
        wanted = list(TRANSLATABLE_ATTRS)
        if tag == "meta" and d.get("name") == "description":
            wanted.append("content")
        for name in wanted:
            value = d.get(name)
            if not value or not HAS_WORDS.search(value) or NEVER.match(value.strip()):
                continue
            m = re.search(rf'\b{re.escape(name)}\s*=\s*(["\'])(.*?)\1', raw, re.S)
            if not m or m.group(2) != value:
                continue
            at = base + m.start(2)
            self.found.append((at, at + len(m.group(2)), f"@{name}", m.group(2)))

    # -- content ---------------------------------------------------------
    def handle_data(self, data):
        if self.open and not self.opaque and not self.no_translate and HAS_WORDS.search(data):
            self.open[-1][3] = True

    def handle_comment(self, data):
        # Never evidence that an element holds prose. A `<p>` containing only a
        # comment is not a sentence.
        return

    def handle_entityref(self, name):
        self._charlike()

    def handle_charref(self, name):
        self._charlike()

    def _charlike(self):
        pass


_TAG = re.compile(r"<(/?)([a-zA-Z][\w-]*)\b([^>]*?)(/?)>")


_COMMENT = re.compile(r"<!--.*?-->", re.S)


def _tokens(inner: str):
    """Walk inner HTML, yielding ("text", s) and ("el", tag, raw, span) pairs.

    An OPAQUE element yields one token covering the whole element, its content
    included, so nothing inside an `<svg>` or a `<code>` can be reached by a
    translation.

    SO DOES A COMMENT, and that one was found the hard way. These pages carry
    long notes inside the markup -- the status pill's is a paragraph about
    relative luminance and red-green colorblindness -- and a comment sitting
    inside a translatable span became part of the msgid, got HTML-escaped on
    the way back, and rendered as VISIBLE TEXT on the page. A stub catalog
    caught it; nothing else would have until somebody loaded the German 404.
    """
    at = 0
    while at < len(inner):
        c = _COMMENT.search(inner, at)
        m = _TAG.search(inner, at)
        if c and (not m or c.start() <= m.start()):
            if c.start() > at:
                yield ("text", inner[at : c.start()])
            yield ("opaque", "#comment", c.group(0))
            at = c.end()
            continue
        if not m:
            yield ("text", inner[at:])
            return
        if m.start() > at:
            yield ("text", inner[at : m.start()])
        closing, tag, selfclose = m.group(1), m.group(2).lower(), m.group(4)
        if tag in OPAQUE and not closing:
            close = re.search(rf"</{re.escape(tag)}\s*>", inner[m.end():], re.I)
            stop = m.end() + (close.end() if close else 0)
            yield ("opaque", tag, inner[m.start():stop])
            at = stop
            continue
        yield ("el", tag, m.group(0), bool(closing), bool(selfclose))
        at = m.end()


def to_msgid(inner: str) -> str:
    """Inner HTML -> the string a translator sees.

    Inline tags become `<0>`/`</0>` in the order they open, opaque elements
    become one `<0/>`, entities decode to characters, whitespace collapses.
    Everything a translator must not touch -- hrefs, classes, the contents of
    a `<code>` -- is left behind here and put back by `from_msgid`.
    """
    out, counter, stack = [], 0, []
    for token in _tokens(inner):
        if token[0] == "text":
            out.append(token[1])
        elif token[0] == "opaque":
            out.append(f"<{counter}/>")
            counter += 1
        else:
            _, tag, _raw, closing, selfclose = token
            if tag not in INLINE:
                continue
            if closing:
                out.append(f"</{stack.pop()}>" if stack else "")
            elif tag == "br" or selfclose:
                out.append(f"<{counter}/>")
                counter += 1
            else:
                out.append(f"<{counter}>")
                stack.append(counter)
                counter += 1
    return " ".join(html.unescape("".join(out)).split())


def _placeholders(inner: str) -> tuple[dict[int, str], dict[int, str]]:
    """`{n: opening markup}` and `{n: tag name}`, numbered as `to_msgid` does."""
    originals: dict[int, str] = {}
    names: dict[int, str] = {}
    counter = 0
    for token in _tokens(inner):
        if token[0] == "text":
            continue
        if token[0] == "opaque":
            originals[counter] = token[2]
            counter += 1
            continue
        _, tag, raw, closing, selfclose = token
        if tag not in INLINE or closing:
            continue
        originals[counter] = raw
        names[counter] = tag
        counter += 1
    return originals, names


def from_msgid(translated: str, inner: str) -> str:
    """The inverse: put the original markup back where the translation put it."""
    originals, names = _placeholders(inner)
    out = html.escape(translated, quote=False)
    out = re.sub(r"&lt;(\d+)/&gt;", lambda m: originals.get(int(m.group(1)), ""), out)
    out = re.sub(r"&lt;(\d+)&gt;", lambda m: originals.get(int(m.group(1)), ""), out)
    out = re.sub(
        r"&lt;/(\d+)&gt;",
        lambda m: f"</{names[int(m.group(1))]}>" if int(m.group(1)) in names else "",
        out,
    )
    return out


def collect(source: str) -> list[tuple[int, int, str, str]]:
    """`(start, end, kind, raw)` for every translatable span, in document order."""
    c = _Collector(source)
    c.feed(source)
    c.close()
    for start, end, kind, raw in c.found:
        assert source[start:end] == raw, (
            f"offset {start}:{end} is {source[start:end]!r}, expected {raw!r}"
        )
    # Overlapping spans would corrupt a splice. A block inside a block is the
    # way that happens, and `_close` already refuses those, so this is the
    # guard on that logic rather than on the input.
    spans = sorted(c.found)
    for (s1, e1, *_), (s2, *_) in zip(spans, spans[1:]):
        assert e1 <= s2, f"spans overlap: {s1}:{e1} and {s2}"
    return spans
