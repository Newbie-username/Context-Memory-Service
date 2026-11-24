class ServiceError(Exception):
    def __init__(self, status: int, code: str, **details: str):
        super().__init__(code)
        self.status = status
        self.code = code
        self.details = details


def denied() -> ServiceError:
    return ServiceError(403, "AUTH_SCOPE_DENIED")
