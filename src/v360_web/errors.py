class CameraError(RuntimeError):
    """An operational error whose public message never contains device credentials."""

    def __init__(self, code: str, message: str, sdk_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.sdk_code = sdk_code

    def as_dict(self):
        return {"error": self.code, "message": str(self), "sdk_code": self.sdk_code}
