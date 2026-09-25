"""Escaping for the XML documents the master serves.

Every document here is built by string formatting rather than a DOM, because the speakers are
matched byte for byte against what a real master sends (REPORT.md S4) and a serializer would
reorder attributes and self-close differently. That choice is fine; interpolating unescaped text
into it is not, and the two are easy to confuse.

So the rule is per POSITION, not per value: a value going between tags goes through :func:`text`,
a value going inside quotes goes through :func:`attr`, and a value that is itself markup - the
``ContentItem`` fragment a slave sent, which the master echoes back the way a real one does - goes
through :func:`fragment`, which escapes nothing and leaves it markup.

All three drop the characters XML 1.0 cannot carry, which is why the third one exists at all: the
fragment is exempt from ESCAPING, not from being XML, and it arrives from the same unauthenticated
LAN side as everything else here.
"""

from __future__ import annotations

import re

# bandit B406 blacklists xml.sax imports because PARSING untrusted XML is attackable. Nothing here
# parses: escape and quoteattr are pure string functions, and this module only writes.
from xml.sax.saxutils import escape, quoteattr  # nosec B406

__all__ = ["attr", "fragment", "text"]

_NOT_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
"""Everything XML 1.0's Char production excludes, which is not an escaping problem but a hard one.

``&#0;`` is not the escape for a NUL: these codepoints may not appear in a document at all, by any
spelling, so a builder's only move is to leave them out. Tab, newline and carriage return are the
three C0 characters XML does allow and are deliberately absent from this class - sweeping them up
would break the documents this module exists to keep working.

Left in on purpose: C1 (#x7f-#x9f), which XML 1.0 permits. This drops what a parser must reject,
not what a style guide dislikes.
"""


def _keep_only_xml(value: str) -> str:
    """The value with every character XML cannot carry removed."""
    return _NOT_XML.sub("", value)


# What quoteattr escapes beyond the markup characters, so a control character in an
# attribute survives the round trip instead of being normalised to a space.
_ATTR_ENTITIES = {"\n": "&#10;", "\r": "&#13;", "\t": "&#9;"}


def text(value: str) -> str:
    """Escape a value being placed between tags.

    A station name is the usual one, and it is ordinary for it to contain an ampersand; unescaped,
    ``<track>Simon & Garfunkel</track>`` is not XML at all and a slave stops parsing there.
    """
    return escape(_keep_only_xml(value))


def attr(value: str) -> str:
    """Escape a value being placed inside an attribute, WITHOUT its quotes.

    ``quoteattr`` adds the quotes and picks which kind; the documents here already write their
    own, so the quotes it added are stripped back off and the caller keeps its literal ``"``.
    """
    value = _keep_only_xml(value)
    quoted = quoteattr(value)
    if quoted.startswith("'"):
        # quoteattr fell back to single quotes because the value holds a double quote; the
        # callers all write double quotes, so escape it the way those callers need instead.
        # The entity map is quoteattr's own: without it this branch would leave a literal
        # newline or tab in the attribute, which a parser then normalises to a space, so the
        # two branches of this function would disagree about what survives.
        return escape(value, _ATTR_ENTITIES).replace('"', "&quot;")
    return quoted[1:-1]


def fragment(markup: str) -> str:
    """Pass through a value that is itself markup, dropping only what XML cannot carry.

    The ``ContentItem`` a slave sent is echoed back as markup, because that is what a real master
    does and a slave matches it. So it must NOT be escaped - escaping it would turn an element the
    speakers read into text they display. It must still be XML, though, and a control character in
    it reaches every speaker in the zone through ``/notification`` exactly as one in a station name
    would, so the one thing this does is take those out.

    It cannot make a malformed fragment well-formed, and it does not try: an unbalanced tag from a
    slave is a different problem with a different answer.
    """
    return _keep_only_xml(markup)
