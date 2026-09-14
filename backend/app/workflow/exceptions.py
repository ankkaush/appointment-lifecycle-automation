class WorkflowError(Exception):
    """Base class for errors raised by the workflow orchestration layer."""


class InvalidTransitionError(WorkflowError):
    """Raised when code attempts a ProcessingRun state transition that
    isn't in the deterministic transition table -- a bug, not a business
    outcome, so this should never be caught and silently ignored."""


class ProcessingRunNotFoundError(WorkflowError):
    def __init__(self, processing_run_id: object) -> None:
        self.processing_run_id = processing_run_id
        super().__init__(f"ProcessingRun {processing_run_id} not found")


class InvalidSlotChoiceError(WorkflowError):
    """The customer's chosen slot doesn't match any of the slots this
    ProcessingRun actually offered."""


class ProcessingRunStateError(WorkflowError):
    """The ProcessingRun isn't in a state that allows the requested
    action (e.g. confirming a run that isn't AWAITING_CONFIRMATION)."""
