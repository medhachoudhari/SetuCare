class ServiceError(Exception):
    """Domain error rendered as {"error": {"code", "message", "details"}}."""

    def __init__(self, status_code: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status_code, self.code, self.message, self.details = status_code, code, message, details


def not_found(what: str = "Resource") -> ServiceError:
    return ServiceError(404, "not_found", f"{what} not found")


def forbidden(message: str = "You do not have permission to perform this action") -> ServiceError:
    return ServiceError(403, "forbidden", message)


def conflict(message: str, code: str = "conflict") -> ServiceError:
    return ServiceError(409, code, message)


def bad_request(message: str, code: str = "bad_request", details=None) -> ServiceError:
    return ServiceError(400, code, message, details)
