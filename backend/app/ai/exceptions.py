class AIInterpretationError(Exception):
    """Raised when the AI layer fails or returns something that doesn't
    satisfy the structured contract. The caller must treat this as a
    failed interpretation — never fall back to default/guessed values and
    continue as if it had succeeded."""
