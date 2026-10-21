"""Jaro-Winkler string similarity (written in-house, no dependency), 0..1."""

from __future__ import annotations


def jaro(a: str, b: str) -> float:
    if a == b:
        return 1.0 if a else 0.0
    if not a or not b:
        return 0.0
    window = max(max(len(a), len(b)) // 2 - 1, 0)
    used = [False] * len(b)
    matched_a = []
    for i, ch in enumerate(a):
        for j in range(max(0, i - window), min(len(b), i + window + 1)):
            if not used[j] and b[j] == ch:
                used[j] = True
                matched_a.append(ch)
                break
    if not matched_a:
        return 0.0
    matched_b = [b[j] for j in range(len(b)) if used[j]]
    transpositions = sum(x != y for x, y in zip(matched_a, matched_b, strict=True)) / 2
    m = len(matched_a)
    return (m / len(a) + m / len(b) + (m - transpositions) / m) / 3


def jaro_winkler(a: str, b: str, prefix_scale: float = 0.1) -> float:
    j = jaro(a, b)
    prefix = 0
    for x, y in zip(a[:4], b[:4], strict=False):
        if x != y:
            break
        prefix += 1
    return j + prefix * prefix_scale * (1 - j)


def title_similarity(a: str, b: str) -> float:
    """Jaro-Winkler on normalised titles (case, punctuation and accents folded, as match.norm does)."""
    from .match import ascii_fold, norm

    na, nb = norm(a or ""), norm(b or "")
    if not na or not nb:
        return 0.0
    return max(jaro_winkler(na, nb), jaro_winkler(ascii_fold(na), ascii_fold(nb)))
