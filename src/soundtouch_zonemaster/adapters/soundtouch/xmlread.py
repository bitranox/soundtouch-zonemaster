"""Read XML that a speaker, or anything else on the LAN, sent us: bounded in size, depth and time.

Every document this master reads arrives unauthenticated. ``POST /slaveMsg`` on 8090 answers
whatever is on the flat's network, and so does a notification frame. That is why this module
exists rather than a bare ``ET.fromstring`` at each call site: one place decides what may be
parsed at all, and it answers ``None`` for everything it will not touch instead of raising into
an event loop.

What the stdlib already does, measured 2026-09-22 on Python 3.14.5 with expat 2.6.3 (service-host runs
3.14.4 with expat 2.7.4), each case fed as the only content of a parse:

* a billion-laughs document is refused by expat itself, ``limit on input amplification factor``,
  after 11 to 27 ms and up to 11.5 MiB, depending on how far it expands before the limit stops it;
* a quadratic-blowup document the same way, after 3.8 ms;
* an external entity is never fetched - ElementTree declares no handler for one, so the reference
  comes back as ``undefined entity`` and no file can be disclosed;
* a recursive entity pair is refused as ``recursive entity reference``.

So the three checks below are what the stdlib does NOT do:

1. **Size.** 64 KiB, against a largest recorded frame of 2,831 characters. It is what bounds
   everything the parser will spend on one document: at the cap the worst legal shape measured
   4.2 ms and 1.4 MiB.
2. **No DOCTYPE at all**, screened out of the text before the parser is built. This is sound
   because the input is already a ``str``: ElementTree feeds a ``str`` to expat as UTF-8 and
   overrides the declared encoding, so the text screened here is the text parsed. Without a
   DOCTYPE no entity can be declared, which is the whole entity-attack family - and it costs a
   substring search rather than the milliseconds expat's own limit needs to reach that answer. It
   also means this protection does not depend on which expat the host happens to carry.
3. **Depth**, walked iteratively after the parse. This one is not decoration: ``ET.tostring``
   RECURSES, and the master calls it to rebuild the ContentItem of a select, so a body nesting a
   few thousand elements inside ``<content>`` would reach a ``RecursionError`` well away from
   here. 32 is far above anything a speaker sends; the deepest recorded frame nests 6.

The parsed tree is read through the helpers here rather than by walking children, because the
patterns these replaced searched the WHOLE text: ``deviceID="..."`` found the first one anywhere
in the frame, wherever it sat. ``element_anywhere`` and ``attribute_anywhere`` keep that, in
document order, iteratively.
"""

from __future__ import annotations

# The one suppression in this package (S314, on the parse call below) is about parsing untrusted
# XML with the stdlib, which is exactly what this module does - deliberately, and as the single
# place it happens. The docstring above records what expat refuses by itself and what the three
# checks in `parse` add; tests/test_xmlread.py holds every one of those answers, and a mutation
# arm reddens it when any check is removed. Hardening the parser is the fix that rule asks for;
# defusedxml would add a 2021 dependency doing less than this.
#
# A comment line here must not BEGIN with the linter's name: that spelling is a file-level
# directive to it (RUF103), whatever the sentence says.
import xml.etree.ElementTree as ET  # nosec B405 - this module IS the guarded parse; see the docstring
from typing import Final

__all__ = [
    "MAX_DEPTH",
    "MAX_XML_CHARS",
    "Element",
    "attribute_anywhere",
    "child_tag",
    "element_anywhere",
    "parse",
    "serialised_as",
]

type Element = ET.Element
"""A parsed element, for the modules that read one.

They annotate with this rather than importing ``xml.etree`` themselves, so this module stays the
ONLY place in the package that names the stdlib XML parser - which is what makes "everything is
parsed here" checkable by reading the imports rather than by trusting a convention.
"""

MAX_XML_CHARS: Final = 64 * 1024
"""The longest document this master will parse.

More than twenty times the largest frame any recorded run carried (2,831 characters), and well
under the megabyte the HTTP face accepts, so a body between the two is refused here rather than
parsed. It is the bound that makes every other cost finite.
"""

MAX_DEPTH: Final = 32
"""How deeply a document may nest. The deepest frame a speaker has sent nests six."""

_DOCTYPE: Final = "<!DOCTYPE"
"""The only spelling XML allows; a document carrying it is refused unparsed."""


def parse(text: str) -> ET.Element | None:
    """The document's root element, or ``None`` for anything this master will not read.

    ``None`` rather than an exception for every refusal and every malformed document, because
    every caller answers them the same way: a frame is kept whole and reported as unreadable, and
    a request is logged and answered. Nothing here raises into an event loop.
    """
    if len(text) > MAX_XML_CHARS or _DOCTYPE in text:
        return None
    try:
        # The one suppressed line in the package, and the rules are right about the risk: this is
        # the hardened parse they ask for instead - text screened for a DOCTYPE above, depth
        # checked below, and tests/test_xmlread.py holding every answer.
        root = ET.fromstring(text)  # noqa: S314  # nosec B314
    except (ET.ParseError, ValueError):
        # ValueError covers the UnicodeEncodeError a lone surrogate raises on the way to expat,
        # which is not a ParseError and would otherwise leave this function by the back door.
        return None
    return root if _within_depth(root) else None


def _within_depth(root: ET.Element) -> bool:
    """Whether the tree nests no deeper than :data:`MAX_DEPTH`, walked with a stack, never recursion."""
    stack: list[tuple[ET.Element, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        if depth > MAX_DEPTH:
            return False
        stack.extend((child, depth + 1) for child in element)
    return True


def element_anywhere(root: ET.Element, tag: str) -> ET.Element | None:
    """The first ``<tag>`` in document order, the root itself included, or ``None``.

    ``<nowPlaying>`` sits under a different parent in each kind of frame, and the patterns this
    replaced simply searched the text for it, so the search stays over the whole document.
    """
    return next(root.iter(tag), None)


def attribute_anywhere(root: ET.Element, name: str) -> str | None:
    """The value of the first element in document order carrying attribute ``name``, or ``None``."""
    for element in root.iter():
        value = element.get(name)
        if value is not None:
            return value
    return None


def child_tag(element: ET.Element) -> str | None:
    """The tag of the first child element, or ``None`` when it has none."""
    first = next(iter(element), None)
    return None if first is None else first.tag


def serialised_as(element: ET.Element, tag: str) -> str:
    """``element`` written back out under a different tag: the ContentItem of a select.

    A speaker sends its selection as ``<content ...>`` and stores it as ``<ContentItem ...>``, and
    the master keeps the document to play it again later. Rebuilt from the tree rather than sliced
    out of the body verbatim: all 34 recorded selects come back byte for byte either way
    (measured 2026-09-22), and what an unparsed body might carry is exactly what should not be
    handed on to a speaker.
    """
    element.tag = tag
    element.tail = None
    return ET.tostring(element, encoding="unicode")
