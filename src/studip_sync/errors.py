"""Domain-specific exceptions for studip-sync."""

from __future__ import annotations


class StudipSyncError(Exception):
    """Base class for errors that should be shown without a traceback."""


class ApiError(StudipSyncError):
    """A Stud.IP API request failed."""

    def __init__(self, url: str, status_code: int, detail: str = "") -> None:
        message = f"Stud.IP returned HTTP {status_code} for {url}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)
        self.url = url
        self.status_code = status_code


class SyncIncompleteError(StudipSyncError):
    """The sync completed as far as possible, but some files failed."""

    def __init__(self, failures: list[tuple[str, str]]) -> None:
        self.failures = failures
        super().__init__(
            f"Sync incomplete: {len(failures)} file(s) could not be downloaded"
        )
