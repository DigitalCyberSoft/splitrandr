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

import json
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
    """Get the PID of the live (non-zombie) cinnamon process."""
    try:
        result = subprocess.run(
            ['pgrep', '-x', 'cinnamon'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                pid_str = line.strip()
                if not pid_str:
                    continue
                pid = int(pid_str)
                # Skip zombie processes (state 'Z')
                try:
                    with open('/proc/%d/status' % pid) as f:
                        for sline in f:
                            if sline.startswith('State:'):
                                if 'Z' in sline:
                                    break
                                return pid
                except (OSError, PermissionError):
                    pass
    except Exception:
        pass
    return None


def _poll_until(predicate, timeout, interval=0.1, description=""):
    """Poll predicate() until it returns True or timeout expires.

    Returns True if predicate succeeded, False on timeout.
    """
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    if description:
        log.warning("poll timed out after %.1fs: %s", timeout, description)
    return False


def _wait_cinnamon_on_dbus(timeout=15.0):
    """Wait for Cinnamon's JS engine to be ready on D-Bus.

    Polls org.Cinnamon.Eval to check that the JS engine is up.
    Falls back to org.freedesktop.DBus.Peer.Ping.
    Returns True if Cinnamon responded, False on timeout.
    """
    log.info("waiting for Cinnamon D-Bus readiness (timeout=%.0fs)", timeout)

    def _cinnamon_eval_ready():
        result = subprocess.run(
            ['gdbus', 'call', '--session',
             '--dest', 'org.Cinnamon',
             '--object-path', '/org/Cinnamon',
             '--method', 'org.Cinnamon.Eval',
             'global.display.get_n_monitors()'],
            capture_output=True, text=True, timeout=3
        )
        return result.returncode == 0

    if _poll_until(_cinnamon_eval_ready, timeout, interval=0.25,
                   description="Cinnamon D-Bus Eval readiness"):
        log.info("Cinnamon is ready on D-Bus")
        return True

    # Fallback: try a simpler Ping
    log.info("Eval failed, trying D-Bus Peer.Ping fallback")

    def _cinnamon_ping_ready():
        result = subprocess.run(
            ['gdbus', 'call', '--session',
             '--dest', 'org.Cinnamon',
             '--object-path', '/org/Cinnamon',
             '--method', 'org.freedesktop.DBus.Peer.Ping'],
            capture_output=True, text=True, timeout=3
        )
        return result.returncode == 0

    if _poll_until(_cinnamon_ping_ready, 3.0, interval=0.25,
                   description="Cinnamon D-Bus Ping"):
        log.info("Cinnamon responded to Ping (Eval may still be initializing)")
        return True

    return False


def _cinnamon_eval(expr):
    """Run a JS expression via org.Cinnamon.Eval and return the string result.

    Eval JSON-stringifies the return value, so callers receive the JSON text.
    For simple values (numbers, booleans), this is just the literal (e.g. '2').
    For objects/arrays, it's a JSON string (e.g. '[{"x":0}]').

    Returns the result string on success, or None on failure.
    """
    try:
        result = subprocess.run(
            ['gdbus', 'call', '--session',
             '--dest', 'org.Cinnamon',
             '--object-path', '/org/Cinnamon',
             '--method', 'org.Cinnamon.Eval',
             expr],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return None
        # Output format: (true, 'value')  or  (false, 'error')
        out = result.stdout.strip()
        if not out.startswith('(true,'):
            return None
        # Extract the GVariant string between single quotes
        start = out.index("'") + 1
        end = out.rindex("'")
        raw = out[start:end]
        # Unescape GVariant string format: \\ → \, \' → '
        chars = []
        i = 0
        while i < len(raw):
            if raw[i] == '\\' and i + 1 < len(raw):
                chars.append(raw[i + 1])
                i += 2
            else:
                chars.append(raw[i])
                i += 1
        return ''.join(chars)
    except Exception as e:
        log.debug("cinnamon eval failed for %r: %s", expr, e)
        return None


def query_cinnamon_monitors():
    """Query Cinnamon's compositor for the actual monitor layout via DBUS.

    Returns a list of dicts on success:
        [{"name": "Samsung ...", "x": 0, "y": 0,
          "width": 3840, "height": 2160, "primary": False, "scale": 1}, ...]

    Returns None if Cinnamon is not running or DBUS query fails.
    """
    if not _is_cinnamon_running():
        return None

    # Single DBUS call: query all monitor data at once.
    # Don't use JSON.stringify — Eval already JSON-serializes the return value.
    js = (
        '(function() {'
        '  var n = global.display.get_n_monitors();'
        '  if (n <= 0) return [];'
        '  var p = global.display.get_primary_monitor();'
        '  var r = [];'
        '  for (var i = 0; i < n; i++) {'
        '    var g = global.display.get_monitor_geometry(i);'
        '    var name = "";'
        '    try { name = global.display.get_monitor_name(i); } catch(e) {}'
        '    var scale = 1;'
        '    try { scale = global.display.get_monitor_scale(i); } catch(e) {}'
        '    r.push({name: name, x: g.x, y: g.y,'
        '            width: g.width, height: g.height,'
        '            primary: (i === p), scale: scale});'
        '  }'
        '  return r;'
        '})()'
    )
    result_str = _cinnamon_eval(js)
    if result_str is None:
        return None

    try:
        monitors = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        log.warning("failed to parse Cinnamon monitor JSON: %r", result_str)
        return None

    if not isinstance(monitors, list) or len(monitors) == 0:
        return None

    log.info("queried %d monitors from Cinnamon DBUS", len(monitors))
    return monitors


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
                # Wait for gsettings to propagate
                def _gsettings_is_false():
                    r = subprocess.run(
                        ['gsettings', 'get',
                         'org.cinnamon.settings-daemon.plugins.xrandr', 'active'],
                        capture_output=True, text=True, timeout=5
                    )
                    return r.stdout.strip() == 'false'
                if not _poll_until(_gsettings_is_false, timeout=1.0, interval=0.05,
                                   description="gsettings propagation"):
                    time.sleep(0.3)  # fallback
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
            # X server round-trip to flush pending RandR events
            try:
                subprocess.run(
                    ['xrandr', '--listmonitors'],
                    capture_output=True, timeout=5
                )
            except Exception:
                time.sleep(0.2)  # fallback
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
