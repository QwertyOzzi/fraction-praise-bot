"""CPU-only, trained tiny neural intent classifier for FBI commands.

Only intent is inferred; names/amount/category must be explicitly present.
No Firebase writes, permissions or API calls are possible here.
"""
from __future__ import annotations

from functools import lru_cache
import base64
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

FEATURES = 256
HIDDEN = 16
LABELS = ("credit", "debit", "other")
WEIGHTS_PATH = Path(__file__).with_name("tiny_neural_weights.json")
MODEL_VERSION = 2
CREDIT_STEMS = ("начисл", "добав", "выда", "выдай", "дай", "дайте", "прибав",
                "наки", "зачисл", "плюс", "постав", "присво", "подкин")
DEBIT_STEMS = ("сним", "снять", "сними", "спиш", "спис", "вычт", "вычес", "убер", "убир",
               "минус", "отним", "отнять", "забер", "забир", "штраф", "оштраф",
               "уменьш", "сниз", "сократ")
NOISE_WORDS = frozenset((
    "пожалуйста", "нужно", "надо", "можно", "можешь", "можете", "прошу",
    "сделай", "сделайте", "мне", "ему", "ей", "мы", "хотим", "хочу", "бы",
    "у", "для", "на", "по", "каждому", "каждой", "агенту", "агентам",
    "сотруднику", "сотрудникам", "балл", "балла", "баллов", "баллы",
    "очко", "очки", "очков", "давай", "давайте", "проведи", "проведите",
))
KIND_RE = re.compile(
    r"(?<!\w)(?P<kind>фрп|frp|отметк[иауы]?|отметок|"
    r"похвал(?:а|у|ы|е|ой|ою|ами|ам|ах)?|praise)(?!\w)", re.I
)
NUMBER_WORDS = {
    "один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2, "три": 3,
    "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8,
    "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
    "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15,
    "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19, "двадцать": 20,
}
NUMBER_RE = re.compile(r"(?<![\w])(?P<sign>[+-])?\s*(?P<num>\d{1,3})(?!\w)")
WORD_NUMBER_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True)) + r")(?!\w)", re.I
)
WORD_RE = re.compile(r"[a-zа-яё]+", re.I)


def _action_direction(word: str) -> str | None:
    word = word.casefold()
    if any(word.startswith(prefix) for prefix in CREDIT_STEMS):
        return "credit"
    if any(word.startswith(prefix) for prefix in DEBIT_STEMS):
        return "debit"
    return None


def action_features(text: str) -> str:
    """Feed intent cues only, never a nickname or profile, to the classifier."""
    normalized = text.casefold().replace("−", "-").replace("–", "-")
    cues = [w for w in WORD_RE.findall(normalized) if _action_direction(w)]
    signs = ["знакплюс" if m["sign"] == "+" else "знакминус"
             for m in NUMBER_RE.finditer(normalized) if m["sign"]]
    return " ".join(cues + signs)


def _vector(text: str) -> dict[int, float]:
    counts: dict[int, float] = {}
    for token in text.split():
        framed = "^" + token + "$"
        for k in (2, 3, 4):
            for i in range(len(framed) - k + 1):
                gram = framed[i:i + k]
                idx = int.from_bytes(
                    hashlib.blake2s(gram.encode("utf-8"), digest_size=4).digest(),
                    "big"
                ) % FEATURES
                counts[idx] = counts.get(idx, 0.0) + 1.0
    norm = math.sqrt(sum(v*v for v in counts.values())) or 1.0
    return {i: v / norm for i, v in counts.items()}


