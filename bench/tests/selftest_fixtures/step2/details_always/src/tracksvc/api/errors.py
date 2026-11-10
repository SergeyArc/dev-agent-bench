from fastapi.responses import JSONResponse


def error_response(status: int, code: str, message: str, field: str | None = None) -> JSONResponse:
    """Build the single error body; `field` is given for a validation error only."""
    # Broken on purpose: `details` is present for every code, not only for validation_error.
    details = [] if field is None else [{"field": field, "message": message}]
    error: dict[str, object] = {"code": code, "message": message, "details": details}
    return JSONResponse({"error": error}, status_code=status)
