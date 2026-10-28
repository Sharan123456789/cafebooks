"""Storage errors whose messages are safe to show to the user."""


class StorageError(Exception):
    """Base class for storage problems that are safe to show to the user."""


class StartupError(StorageError):
    pass


class WorkbookCorruptError(StorageError):
    pass


class WorkbookSchemaError(StorageError):
    pass


class BusyError(StorageError):
    """Another save held the write lock for too long; nothing was changed."""
