"""Central mapping of errors to HTTP responses."""

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from tracksvc.api.schemas import ErrorDetail, ErrorResponse, FieldViolation
from tracksvc.application.errors import TrackNotFoundError
from tracksvc.domain.errors import TrackCodeTakenError


class DatabaseUnavailableError(Exception):
    """The readiness probe reported that the database is not ready."""

    def __init__(self) -> None:
        super().__init__("The database is not available.")


_STATUS_AND_CODE: dict[type[Exception], tuple[int, str]] = {
    DatabaseUnavailableError: (status.HTTP_503_SERVICE_UNAVAILABLE, "database_unavailable"),
    TrackNotFoundError: (status.HTTP_404_NOT_FOUND, "track_not_found"),
    TrackCodeTakenError: (status.HTTP_409_CONFLICT, "track_code_taken"),
}


def _field_violation(location: tuple[int | str, ...], message: str) -> FieldViolation:
    # The location starts with the part of the request ("body", "path"). What follows is
    # the path inside that part; it is empty when the body as a whole is rejected.
    source, *path = location
    return FieldViolation(field=".".join(map(str, path)) or str(source), message=message)


def _status_and_detail(error: Exception) -> tuple[int, ErrorDetail]:
    if isinstance(error, RequestValidationError):
        details = [_field_violation(item["loc"], item["msg"]) for item in error.errors()]
        return status.HTTP_422_UNPROCESSABLE_CONTENT, ErrorDetail(
            code="validation_error", message="The request is not valid.", details=details
        )
    status_code, code = _STATUS_AND_CODE[type(error)]
    return status_code, ErrorDetail(code=code, message=str(error))


async def _error_response(_request: Request, error: Exception) -> JSONResponse:
    status_code, detail = _status_and_detail(error)
    body = ErrorResponse(error=detail)
    return JSONResponse(status_code=status_code, content=body.model_dump(exclude_none=True))


def register_error_handlers(app: FastAPI) -> None:
    """Install the handler that translates errors into error responses."""
    for error_type in (*_STATUS_AND_CODE, RequestValidationError):
        app.add_exception_handler(error_type, _error_response)
