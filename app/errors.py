"""The one exception services raise; the API layer turns it into an HTTP response."""

from enum import StrEnum


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    FORBIDDEN_ROLE = "FORBIDDEN_ROLE"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    CONFLICT = "CONFLICT"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


# Each code's HTTP status, from the spec's shared responses.
HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.FORBIDDEN_ROLE: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.METHOD_NOT_ALLOWED: 405,
    ErrorCode.INVALID_TRANSITION: 409,
    ErrorCode.CONFLICT: 409,
    ErrorCode.NOT_IMPLEMENTED: 501,
}


class TracerError(Exception):
    """Carries the Error schema: error code, message and (for validation) a path-style field."""

    def __init__(self, code: ErrorCode, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field

    @property
    def status(self) -> int:
        return HTTP_STATUS[self.code]

    def to_dict(self) -> dict[str, str | None]:
        return {"error": self.code.value, "message": self.message, "field": self.field}

    def __repr__(self) -> str:
        return f"TracerError({self.code.value!r}, {self.message!r}, field={self.field!r})"
