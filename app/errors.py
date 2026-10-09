"""The one exception services raise; the API layer maps it to HTTP responses later."""

from enum import StrEnum


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"  # 400
    FORBIDDEN_ROLE = "FORBIDDEN_ROLE"  # 403
    # The plan doesn't name the 404 code; rewire once the final API arrives.
    NOT_FOUND = "NOT_FOUND"  # 404
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"  # 405
    INVALID_TRANSITION = "INVALID_TRANSITION"  # 409
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"  # 501


class TracerError(Exception):
    """Carries the Error schema: error code, message and (for validation) a path-style field."""

    def __init__(self, code: ErrorCode, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field

    def to_dict(self) -> dict[str, str | None]:
        return {"error": self.code.value, "message": self.message, "field": self.field}

    def __repr__(self) -> str:
        return f"TracerError({self.code.value!r}, {self.message!r}, field={self.field!r})"
