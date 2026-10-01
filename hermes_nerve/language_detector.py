"""Language detector and exemption pipeline for Hermes Nerve (KEE-73 phase 2).

This module provides the text-classification pipeline that determines whether a
chunk of assistant-authored text is *natural-language prose* subject to the
Language Contract, or *exempt* content that should not influence language
detection or enforcement.

Exemption categories
--------------------
The following content categories are always exempt from language contract
detection and validation:

1.  ``code``          — fenced/indented code blocks (``` or ~~~), inline code
                        spans (`…`), or content that looks like program source.
2.  ``command``       — shell/CLI invocations: lines starting with ``$``, ``#!``,
                        tool-call payloads, or ``>`` shell prompts.
3.  ``url``           — HTTP/HTTPS/FTP/data URIs, ``mailto:``, or any token
                        matching a loose RFC-3986 URL pattern.
4.  ``filename``      — bare file paths (relative or absolute) without surrounding
                        prose, extensions list (``*.py``), dotfiles, etc.
5.  ``structured``    — JSON, YAML, TOML, XML/HTML fragments, CSV rows, Markdown
                        tables, or other machine-readable structured data.
6.  ``log``           — log/tool output lines: timestamps, severity prefixes
                        (``INFO:``, ``ERROR:``, ``DEBUG:``), stack traces, or
                        content explicitly tagged as tool output.
7.  ``quote``         — block-quoted text (``>`` Markdown blockquote), explicitly
                        attributed citations, or ``---`` fenced quote blocks.
8.  ``cjk_identifier``— CJK characters used as code identifiers (variable/function
                        names in CJK-identifier-enabled languages), not as
                        natural-language prose.  Detected by very short CJK token
                        sequences surrounded by code syntax.
9.  ``translation``   — content that is an explicit translation payload: marked by
                        the caller with a ``translation_payload=True`` flag, or
                        wrapped in a recognised translation wrapper format.

Usage
-----
::

    from hermes_nerve.language_detector import (
        ExemptionCategory,
        classify_text_segments,
        extract_prose_segments,
        is_exempt,
    )

    segments = classify_text_segments(assistant_response_text)
    prose_only = extract_prose_segments(segments)

Observe-only validation is integrated into
:class:`hermes_nerve.language_contract.LanguageContract` via
``observe_response()``; this module provides only the classification
primitives, keeping concerns separated.

PRIVATE implementation — KEE-73 phase 2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Sequence

__all__ = [
    "ExemptionCategory",
    "TextSegment",
    "classify_text_segments",
    "extract_prose_segments",
    "is_exempt",
    "prose_for_detection",
]

# ---------------------------------------------------------------------------
# Exemption category
# ---------------------------------------------------------------------------

ExemptionCategory = Literal[
    "code",
    "command",
    "url",
    "filename",
    "structured",
    "log",
    "quote",
    "cjk_identifier",
    "translation",
    "prose",  # NOT exempt — natural-language prose subject to contract
]

_EXEMPT_CATEGORIES: frozenset[str] = frozenset({
    "code",
    "command",
    "url",
    "filename",
    "structured",
    "log",
    "quote",
    "cjk_identifier",
    "translation",
})


# ---------------------------------------------------------------------------
# TextSegment — unit of classification
# ---------------------------------------------------------------------------

@dataclass
class TextSegment:
    """A contiguous span of text with its classification.

    Attributes
    ----------
    text : str
        The raw text of this segment.
    category : ExemptionCategory
        Classification of this segment.  ``"prose"`` means the segment is
        natural-language text subject to the language contract.  Any other
        value means the segment is exempt.
    start : int
        Byte/character offset of the segment start within the original string.
    end : int
        Byte/character offset of the segment end (exclusive).
    """

    text: str
    category: ExemptionCategory
    start: int
    end: int
    metadata: dict = field(default_factory=dict)

    @property
    def is_exempt(self) -> bool:
        return self.category in _EXEMPT_CATEGORIES

    @property
    def is_prose(self) -> bool:
        return self.category == "prose"


# ---------------------------------------------------------------------------
# Compiled patterns — order matters for correctness / performance
# ---------------------------------------------------------------------------

# Fenced code block: ``` or ~~~ with optional language tag
_FENCED_CODE_RE = re.compile(
    r"(?:^|\n)([ \t]*)(`{3,}|~{3,})([^\n]*)\n(.*?)(\n\1\2[^\S\n]*(?:\n|$))",
    re.DOTALL,
)

# Inline code span: `text` or ``text`` (single-line; avoid multi-line)
_INLINE_CODE_RE = re.compile(r"`{1,2}(?!`)[^`\n]{0,200}?`{1,2}")

# Indented code block: 4+ spaces or 1 tab at line start (only when ≥2 consecutive lines)
_INDENTED_CODE_RE = re.compile(
    r"(?:(?:^|\n)(?: {4}|\t)[^\n]*)+",
)

# URL: http/https/ftp/data/mailto/ssh/git schemes
_URL_RE = re.compile(
    r"(?:"
    r"(?:https?|ftp|data|ssh|git|file)://"
    r"[^\s<>\"'(){}\[\]\\]+"
    r"|"
    r"mailto:[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
    r"|"
    r"www\.[a-zA-Z0-9][-a-zA-Z0-9.]{1,}\.[a-zA-Z]{2,}(?:/[^\s<>\"'(){}\[\]\\]*)?"
    r")",
    re.IGNORECASE,
)

# Shell command lines: starts with $, #!, or common shell prompts
_COMMAND_LINE_RE = re.compile(
    r"(?:^|\n)"                          # line start
    r"(?:"
    r"\$[ \t]+"                          # $ prompt
    r"|#![ \t]*/"                        # shebang
    r"|> "                               # REPL prompt
    r"|% "                               # zsh/csh prompt
    r"|PS[12]>"                          # PowerShell prompt
    r")"
    r"[^\n]+",
)

# File path / filename: relative paths, absolute Unix/Windows paths, globs, dotfiles
_FILEPATH_RE = re.compile(
    r"(?:"
    r"(?:/|\.{1,2}/)[a-zA-Z0-9_./-]{2,100}"  # Unix path
    r"|[a-zA-Z]:\\[^<>:\"\\|?*\n]{2,100}"      # Windows path
    r"|~[/\\][^\s<>\"'(){}\[\]]{2,80}"         # ~ path
    r"|\.[a-zA-Z0-9_-]{1,20}(?:/[^\s<>\"'()\[\]]{1,80})?"  # dotfile/dotdir
    r"|[a-zA-Z0-9_-]+\.[a-z]{1,6}(?:/[^\s<>\"'()\[\]]{0,80})?(?=\s|$|[,;)])"  # filename.ext
    r")",
    re.UNICODE,
)

# JSON fragments: objects { ... } or arrays [ ... ] with key: value pairs
_JSON_RE = re.compile(
    r"\{[ \t]*(?:\"[^\"]*\"[ \t]*:[ \t]*[^,\n\}]{1,200}[ \t]*,?[ \t]*\n?[ \t]*){1,}[ \t]*\}"
    r"|"
    r"\[[ \t]*\n[ \t]*(?:[^\n\]]{1,200}\n[ \t]*){1,}\]",
    re.DOTALL,
)

# YAML-like key: value lines (at least 2 consecutive)
_YAML_RE = re.compile(
    r"(?:(?:^|\n)[ \t]*[a-zA-Z_][a-zA-Z0-9_-]*[ \t]*:[ \t]+[^\n]{1,200}){2,}",
)

# XML/HTML tags
_XML_TAG_RE = re.compile(
    r"<(?:[a-zA-Z][a-zA-Z0-9:_-]*|/[a-zA-Z][a-zA-Z0-9:_-]*)(?:\s[^>]{0,200})?/?>",
)

# Markdown table rows: | cell | cell |
_MD_TABLE_RE = re.compile(
    r"(?:(?:^|\n)\|[^\n]+\|){2,}",
)

# Log / tool output lines: timestamp prefixes, severity keywords
_LOG_LINE_RE = re.compile(
    r"(?:^|\n)"
    r"(?:"
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"   # ISO timestamp
    r"|(?:INFO|WARN|WARNING|ERROR|DEBUG|TRACE|CRITICAL|FATAL)[ \t]*[:\|]"
    r"|File \"[^\"]+\", line \d+"                  # Python traceback frame
    r"|Traceback \(most recent call last\)"
    r"|    at [a-zA-Z_$]"                           # JS/Java stack frame
    r"|^\d{2}:\d{2}:\d{2}\.\d{3}"                  # time-only prefix
    r")"
    r"[^\n]*",
)

# Blockquote: lines starting with >
_BLOCKQUOTE_RE = re.compile(
    r"(?:(?:^|\n)>[ \t][^\n]{0,500})+",
)

# CJK identifier: 1-4 CJK chars surrounded by code punctuation (not long enough for prose)
# Prose detection threshold is ≥3 chars; identifiers tend to be 1-3 chars mixed with ASCII.
_CJK_IDENTIFIER_RE = re.compile(
    r"(?<=[.\[(,\s:=])[\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]{1,3}(?=[.\](),\s:;])",
)

# Ordered list of (pattern, category) to apply in sequence.
# Fenced code must come before inline code.  Structured data after URLs.
_BLOCK_PATTERNS: list[tuple[re.Pattern, ExemptionCategory]] = [
    (_FENCED_CODE_RE,    "code"),
    (_INDENTED_CODE_RE,  "code"),
    (_BLOCKQUOTE_RE,     "quote"),
    (_LOG_LINE_RE,       "log"),
    (_COMMAND_LINE_RE,   "command"),
    (_MD_TABLE_RE,       "structured"),
    (_JSON_RE,           "structured"),
    (_YAML_RE,           "structured"),
]

_INLINE_PATTERNS: list[tuple[re.Pattern, ExemptionCategory]] = [
    (_INLINE_CODE_RE,    "code"),
    (_URL_RE,            "url"),
    (_CJK_IDENTIFIER_RE, "cjk_identifier"),
]


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def classify_text_segments(
    text: str,
    *,
    translation_payload: bool = False,
) -> list[TextSegment]:
    """Classify a string into a list of contiguous :class:`TextSegment` objects.

    Parameters
    ----------
    text : str
        Raw text to classify (typically one assistant response or message part).
    translation_payload : bool
        If ``True``, the entire text is marked as ``"translation"`` and returned
        as a single exempt segment.  Use this when the caller knows the content
        is an explicit translation (e.g. a ``/translate`` command result).

    Returns
    -------
    list[TextSegment]
        Ordered, non-overlapping segments covering the entire input.  Segments
        of category ``"prose"`` are natural-language text subject to the contract.
    """
    if not text:
        return []

    if translation_payload:
        return [TextSegment(text=text, category="translation", start=0, end=len(text))]

    # Build a sorted list of non-overlapping exempt spans by applying patterns.
    # Earlier patterns have priority; overlapping later matches are skipped.
    exempt_spans: list[tuple[int, int, ExemptionCategory]] = []

    def _add_span(start: int, end: int, cat: ExemptionCategory) -> None:
        """Add span only if it does not overlap any existing span."""
        for ex_s, ex_e, _ in exempt_spans:
            if start < ex_e and end > ex_s:
                return  # overlaps — skip
        exempt_spans.append((start, end, cat))

    # Apply block patterns first (they consume multi-line regions)
    for pat, cat in _BLOCK_PATTERNS:
        for m in pat.finditer(text):
            _add_span(m.start(), m.end(), cat)

    # Apply inline patterns (single-span matches within remaining prose)
    for pat, cat in _INLINE_PATTERNS:
        for m in pat.finditer(text):
            _add_span(m.start(), m.end(), cat)

    # File path: only classify if the match does not overlap existing spans
    for m in _FILEPATH_RE.finditer(text):
        _add_span(m.start(), m.end(), "filename")

    # Sort all exempt spans by start offset
    exempt_spans.sort(key=lambda t: t[0])

    # Build segment list: fill gaps between exempt spans as prose
    segments: list[TextSegment] = []
    pos = 0
    for span_start, span_end, cat in exempt_spans:
        if pos < span_start:
            prose_text = text[pos:span_start]
            if prose_text.strip():  # only emit non-blank prose
                segments.append(TextSegment(
                    text=prose_text,
                    category="prose",
                    start=pos,
                    end=span_start,
                ))
        segments.append(TextSegment(
            text=text[span_start:span_end],
            category=cat,
            start=span_start,
            end=span_end,
        ))
        pos = span_end

    # Trailing prose after last exempt span
    if pos < len(text):
        prose_text = text[pos:]
        if prose_text.strip():
            segments.append(TextSegment(
                text=prose_text,
                category="prose",
                start=pos,
                end=len(text),
            ))

    # If no segments at all, the whole text is prose
    if not segments and text.strip():
        segments.append(TextSegment(text=text, category="prose", start=0, end=len(text)))

    return segments


def extract_prose_segments(segments: Sequence[TextSegment]) -> list[TextSegment]:
    """Return only the non-exempt (prose) segments from a classified list."""
    return [s for s in segments if s.is_prose]


def is_exempt(text: str, *, translation_payload: bool = False) -> bool:
    """Return True if *all* non-whitespace content in *text* is exempt.

    A text is considered fully exempt when no ``"prose"`` segment survives
    after classification.  This is used by the observe-only validator to skip
    responses that contain no natural-language prose.
    """
    if not str(text or "").strip():
        return True
    segs = classify_text_segments(text, translation_payload=translation_payload)
    return all(s.is_exempt for s in segs)


def prose_for_detection(
    text: str,
    *,
    translation_payload: bool = False,
    min_prose_chars: int = 20,
) -> str | None:
    """Extract the prose portions of *text* suitable for language detection.

    Returns a concatenated string of prose segments, or ``None`` if the total
    prose content is below *min_prose_chars* (too short to be reliable).

    Parameters
    ----------
    text : str
        Raw text (e.g. an assistant response).
    translation_payload : bool
        Mark the whole text as a translation payload (fully exempt).
    min_prose_chars : int
        Minimum total prose characters required to return a result.
        Default: 20.  Short snippets are unreliable for language detection.
    """
    segs = classify_text_segments(text, translation_payload=translation_payload)
    prose_parts = [s.text for s in segs if s.is_prose]
    combined = " ".join(prose_parts)
    if len(combined.strip()) < min_prose_chars:
        return None
    return combined