@lru_cache(maxsize=1)
def _weights():
    try:
        obj = json.loads(WEIGHTS_PATH.read_text(encoding="utf-8"))
        if (obj.get("version") != MODEL_VERSION or obj.get("labels") != list(LABELS)
                or obj.get("features") != FEATURES or obj.get("hidden") != HIDDEN):
            return None

        def decode(name: str, expected: int):
            packed = base64.b64decode(obj[name], validate=True)
            if len(packed) != expected:
                raise ValueError("wrong weight shape")
            scale = float(obj["scale_" + name])
            if not 0 < scale < 1:
                raise ValueError("wrong weight scale")
            return [(b if b < 128 else b - 256) * scale for b in packed]

        flat1 = decode("w1", FEATURES * HIDDEN)
        flat2 = decode("w2", HIDDEN * len(LABELS))
        b1 = decode("b1", HIDDEN)
        b2 = decode("b2", len(LABELS))
        w1 = [flat1[i*HIDDEN:(i+1)*HIDDEN] for i in range(FEATURES)]
        w2 = [flat2[i*len(LABELS):(i+1)*len(LABELS)] for i in range(HIDDEN)]
        return w1, b1, w2, b2
    except (OSError, ValueError, KeyError, TypeError, base64.binascii.Error):
        return None


def predict(text: str) -> tuple[str, float, float] | None:
    """Returns (class, probability, margin) or None if weights unavailable."""
    weights = _weights()
    if not weights:
        return None
    w1, b1, w2, b2 = weights
    hidden = list(b1)
    for idx, value in _vector(action_features(text)).items():
        row = w1[idx]
        for i in range(HIDDEN):
            hidden[i] += value * row[i]
    hidden = [max(0.0, x) for x in hidden]
    logits = [b2[i] + sum(hidden[j] * w2[j][i] for j in range(HIDDEN))
              for i in range(len(LABELS))]
    peak = max(logits)
    exp = [math.exp(min(0, x - peak)) for x in logits]
    probabilities = [v / sum(exp) for v in exp]
    best = max(range(len(LABELS)), key=lambda i: probabilities[i])
    second = max((p for i, p in enumerate(probabilities) if i != best), default=0.0)
    return LABELS[best], probabilities[best], probabilities[best] - second


def parse(text: str) -> dict[str, Any] | None:
    """No text generation; all extracted names, sums and sheet tokens are literal."""
    if not isinstance(text, str) or not 3 <= len(text) <= 400 or "|" in text:
        return None
    normalized = text.replace("−", "-").replace("–", "-").strip()
    kinds = list(KIND_RE.finditer(normalized))
    if len(kinds) != 1:
        return None
    kind = kinds[0]["kind"].casefold()
    sheet = "praise" if kind.startswith(("похвал", "praise")) else "fractionrp"
    remaining = normalized[:kinds[0].start()] + " " + normalized[kinds[0].end():]
    numeric = list(NUMBER_RE.finditer(remaining))
    written = list(WORD_NUMBER_RE.finditer(remaining))
    if len(numeric) + len(written) != 1:
        return None
    if numeric:
        match = numeric[0]
        amount, sign = int(match["num"]), match["sign"]
    else:
        match = written[0]
        amount, sign = NUMBER_WORDS[match.group().casefold()], None
    if not 1 <= amount <= 20:
        return None
    remaining = remaining[:match.start()] + " " + remaining[match.end():]
    detected_actions: set[str] = set()
    name_words = []
    for token in re.findall(r"[\w]+|[,;]", remaining, re.U):
        if token in (",", ";"):
            name_words.append(token)
        elif token.casefold() == "и":
            name_words.append(",")
        elif (direction := _action_direction(token)):
            detected_actions.add(direction)
        elif token.casefold() in NOISE_WORDS:
            continue
        else:
            name_words.append(token)
    if len(detected_actions) != 1:
        return None
    action = next(iter(detected_actions))
    if sign and action != ("credit" if sign == "+" else "debit"):
        return None
    names_text = " ".join(name_words).strip(" ,;")
    names = [n.strip() for n in re.split(r"\s*[,;]\s*", names_text) if n.strip()]
    if (not 1 <= len(names) <= 10
            or any(not 2 <= len(n) <= 40 for n in names)
            or any(re.search(r"\d", n) for n in names)
            or len({n.casefold() for n in names}) != len(names)):
        return None
    output = predict(normalized)
    if not output:
        return None
    label, probability, margin = output
    if label != action or probability < 0.90 or margin < 0.75:
        return None
    return {"action": action, "sheet": sheet, "amount": amount, "names": names}
