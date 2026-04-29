# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Per-session singleton lock so two splitrandr processes can't race on
``~/.config/fakexrandr.bin``.
"""

import os


# Held for the lifetime of the process; the kernel releases the flock
# automatically when the fd is closed (process exit). Stored at module
# scope so it can't be GC'd by the caller.
_singleton_lock_fd = None


def _lock_path():
    runtime_dir = os.environ.get('XDG_RUNTIME_DIR') or '/tmp'
    return os.path.join(runtime_dir, 'splitrandr', 'splitrandr.lock')


def _acquire_singleton_lock():
    """Block a second splitrandr from running in the same user session.

    Two simultaneous splitrandr processes will both write
    ~/.config/fakexrandr.bin via the same .tmp file and race on
    os.replace. They also both try to manage Cinnamon's monitor state
    independently. This was observed on 2026-04-29 with PIDs 24514
    (autostart, no LD_PRELOAD) and 26244 (relaunched after a Cinnamon
    restart, with LD_PRELOAD inherited from cinnamon-session). The
    second instance was the one that started a feedback loop of
    crashes.

    Uses fcntl.flock on a file in $XDG_RUNTIME_DIR/splitrandr/. The
    lock is per-session; it does NOT span multiple X sessions on the
    same user account, which is what we want — different sessions
    have different X displays and need independent splitrandrs.

    Returns True if the lock was acquired (caller should keep
    running). Returns False if another instance already holds it
    (caller should log and exit cleanly).
    """
    global _singleton_lock_fd
    import fcntl
    lock_path = _lock_path()
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    except OSError:
        return True  # Can't create the dir — don't block on missing fs
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return True  # Can't open the lock file — fail open, don't block
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    # Stamp our PID into the file for diagnosis (`cat $XDG_RUNTIME_DIR/splitrandr/splitrandr.lock`).
    try:
        os.ftruncate(fd, 0)
        os.write(fd, ("%d\n" % os.getpid()).encode('ascii'))
    except OSError:
        pass
    _singleton_lock_fd = fd
    return True


def _signal_existing_instance():
    """Read the PID from the lock file and SIGUSR1 the running splitrandr.

    Returns True if a signal was delivered, False otherwise.  Used by a
    second-launch attempt (lock held → existing tray instance) to ask
    the existing process to raise its window instead of just exiting
    silently and looking broken to the user.
    """
    import signal
    try:
        with open(_lock_path(), 'r') as f:
            pid = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, signal.SIGUSR1)
        return True
    except (ProcessLookupError, PermissionError):
        return False
