"""Errors with stable meanings at external boundaries."""


class ConfigurationError(ValueError):
    """Configuration is absent, malformed, or unsafe to infer."""


class ContractError(ValueError):
    """An external payload does not satisfy the documented contract."""


class AnnotationConflictError(RuntimeError):
    """The S3 object changed before its annotation could be written."""


class ExternalNotFoundError(LookupError):
    """An external resource does not exist."""


class ExternalConflictError(RuntimeError):
    """An external conditional operation lost a race."""


class ResultValidationError(ContractError):
    """Rovo output cannot safely drive finalization."""


class ContextUnavailableError(RuntimeError):
    """A required, externally synchronized input has not arrived yet."""
