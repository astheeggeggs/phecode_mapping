from __future__ import annotations

import re


def normalize_code(code: str | None, vocabulary: str | None) -> str | None:
    """Normalize only presentation differences; do not derive parent codes."""
    if code is None or vocabulary is None:
        return None
    value = str(code).strip().upper()
    vocab = str(vocabulary).strip().upper()
    if not value:
        return None
    if vocab in {"ICD9CM", "ICD10CM", "ICD10"}:
        return re.sub(r"[.\s-]", "", value)
    if vocab == "SNOMED":
        return re.sub(r"\s+", "", value)
    return value
