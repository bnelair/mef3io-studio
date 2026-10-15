"""Markers for user-facing text. See docs/ui-language.md."""


def technical(text: str) -> str:
    """Mark ``text`` as shown only inside a "Technical details" panel, where
    technical terms are allowed. The UI-language test skips these."""
    return text
