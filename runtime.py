"""Process ownership and bounded retries for read-only browser operations."""

import fcntl
import os
import time
from contextlib import contextmanager

from playwright.sync_api import TimeoutError as PWTimeout


class AlreadyRunning(RuntimeError):
    pass


class NetworkUnavailable(RuntimeError):
    """An operation could not complete reliably; stop consuming candidates."""


class TransientReadError(RuntimeError):
    pass


def is_network_error(error):
    message = str(error).lower()
    return isinstance(error, (PWTimeout, TransientReadError)) or any(
        marker in message
        for marker in (
            "net::err_network_changed", "net::err_connection_refused",
            "net::err_connection_reset", "net::err_connection_closed",
            "net::err_connection_aborted", "net::err_internet_disconnected",
            "net::err_name_not_resolved", "net::err_timed_out",
            "net::err_connection_timed_out", "net::err_address_unreachable",
            "net::err_network_access_denied", "failed to fetch",
            "chrome-error://chromewebdata/",
            "networkerror when attempting to fetch", "load failed",
        )
    )


def retry_read(operation, label, notify, delays=(5, 15, 30)):
    """Retry only explicitly read-only operations, never a submission POST."""
    for attempt in range(len(delays) + 1):
        try:
            return operation()
        except Exception as error:
            if not is_network_error(error):
                raise
            if attempt == len(delays):
                raise NetworkUnavailable(
                    f"{label} failed after {attempt + 1} tries: {error}"
                ) from error
            delay = delays[attempt]
            notify(f"{label}: connection unavailable; retrying in {delay}s "
                   f"({attempt + 2}/{len(delays) + 1})")
            time.sleep(delay)


@contextmanager
def exclusive_run(lock_path):
    """Hold a kernel lock through cleanup; a process exit releases it."""
    # Do not unlink the file: replacing the inode would allow a second owner.
    with open(lock_path, "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise AlreadyRunning(
                "Another bot command is using this profile and history. "
                "Wait for it to finish before starting another command."
            ) from error
        try:
            lock.seek(0)
            lock.truncate()
            lock.write(str(os.getpid()))
            lock.flush()
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
