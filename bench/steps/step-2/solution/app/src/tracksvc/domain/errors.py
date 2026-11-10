"""Errors raised when a business rule rejects an operation."""


class TrackCodeTakenError(Exception):
    """Another track already has the requested code."""

    def __init__(self, code: str) -> None:
        super().__init__(f"Track code {code!r} is already taken.")
        self.code = code
