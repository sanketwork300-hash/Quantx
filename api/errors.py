"""RFC 7807-style problem responses.

HTTP status expresses whether the *request* was valid. Whether a *calculation*
succeeded is expressed by ``ResultStatus`` inside a 200 envelope, so a partially
computable analysis never becomes a 500 that tells the user nothing.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from infrastructure.observability.logging import get_correlation_id, get_logger

logger = get_logger(__name__)

BASE_URI = "https://qip.dev/errors"


class ProblemDetail(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        title: str,
        detail: str | None = None,
        **extra,
    ) -> None:
        super().__init__(detail or title)
        self.status_code = status_code
        self.code = code
        self.title = title
        self.detail = detail
        self.extra = extra

    def to_response(self) -> JSONResponse:
        payload = {
            "type": f"{BASE_URI}/{self.code.lower().replace('_', '-')}",
            "title": self.title,
            "status": self.status_code,
            "code": self.code,
            "detail": self.detail,
            "correlation_id": get_correlation_id(),
            **self.extra,
        }
        return JSONResponse(status_code=self.status_code, content=payload)


class NotFound(ProblemDetail):
    """404 for a missing resource *and* for a resource owned by someone else.

    Returning 403 for a foreign resource confirms that the id exists, which
    makes ids enumerable. 404 is the correct answer to "is this yours?".
    """

    def __init__(self, resource: str, detail: str | None = None) -> None:
        super().__init__(
            status.HTTP_404_NOT_FOUND,
            "RESOURCE_NOT_FOUND",
            f"{resource} not found",
            detail or f"No accessible {resource.lower()} matches that identifier.",
        )


class Unauthorized(ProblemDetail):
    def __init__(self, detail: str = "Authentication is required.") -> None:
        super().__init__(status.HTTP_401_UNAUTHORIZED, "UNAUTHORIZED", "Unauthorized", detail)


class BadRequest(ProblemDetail):
    def __init__(self, code: str, detail: str, **extra) -> None:
        super().__init__(status.HTTP_400_BAD_REQUEST, code, "Bad request", detail, **extra)


#: Starlette renamed its 422 constant; the numeric code is stable and the
#: rename churns with the dependency, so it is written literally here.
HTTP_422_UNPROCESSABLE = 422


class UnprocessableEntity(ProblemDetail):
    def __init__(self, code: str, detail: str, **extra) -> None:
        super().__init__(HTTP_422_UNPROCESSABLE, code, "Unprocessable entity", detail, **extra)


class Conflict(ProblemDetail):
    def __init__(self, code: str, detail: str, **extra) -> None:
        super().__init__(status.HTTP_409_CONFLICT, code, "Conflict", detail, **extra)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemDetail)
    async def _problem(_request: Request, exc: ProblemDetail) -> JSONResponse:
        return exc.to_response()

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=HTTP_422_UNPROCESSABLE,
            content={
                "type": f"{BASE_URI}/request-validation-failed",
                "title": "Request validation failed",
                "status": HTTP_422_UNPROCESSABLE,
                "code": "REQUEST_VALIDATION_FAILED",
                "detail": "One or more fields failed validation.",
                "errors": [
                    {
                        "location": list(error.get("loc", [])),
                        "message": error.get("msg"),
                        "type": error.get("type"),
                    }
                    for error in exc.errors()
                ],
                "correlation_id": get_correlation_id(),
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "type": f"{BASE_URI}/http-error",
                "title": "HTTP error",
                "status": exc.status_code,
                "code": "HTTP_ERROR",
                "detail": str(exc.detail),
                "correlation_id": get_correlation_id(),
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception", error=str(exc))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "type": f"{BASE_URI}/internal-error",
                "title": "Internal server error",
                "status": 500,
                "code": "INTERNAL_ERROR",
                # No exception text: it can carry connection strings and paths.
                "detail": "An unexpected error occurred. Quote the correlation id.",
                "correlation_id": get_correlation_id(),
            },
        )
