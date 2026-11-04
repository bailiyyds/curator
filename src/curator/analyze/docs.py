"""Bounded TF-IDF with hierarchical topic clustering; no model downloads."""

import math
import re
from collections import Counter

from curator.core.plan import slugify


def terms(text: str) -> list[str]:
    """Tokenize Latin words and Chinese bigrams for local topic grouping."""
    words = re.findall(r"[a-zA-Z]{3,}|[\u3400-\u9fff]+", text.lower())
    result: list[str] = []
    for word in words:
        if word.isascii():
            if word not in {"the", "and", "with", "this", "that", "for", "from"}:
                result.append(word)
        else:
            result.extend(word[i : i + 2] for i in range(max(1, len(word) - 1)))
    return result


def topic_labels(documents: dict[str, str], max_topics: int = 12) -> dict[str, str]:
    """Cluster a bounded sample hierarchically, then assign remaining documents.

    Args:
        documents: Relative paths mapped to extracted text.
        max_topics: Label budget including the fallback group.
    """
    if not 2 <= max_topics <= 12:
        raise ValueError("Topic budget must be between 2 and 12")
    keys = sorted(documents)
    counts = {key: Counter(terms(documents[key][:20000])).most_common(128) for key in keys}
    frequency: Counter[str] = Counter(t for values in counts.values() for t, _ in values)
    vectors: dict[str, dict[str, float]] = {}
    for key in keys:
        values = {
            t: (1 + math.log(n)) * (1 + math.log((len(keys) + 1) / (frequency[t] + 1)))
            for t, n in counts[key]
        }
        norm = math.sqrt(sum(v * v for v in values.values())) or 1
        vectors[key] = {t: v / norm for t, v in values.items()}

    def similarity(a: dict[str, float], b: dict[str, float]) -> float:
        return sum(v * b.get(t, 0) for t, v in a.items())

    # Evenly spaced representatives bound the quadratic hierarchy to 64 documents.
    sample = [keys[i * len(keys) // min(64, len(keys))] for i in range(min(64, len(keys)))]
    groups = [[key] for key in sample if vectors[key]]

    def centroid(group: list[str]) -> dict[str, float]:
        total: dict[str, float] = {}
        for key in group:
            for term, weight in vectors[key].items():
                total[term] = total.get(term, 0) + weight
        norm = math.sqrt(sum(v * v for v in total.values())) or 1
        return {t: v / norm for t, v in total.items()}

    centers = [centroid(g) for g in groups]
    while len(groups) > 1:
        best = (0.12, -1, -1)
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                score = similarity(centers[i], centers[j])
                if score > best[0]:
                    best = score, i, j
        _, i, j = best
        if i < 0:
            break
        groups[i].extend(groups.pop(j))
        centers[i] = centroid(groups[i])
        centers.pop(j)
    selected = sorted(zip(groups, centers), key=lambda pair: (-len(pair[0]), pair[0][0]))[
        : max_topics - 1
    ]
    labels: list[str] = []
    for _, center in selected:
        title = slugify("-".join(sorted(center, key=lambda t: (-center[t], t))[:2]))
        candidate, number = title, 2
        while candidate in labels or candidate == "其他":
            candidate, number = f"{title[:70]}-{number}", number + 1
        labels.append(candidate)
    result: dict[str, str] = {}
    for key in keys:
        scores = [similarity(vectors[key], center) for _, center in selected]
        best_index = max(range(len(scores)), key=lambda i: scores[i]) if scores else -1
        result[key] = (
            labels[best_index] if best_index >= 0 and scores[best_index] >= 0.12 else "其他"
        )
    return result
