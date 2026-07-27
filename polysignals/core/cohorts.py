"""
Market universes and how a market is assigned to one.

The project runs the same detector over two populations:

  committee — outcome decided by a panel's private deliberation (awards,
              prizes, inductions). Insider information is *plausible*:
              voters know before the public does.
  sports    — outcome decided by play on a field. Insider information is
              *implausible*: nobody knows the final score in advance.

The counter-test rests entirely on the second being a genuine null. If
the detector performs the same in both, it is measuring something about
large trades near resolution generally — market microstructure — and not
information asymmetry.

Assignment is EXCLUSIVE, and that is the important design decision here.
A market matching more than one keyword list is assigned to neither, and
counted as ambiguous. Two reasons:

  1. Correctness of the experiment. Overlap between the arms means the
     same market is evidence for both the treatment and the control, which
     is not a comparison of anything.
  2. Correctness of the data. markets.market_id is a PRIMARY KEY and
     inserts use INSERT OR IGNORE, so a market loaded under two cohorts
     would have its second copy dropped *silently* — no error, just a
     quietly wrong row count.

The overlap is not hypothetical: "hall of fame" and "trophy" are in the
committee list and both are sports-adjacent. classify() surfaces those
rather than letting them land arbitrarily, and cohort_report() exists so
they can be inspected before a load rather than discovered afterwards.
"""

from __future__ import annotations

from collections import Counter

from polysignals.config import COHORTS


class UnknownCohortError(ValueError):
    """Raised when a caller names a cohort that isn't configured."""


def available() -> list[str]:
    """Configured cohort names, sorted for stable CLI help output."""
    return sorted(COHORTS)


def keywords_for(cohort: str) -> list[str]:
    """Keyword list for one cohort. Raises if the name is unknown, rather
    than returning [] — an empty list would silently match nothing and
    look like a data problem instead of a typo."""
    try:
        return COHORTS[cohort]
    except KeyError:
        raise UnknownCohortError(
            f"Unknown cohort {cohort!r}. Configured cohorts: {', '.join(available())}"
        ) from None


def matches(question: str, keywords: list[str]) -> bool:
    """True if any keyword appears in the question (case-insensitive)."""
    q = (question or "").lower()
    return any(kw in q for kw in keywords)


def matching_cohorts(question: str) -> list[str]:
    """Every cohort whose keywords match — usually zero or one."""
    return [name for name in available() if matches(question, COHORTS[name])]


def classify(question: str) -> str | None:
    """
    The single cohort this question belongs to, or None.

    None covers both "matches nothing" and "matches several" — neither is
    usable, and callers that need to tell them apart can use
    matching_cohorts() directly.
    """
    hits = matching_cohorts(question)
    return hits[0] if len(hits) == 1 else None


def is_in_cohort(question: str, cohort: str) -> bool:
    """
    True if this question belongs to `cohort` *and to no other*.

    This is the predicate the loaders filter on. Using classify() rather
    than a bare keyword match is what keeps the arms disjoint.
    """
    keywords_for(cohort)  # validate the name even when the answer is False
    return classify(question) == cohort


def cohort_report(questions) -> dict[str, int]:
    """
    Count how an iterable of questions distributes across cohorts.

    Returns counts per cohort plus two diagnostics:
      _ambiguous — matched more than one cohort, dropped from both
      _unmatched — matched none, outside every configured universe

    A large _ambiguous count means the keyword lists need tightening
    before the comparison means anything.
    """
    counts: Counter[str] = Counter()
    for question in questions:
        hits = matching_cohorts(question)
        if len(hits) == 1:
            counts[hits[0]] += 1
        elif hits:
            counts["_ambiguous"] += 1
        else:
            counts["_unmatched"] += 1

    report = {name: counts.get(name, 0) for name in available()}
    report["_ambiguous"] = counts.get("_ambiguous", 0)
    report["_unmatched"] = counts.get("_unmatched", 0)
    return report


def ambiguous_questions(questions, limit: int = 20) -> list[tuple[str, list[str]]]:
    """
    Sample of questions matching multiple cohorts, with the cohorts they
    matched. For eyeballing which keywords are colliding.
    """
    out: list[tuple[str, list[str]]] = []
    for question in questions:
        hits = matching_cohorts(question)
        if len(hits) > 1:
            out.append((question, hits))
            if len(out) >= limit:
                break
    return out
