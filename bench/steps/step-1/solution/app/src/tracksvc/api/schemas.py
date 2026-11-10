"""Response bodies of the HTTP API."""

from typing import Literal

from pydantic import BaseModel


class StatusResponse(BaseModel):
    """Body of a successful operational endpoint."""

    status: Literal["ok", "ready"]


class ErrorDetail(BaseModel):
    """Machine-readable code and human-readable message of an error."""

    code: str
    message: str


class ErrorResponse(BaseModel):
    """Body of every error response."""

    error: ErrorDetail
