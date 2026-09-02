"""Span-level (PolyWER-style) word error. Not a clinical quality claim.

Gold tokens come from AfriSwitchCare-style ``[[EN]]…[[/EN]]`` tags. A hypothesis
is scored the same way when it is tagged; an untagged hypothesis only yields an
overall WER, not per-language scores.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TAG = re.compile(r"\[\[([A-Z]+)\]\](.*?)\[\[/\1\]\]", re.S)
_TOKEN = re.compile(r"[A-Za-z0-9\u3400-\u9fff]+")


@dataclass(frozen=True)
class WerScore:
    language: str
    substitutions: int
    deletions: int
    insertions: int
    reference_tokens: int

    @property
    def wer(self) -> float | None:
        if self.reference_tokens == 0:
            return None
        return round(
            (self.substitutions + self.deletions + self.insertions)
            / self.reference_tokens,
            4,
        )


def tagged_tokens(tagged: str, *, matrix_language: str = "matrix") -> list[tuple[str, str]]:
    pieces: list[tuple[str, str]] = []
    cursor = 0
    for match in _TAG.finditer(tagged):
        if match.start() > cursor:
            pieces.append((matrix_language, tagged[cursor : match.start()]))
        pieces.append((match.group(1).lower(), match.group(2)))
        cursor = match.end()
    if cursor < len(tagged):
        pieces.append((matrix_language, tagged[cursor:]))
    tokens: list[tuple[str, str]] = []
    for language, chunk in pieces:
        for token in _TOKEN.findall(chunk):
            tokens.append((token.casefold(), language))
    return tokens


def _levenshtein(reference: list[str], hypothesis: list[str]) -> tuple[int, int, int]:
    """Return substitutions, deletions, insertions."""

    rows = len(reference)
    cols = len(hypothesis)
    dp = [[0] * (cols + 1) for _ in range(rows + 1)]
    ops = [[""] * (cols + 1) for _ in range(rows + 1)]
    for i in range(1, rows + 1):
        dp[i][0] = i
        ops[i][0] = "d"
    for j in range(1, cols + 1):
        dp[0][j] = j
        ops[0][j] = "i"
    for i in range(1, rows + 1):
        for j in range(1, cols + 1):
            if reference[i - 1] == hypothesis[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
                ops[i][j] = "m"
                continue
            delete = dp[i - 1][j] + 1
            insert = dp[i][j - 1] + 1
            subst = dp[i - 1][j - 1] + 1
            best = min(delete, insert, subst)
            dp[i][j] = best
            if best == subst:
                ops[i][j] = "s"
            elif best == delete:
                ops[i][j] = "d"
            else:
                ops[i][j] = "i"
    substitutions = deletions = insertions = 0
    i, j = rows, cols
    while i > 0 or j > 0:
        op = ops[i][j]
        if op == "m":
            i -= 1
            j -= 1
        elif op == "s":
            substitutions += 1
            i -= 1
            j -= 1
        elif op == "d":
            deletions += 1
            i -= 1
        elif op == "i":
            insertions += 1
            j -= 1
        else:
            break
    return substitutions, deletions, insertions


def score_wer(reference: list[str], hypothesis: list[str], *, language: str) -> WerScore:
    substitutions, deletions, insertions = _levenshtein(reference, hypothesis)
    return WerScore(
        language=language,
        substitutions=substitutions,
        deletions=deletions,
        insertions=insertions,
        reference_tokens=len(reference),
    )


def polywer(
    gold_tagged: str,
    hypothesis: str,
    *,
    matrix_language: str,
    hypothesis_tagged: bool,
) -> dict[str, WerScore]:
    gold = tagged_tokens(gold_tagged, matrix_language=matrix_language)
    if hypothesis_tagged:
        hyp = tagged_tokens(hypothesis, matrix_language=matrix_language)
    else:
        hyp = [(token.casefold(), "untagged") for token in _TOKEN.findall(hypothesis)]
    scores: dict[str, WerScore] = {
        "overall": score_wer(
            [token for token, _lang in gold],
            [token for token, _lang in hyp],
            language="overall",
        )
    }
    if not hypothesis_tagged:
        return scores
    languages = sorted({lang for _token, lang in gold})
    for language in languages:
        scores[language] = score_wer(
            [token for token, lang in gold if lang == language],
            [token for token, lang in hyp if lang == language],
            language=language,
        )
    return scores
