"""Process ownership and bounded retries for read-only browser operations."""

import os
import time
from contextlib import contextmanager

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from patchright.sync_api import TimeoutError as PWTimeout


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
    with open(lock_path, "a+b") as lock:
        # Windows locks a fixed byte range, which must exist before locking.
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise AlreadyRunning(
                "Another bot command is using this profile and history. "
                "Wait for it to finish before starting another command."
            ) from error
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)
