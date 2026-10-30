from __future__ import annotations


class VirtualSelfError(Exception):
    code: str = "internal_error"
    status_code: int = 500

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(VirtualSelfError):
    code = "not_found"
    status_code = 404


class ConflictError(VirtualSelfError):
    code = "conflict"
    status_code = 409


class ForbiddenError(VirtualSelfError):
    code = "forbidden"
    status_code = 403


class ValidationError(VirtualSelfError):
    code = "validation_error"
    status_code = 422


class PreconditionFailedError(VirtualSelfError):
    code = "precondition_failed"
    status_code = 412
