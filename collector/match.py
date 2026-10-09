"""Keyword matching for Tamil and English text."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .config import Config

TAMIL_RE = re.compile(r"[஀-௿]")


def normalize(text: str) -> str:
    """Lower-case, NFC-normalise, and turn hyphens/underscores into spaces."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text).lower()
    text = re.sub(r"[-_‐-―]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _compile(term: str) -> re.Pattern:
    t = normalize(term).strip()
    if TAMIL_RE.search(t):
        # Tamil is agglutinative: allow any suffix, so plain substring match.
        # A final pulli (்) changes when a case suffix is added
        # (நடுகற்கள் -> நடுகற்களை), so match on the consonant without it.
        if t.endswith("்") and len(t) > 3:
            t = t[:-1]
        return re.compile(re.escape(t).replace(r"\ ", r"\s+"))
    esc = re.escape(t).replace(r"\ ", r"\s+")
    # English: whole-word match so "erode" doesn't hit "eroded", "java" not "javascript".
    return re.compile(r"(?<![a-z0-9])" + esc + r"(?![a-z0-9])")


@dataclass
class MatchResult:
    groups: list[str]          # group keys that matched
    terms: list[str]           # the actual terms found
    context: list[str]         # context terms found
    places: list[str]          # place names found
    first_pos: int             # position of first group-term hit in text (-1 if none)

    @property
    def ok(self) -> bool:
        return bool(self.groups)


class Matcher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.group_patterns = [
            (g.key, [(t, _compile(t)) for t in g.terms]) for g in cfg.groups
        ]
        self.context_patterns = [(t, _compile(t)) for t in cfg.context_terms]
        self.place_patterns = [
            (p["name"], [_compile(t) for t in p.get("terms", [])]) for p in cfg.places
        ]

    def match(self, title: str, text: str = "") -> MatchResult:
        hay = normalize(f"{title or ''}\n{text or ''}")
        groups, terms = [], []
        first = -1
        for key, pats in self.group_patterns:
            hit = False
            for term, pat in pats:
                m = pat.search(hay)
                if m:
                    hit = True
                    if term not in terms:
                        terms.append(term)
                    if first == -1 or m.start() < first:
                        first = m.start()
            if hit:
                groups.append(key)
        context = [t for t, p in self.context_patterns if p.search(hay)]
        places = [name for name, pats in self.place_patterns if any(p.search(hay) for p in pats)]
        return MatchResult(groups, terms, context, places, first)

    def accept(self, title: str, text: str) -> MatchResult | None:
        """Return a MatchResult if the article qualifies, else None."""
        r = self.match(title, text)
        if not r.ok:
            return None
        # Strong groups (inscription / hero stone / rock art) in Tamil script are
        # inherently Tamil-related; otherwise require a Tamil context term.
        if self.cfg.require_context and not r.context:
            if not TAMIL_RE.search(f"{title} {text}"):
                return None
        return r

    def title_candidate(self, title: str) -> bool:
        """Cheap check on a headline only (used for archive index pages)."""
        return self.match(title).ok


def is_tamil(text: str) -> bool:
    if not text:
        return False
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    ta = sum(1 for c in letters if "஀" <= c <= "௿")
    return ta / len(letters) > 0.3


def make_snippet(text: str, title: str, max_len: int = 300) -> str:
    """Short excerpt of the article around the first keyword hit (for scanning)."""
    if not text:
        return ""
    t = re.sub(r"\s+", " ", text).strip()
    return t[:max_len] + ("…" if len(t) > max_len else "")
