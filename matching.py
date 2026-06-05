from __future__ import annotations

from thefuzz import fuzz
from pypinyin import lazy_pinyin, Style
from sqlalchemy.orm import Session as OrmSession

from models import GameSession, Participant, SessionEntry


def to_pinyin(text: str) -> str:
    """Convert Chinese text to joined pinyin without tones, e.g. 陈威 → chenwei."""
    return "".join(lazy_pinyin(text, style=Style.NORMAL)).lower()


def pinyin_variants(name: str) -> list[str]:
    """Return multiple pinyin representations of a name."""
    chars = lazy_pinyin(name, style=Style.NORMAL)
    variants = ["".join(chars).lower()]
    if len(chars) > 1:
        variants.append(" ".join(chars).lower())
    # Surname + given name concatenated without middle syllables for 3-char names
    if len(chars) == 3:
        variants.append((chars[0] + chars[2]).lower())
    return variants


def _best_fuzz(query: str, target: str) -> float:
    if not query or not target:
        return 0.0
    return max(
        fuzz.ratio(query, target),
        fuzz.partial_ratio(query, target),
        fuzz.token_sort_ratio(query, target),
    )


def score_candidate(reference: str, sender_name: str, participant: Participant) -> float:
    """Compute the best fuzzy match score across all name representations."""
    ref = reference.lower().strip()
    sender = sender_name.lower().strip()

    searchables: list[str] = []
    if participant.chinese_name:
        searchables.append(participant.chinese_name.lower())
        searchables.extend(pinyin_variants(participant.chinese_name))
    if participant.pinyin:
        if participant.pinyin not in searchables:
            searchables.append(participant.pinyin)
    if participant.known_references:
        searchables.extend(r.lower() for r in participant.known_references)

    best = 0.0
    for query in [ref, sender]:
        for target in searchables:
            best = max(best, _best_fuzz(query, target))
    return best


def find_matches(
    reference: str,
    sender_name: str,
    amount: float,
    db: OrmSession,
    session_id: int | None = None,
) -> list[dict]:
    """
    Return up to 5 candidates sorted by score descending.
    When session_id is given, only unpaid entries in that session are considered.
    """
    if session_id:
        entries = (
            db.query(SessionEntry)
            .filter(SessionEntry.session_id == session_id, SessionEntry.paid == False)
            .all()
        )
        candidates = [(e.participant, e) for e in entries]
    else:
        participants = db.query(Participant).all()
        candidates = [(p, None) for p in participants]

    results = []
    for participant, entry in candidates:
        score = score_candidate(reference, sender_name, participant)
        # Small boost when the amount exactly matches what the participant owes
        if entry and abs(entry.amount_owed - amount) < 0.01:
            score = min(100.0, score + 10.0)
        results.append({"participant": participant, "entry": entry, "score": score})

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:5]
