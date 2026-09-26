"""业务错误与 HTTP 状态码映射。"""


class AppError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status


class AuthError(AppError):
    status = 401
    code = "unauthorized"


class PermissionError_(AppError):
    status = 403
    code = "forbidden"


class NotFoundError(AppError):
    status = 404
    code = "not_found"


class ConflictError(AppError):
    status = 409
    code = "conflict"


class ConsentError(AppError):
    status = 403
    code = "consent_denied"


class ValidationError_(AppError):
    status = 422
    code = "validation_error"
