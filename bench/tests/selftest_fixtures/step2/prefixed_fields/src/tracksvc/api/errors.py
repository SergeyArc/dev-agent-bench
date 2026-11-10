from fastapi.responses import JSONResponse


def _location(field: str) -> str:
    if field == "body":
        return field
    return f"path.{field}" if field == "track_id" else f"body.{field}"


def error_response(status: int, code: str, message: str, field: str | None = None) -> JSONResponse:
    """Build the single error body; `field` is given for a validation error only."""
    error: dict[str, object] = {"code": code, "message": message}
    if field is not None:
        # Broken on purpose: the location keeps the `body.` / `path.` prefix.
        error["details"] = [{"field": _location(field), "message": message}]
    return JSONResponse({"error": error}, status_code=status)
