class AutomationError(Exception):
    def __init__(self, code, message, details=None, retryable=False):
        super().__init__(message)
        self.code = code
        self.details = details or {}
        self.retryable = retryable

    def as_dict(self):
        return {"code": self.code, "message": str(self),
                "details": self.details, "retryable": self.retryable}
