"""Central mapping of errors to HTTP responses."""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from tracksvc.api.schemas import ErrorDetail, ErrorResponse


class DatabaseUnavailableError(Exception):
    """The readiness probe reported that the database is not ready."""


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    body = ErrorResponse(error=ErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status_code, content=body.model_dump())


async def _database_unavailable(_request: Request, _error: Exception) -> JSONResponse:
    return _error_response(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "database_unavailable",
        "The database is not available.",
    )


def register_error_handlers(app: FastAPI) -> None:
    """Install the handlers that translate errors into error responses."""
    app.add_exception_handler(DatabaseUnavailableError, _database_unavailable)
