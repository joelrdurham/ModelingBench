class ModelBenchError(Exception):
    """Expected user-facing harness error."""


class ValidationError(ModelBenchError):
    """A task, result, or artifact violated its contract."""


class StateError(ModelBenchError):
    """An operation is invalid for the current lifecycle state."""


class LockError(ModelBenchError):
    """Another mutating harness operation is active."""


class BlenderError(ModelBenchError):
    """Blender validation or rendering failed."""

