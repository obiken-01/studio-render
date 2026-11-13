import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

_HTTP_ERRORS = {
    404: ("not_found", "This address was not found."),
    405: ("method_not_allowed", "This action is not allowed here."),
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def ok(data) -> dict:
    return {"data": data, "message": None}


def _error(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"data": None, "message": message, "code": code, "details": details},
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return _error(exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        fields = [
            {"loc": [str(p) for p in e.get("loc", ())], "msg": str(e.get("msg", ""))}
            for e in exc.errors()
        ]
        return _error(422, "invalid_request", "The request is not valid.", {"errors": fields})

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        code, message = _HTTP_ERRORS.get(
            exc.status_code, ("http_error", "The request could not be handled."))
        response = _error(exc.status_code, code, message)
        for name, value in (exc.headers or {}).items():
            response.headers[name] = value
        return response

    # Normally RequestGuard (inside CORS) turns a crash into this envelope first; this is the
    # last line of defence for anything that escapes it.
    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        logger.error("Unhandled error", exc_info=exc)
        return _error(500, "server_error", "Something went wrong.")
