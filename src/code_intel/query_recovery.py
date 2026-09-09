"""Bounded lexical query recovery; no model calls or semantic-search claims."""

from __future__ import annotations

import re
from typing import Any

MAX_TERMS = 8
QUERY_GUIDANCE = "No match. Try an identifier or 2-4 distinctive keywords, or search_text for a literal."
LOOKUP_DESCRIPTION = (
    "Find ranked source symbols and files. Prefer an exact identifier or 2-4 distinctive keywords; "
    "this is lexical search, not semantic search. Sentence misses use a bounded keyword fallback. "
    "Results include signatures, docstring summaries and bounded source excerpts. "
    "Use context_pack for more source context."
)
CONTEXT_DESCRIPTION = (
    "Return bounded source snippets around ranked symbols. Use an identifier or 2-4 distinctive keywords, "
    "not a full question. Includes the declaration and available docstring/body within the line budget."
)
STOP_WORDS = frozenset(
    """a an and are as at be before by can code could do does find for from function functions
get give how i in into is it locate me method methods missing of on or our please provide return returns show
supplies supply tell that the their these this to using value values want what when where which with without""".split()
)


def query_terms(query: str) -> list[str]:
    """Extract at most eight distinct lexical terms from a sentence.

    Single identifiers and paths stay on the exact lookup path. Conservative
    inflection trimming handles plurals/adverbs, without inventing synonyms.
    """
    if len(query) > 512 or len(query.split()) < 2:
        return []
    terms = []
    for word in re.findall(r"[A-Za-z][A-Za-z0-9]*", query):
        word = word.casefold()
        if word in STOP_WORDS or len(word) < 3 or len(word) > 48:
            continue
        if word.endswith("ically") and len(word) > 8:
            word = word[:-4]
        elif word.endswith("ly") and len(word) > 5:
            word = word[:-2]
        elif word.endswith(("shes", "ches", "xes", "sses")):
            word = word[:-2]
        elif word.endswith("ies") and len(word) > 5:
            word = word[:-3] + "y"
        elif word.endswith("s") and not word.endswith(("ss", "us")) and len(word) > 4:
            word = word[:-1]
        if word not in terms:
            terms.append(word)
        if len(terms) == MAX_TERMS:
            break
    return terms if len(terms) >= 2 else []


def rank_keyword_candidates(rows: list[dict[str, Any]], terms: list[str]) -> list[dict[str, Any]]:
    """Rank by distinct term coverage, then name/doc evidence and specificity."""
    ranked = []
    for row in rows:
        name = str(row["qualified_name"]).casefold()
        doc = str(row["doc"]).casefold()
        signature = str(row["signature"]).casefold()
        body_terms = set(row.get("body_terms", []))
        covered = {term for term in terms if term in name or term in doc or term in signature or term in body_terms}
        if len(covered) < max(2, (len(terms) + 2) // 3):
            continue
        name_hits = sum(term in name for term in covered)
        doc_hits = sum(term in doc for term in covered)
        score = 1200 - len(covered) * 70 - name_hits * 20 - doc_hits * 10
        if row["kind"] == "class":
            score += 35
        ranked.append({**row, "recovery_score": score, "matched_terms": sorted(covered)})
    return sorted(ranked, key=lambda row: (row["recovery_score"], row["path"], row["line"]))
