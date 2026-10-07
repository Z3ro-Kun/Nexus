"""Tool failures. Each carries a stable `error_type` that is recorded in ToolFailed events.

These never escape the tool executor: it converts them into a failed `ToolResult`.
"""


class ToolError(Exception):
    error_type = "execution_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnknownToolError(ToolError):
    error_type = "unknown_tool"


class ToolNotAuthorizedError(ToolError):
    error_type = "unauthorized"


class PolicyDeniedError(ToolError):
    """Phase 8: the policy engine denied the action; nothing was executed."""

    error_type = "policy_denied"


class ApprovalRequiredError(ToolError):
    """Phase 8: the action needs a human approval (an action task); nothing was executed."""

    error_type = "approval_required"


class InvalidToolArgumentsError(ToolError):
    error_type = "invalid_arguments"


class ToolTimeoutError(ToolError):
    error_type = "timeout"


class ToolNetworkError(ToolError):
    error_type = "network_error"


class ToolHTTPError(ToolError):
    error_type = "http_error"


class ToolSizeLimitError(ToolError):
    error_type = "size_limit"


class DestinationBlockedError(ToolError):
    """SSRF protection refused the destination."""

    error_type = "ssrf_blocked"


class ToolExecutionError(ToolError):
    error_type = "execution_error"


class ToolUnavailableError(ToolError):
    error_type = "unavailable"


class InvalidToolOutputError(ToolError):
    error_type = "invalid_output"
