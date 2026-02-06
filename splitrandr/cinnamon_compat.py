# SplitRandR -- Cinnamon compatibility for xrandr --setmonitor
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Workarounds for Cinnamon/Muffin crash on xrandr --setmonitor.

Muffin >= 5.4.0 segfaults when processing RandR SetMonitor events
(https://github.com/linuxmint/muffin/issues/532). This module provides
a context manager that freezes Cinnamon during --setmonitor calls and
disables the csd-xrandr settings daemon plugin to prevent it from
reverting our changes.
"""

import os
import signal
import subprocess
import time
import logging

log = logging.getLogger('splitrandr')


def _get_muffin_version():
    """Return muffin version as (major, minor, patch) or None."""
    # Try muffin --version first
    try:
        result = subprocess.run(
            ['muffin', '--version'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                parts = line.strip().split()
                for part in parts:
                    segs = part.split('.')
                    if len(segs) >= 2 and all(s.isdigit() for s in segs):
                        return tuple(int(s) for s in segs)
    except Exception as e:
        log.debug("muffin --version failed: %s", e)

    # Fallback: rpm -q on Fedora/RHEL
    try:
        result = subprocess.run(
            ['rpm', '-q', '--qf', '%{VERSION}', 'muffin'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            segs = result.stdout.strip().split('.')
            if len(segs) >= 2 and all(s.isdigit() for s in segs):
                return tuple(int(s) for s in segs)
    except Exception as e:
        log.debug("rpm query for muffin failed: %s", e)

    # Fallback: dpkg on Debian/Ubuntu/Mint
    try:
        result = subprocess.run(
            ['dpkg-query', '-W', '-f', '${Version}', 'muffin'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            # Version like "6.0.1-1" — take part before the dash
            ver_str = result.stdout.strip().split('-')[0]
            segs = ver_str.split('.')
            if len(segs) >= 2 and all(s.isdigit() for s in segs):
                return tuple(int(s) for s in segs)
    except Exception as e:
        log.debug("dpkg query for muffin failed: %s", e)

    return None


def _is_cinnamon_running():
    """Check if Cinnamon window manager is running."""
    try:
        result = subprocess.run(
            ['pgrep', '-x', 'cinnamon'],
            capture_output=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def _get_cinnamon_pid():
    """Get the PID of the cinnamon process."""
    try:
        result = subprocess.run(
            ['pgrep', '-x', 'cinnamon'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            pids = result.stdout.strip().split('\n')
            if pids and pids[0]:
                return int(pids[0])
    except Exception:
        pass
    return None


def is_setmonitor_affected():
    """Check if the current system is affected by the --setmonitor crash.

    Returns True if Muffin >= 5.4.0 is installed and Cinnamon is running.
    """
    if not _is_cinnamon_running():
        return False

    version = _get_muffin_version()
    if version is None:
        # Can't detect version — assume affected if Cinnamon is running,
        # since most modern distros ship affected versions
        log.info("cannot detect muffin version, assuming affected")
        return True

    affected = (version[0] > 5) or (version[0] == 5 and version[1] >= 4)
    if affected:
        log.info("muffin %s is affected by setmonitor crash", '.'.join(str(v) for v in version))
    else:
        log.info("muffin %s is not affected", '.'.join(str(v) for v in version))
    return affected


class CinnamonSetMonitorGuard:
    """Context manager that makes xrandr --setmonitor safe on affected Cinnamon.

    On enter:
      1. Disables csd-xrandr plugin via gsettings (prevents it from fighting changes)
      2. SIGSTOPs the Cinnamon process (prevents segfault from RandR event handling)

    On exit:
      3. SIGCONTs the Cinnamon process
      4. Optionally re-enables csd-xrandr

    Usage:
        with CinnamonSetMonitorGuard():
            subprocess.run(['xrandr', '--setmonitor', ...])
    """

    def __init__(self, re_enable_csd=False):
        self._affected = False
        self._cinnamon_pid = None
        self._csd_was_active = None
        self._re_enable_csd = re_enable_csd
        self._frozen = False

    def __enter__(self):
        self._affected = is_setmonitor_affected()
        if not self._affected:
            return self

        # Step 1: Disable csd-xrandr via gsettings
        try:
            result = subprocess.run(
                ['gsettings', 'get',
                 'org.cinnamon.settings-daemon.plugins.xrandr', 'active'],
                capture_output=True, text=True, timeout=5
            )
            self._csd_was_active = result.stdout.strip() == 'true'
        except Exception:
            self._csd_was_active = None

        if self._csd_was_active:
            log.info("disabling csd-xrandr plugin")
            try:
                subprocess.run(
                    ['gsettings', 'set',
                     'org.cinnamon.settings-daemon.plugins.xrandr', 'active', 'false'],
                    capture_output=True, timeout=5
                )
                time.sleep(0.3)
            except Exception as e:
                log.warning("failed to disable csd-xrandr: %s", e)

        # Step 2: SIGSTOP Cinnamon
        self._cinnamon_pid = _get_cinnamon_pid()
        if self._cinnamon_pid:
            log.info("freezing Cinnamon (PID %d) for setmonitor safety", self._cinnamon_pid)
            try:
                os.kill(self._cinnamon_pid, signal.SIGSTOP)
                self._frozen = True
            except OSError as e:
                log.warning("failed to SIGSTOP Cinnamon: %s", e)

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self._affected:
            return False

        # Step 3: Resume Cinnamon
        if self._frozen and self._cinnamon_pid:
            time.sleep(0.2)  # Let X server settle
            log.info("resuming Cinnamon (PID %d)", self._cinnamon_pid)
            try:
                os.kill(self._cinnamon_pid, signal.SIGCONT)
            except OSError as e:
                log.warning("failed to SIGCONT Cinnamon: %s", e)

        # Step 4: Optionally re-enable csd-xrandr
        if self._re_enable_csd and self._csd_was_active:
            log.info("re-enabling csd-xrandr plugin")
            try:
                subprocess.run(
                    ['gsettings', 'set',
                     'org.cinnamon.settings-daemon.plugins.xrandr', 'active', 'true'],
                    capture_output=True, timeout=5
                )
            except Exception as e:
                log.warning("failed to re-enable csd-xrandr: %s", e)

        return False  # Don't suppress exceptions
