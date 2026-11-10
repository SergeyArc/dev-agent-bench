from fastapi.responses import JSONResponse


def error_response(status: int, code: str, message: str, field: str | None = None) -> JSONResponse:
    """Build the single error body; `field` is given for a validation error only."""
    error: dict[str, object] = {"code": code, "message": message}
    if field is not None:
        error["details"] = [{"field": field, "message": message}]
    return JSONResponse({"error": error}, status_code=status)
