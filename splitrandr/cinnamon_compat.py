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


def _pid_has_fakexrandr_so(pid):
    """True if the PID has a non-system libXrandr.so mapped.

    During `cinnamon --replace`, both the outgoing and incoming WMs
    can be alive briefly. The incoming one is the one we just launched
    with LD_PRELOAD=libXrandr.so.2 (fakexrandr's wrapper); it has the
    .so mapped from the splitrandr/fakexrandr/ directory. The outgoing
    one either has nothing mapped (was started before LD_PRELOAD was
    introduced) or has the system libXrandr from /usr/lib*. This helper
    distinguishes the two by rejecting system-path mappings.
    """
    try:
        with open('/proc/%d/maps' % pid) as f:
            for line in f:
                if 'libXrandr.so' not in line:
                    continue
                # /proc/PID/maps line format:
                #   addr perms offset dev inode pathname
                parts = line.rstrip('\n').split(maxsplit=5)
                if len(parts) < 6:
                    continue
                path = parts[5].strip()
                if path.endswith(' (deleted)'):
                    path = path[:-len(' (deleted)')]
                # System libXrandr lives in /usr/lib* or /lib*; our
                # fakexrandr wrapper lives somewhere else (typically
                # under the splitrandr install path).
                if path.startswith('/usr/lib') or path.startswith('/lib'):
                    continue
                return True
    except (OSError, PermissionError):
        pass
    return False


def _get_cinnamon_pid():
    """Get the PID of the live (non-zombie) cinnamon WM process.

    During `cinnamon --replace` handoff (right after
    `restart_cinnamon_with_fakexrandr`), BOTH the outgoing and incoming
    cinnamons can be alive briefly. We want the incoming one — it has
    libXrandr.so.2 freshly LD_PRELOAD'd, will read fakexrandr.bin going
    forward, and is the process that CinnamonSetMonitorGuard must
    actually freeze. Naive `pgrep -x cinnamon` returns lines in
    PID-ascending order, so the *first* non-zombie was historically the
    OUTGOING cinnamon — Guard froze the wrong process, the new one ran
    unfrozen during the rm-bin window in save_to_x, and SEGV'd in
    `meta_display_logical_index_to_xinerama_index`.

    Heuristic, in order:
      1. Prefer non-zombie cinnamons whose /proc/PID/maps shows a
         non-system libXrandr.so (the LD_PRELOAD'd fakexrandr wrapper).
         Among those, prefer the highest PID (PIDs are monotonic
         within a session, so highest = youngest = the one that just
         started).
      2. If none have the .so loaded (e.g., on a fresh login before
         the first restart), fall back to the highest non-zombie PID.

    Returns None if no live cinnamon is found.
    """
    candidates = []
    try:
        result = subprocess.run(
            ['pgrep', '-x', 'cinnamon'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.strip().split('\n'):
            pid_str = line.strip()
            if not pid_str:
                continue
            try:
                pid = int(pid_str)
            except ValueError:
                continue
            # Skip zombies
            try:
                is_zombie = False
                with open('/proc/%d/status' % pid) as f:
                    for sline in f:
                        if sline.startswith('State:'):
                            is_zombie = 'Z' in sline
                            break
                if is_zombie:
                    continue
            except (OSError, PermissionError):
                continue
            candidates.append((pid, _pid_has_fakexrandr_so(pid)))
    except Exception:
        return None

    if not candidates:
        return None
    with_so = [pid for pid, has_so in candidates if has_so]
    if with_so:
        return max(with_so)
    return max(pid for pid, _ in candidates)


def _pid_is_cinnamon(pid):
    """Check that a PID still belongs to a cinnamon process."""
    try:
        with open('/proc/%d/comm' % pid) as f:
            return f.read().strip() == 'cinnamon'
    except (OSError, PermissionError):
        return False


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


def pin_panels_to_primary():
    """Rewrite ``org.cinnamon.panels-enabled`` so every panel sits on
    the current primary monitor.

    Cinnamon stores ``panels-enabled`` in **xinerama** monitor index
    space, but exposes ``Main.layoutManager.primaryIndex`` in
    **logical** index space. On Nvidia tiled hardware the two
    orderings differ (xinerama orders by output XID, logical by
    geometry), so the gsetting can't simply be set to the logical
    index. This function:

      1. Asks Cinnamon for ``primaryIndex`` (logical).
      2. Converts via ``global.display.logical_index_to_xinerama_index``.
      3. Reads ``panels-enabled``, rewrites every entry's monitor
         field to the resulting xinerama index, preserves panel ID
         and position type, writes back.

    Idempotent: if the gsetting already matches, it's not rewritten.
    No-op on Wayland (the conversion isn't applicable), if Cinnamon
    isn't on D-Bus, or if no primary monitor is set.

    Multi-panel caveat: every panel def is moved to the primary's
    xinerama. If the user has multiple panels on different monitors
    by intent, this will collapse them onto one. splitrandr's
    standard config has one panel; that's the case this is tuned
    for.
    """
    # Cinnamon's Eval JSON-stringifies the return value automatically;
    # do NOT call JSON.stringify inside the JS or the result is
    # double-encoded. Returns a plain object so json.loads on the
    # Python side gets a dict in one decode.
    #
    # Note: `Meta` is not in scope inside Cinnamon's Eval context
    # (only `global` and `Main` are). splitrandr is X11-only anyway,
    # so we just call logical_index_to_xinerama_index directly and
    # let any failure bubble up via the catch block as xmon=-2.
    expr = (
        '(function() {'
        '  var p = Main.layoutManager.primaryIndex;'
        '  if (p < 0) return {xmon:-1};'
        '  try {'
        '    return {'
        '      xmon: global.display.logical_index_to_xinerama_index(p),'
        '      lmon: p'
        '    };'
        '  } catch(e) {'
        '    return {xmon:-2, err: e.toString()};'
        '  }'
        '})()'
    )
    raw = _cinnamon_eval(expr)
    if raw is None:
        log.info("pin_panels_to_primary: cinnamon not on D-Bus, skipping")
        return
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("pin_panels_to_primary: failed to parse %r", raw)
        return
    xmon = data.get('xmon', -1)
    if xmon < 0:
        log.info("pin_panels_to_primary: no primary or wayland (xmon=%d)", xmon)
        return

    try:
        result = subprocess.run(
            ['gsettings', 'get', 'org.cinnamon', 'panels-enabled'],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            log.warning("pin_panels_to_primary: gsettings get failed: %s",
                        result.stderr.strip())
            return
        import ast
        current = ast.literal_eval(result.stdout.strip())
        if not isinstance(current, list):
            return
    except Exception as e:
        log.warning("pin_panels_to_primary: read panels-enabled failed: %s", e)
        return

    new_list = []
    for entry in current:
        parts = str(entry).split(':')
        if len(parts) != 3:
            new_list.append(entry)
            continue
        panel_id, _old_xmon, pos = parts
        new_list.append("%s:%d:%s" % (panel_id, xmon, pos))

    if new_list == list(current):
        log.info("pin_panels_to_primary: already on primary xinerama=%d", xmon)
        return

    formatted = "[" + ", ".join("'%s'" % e for e in new_list) + "]"
    try:
        subprocess.run(
            ['gsettings', 'set', 'org.cinnamon', 'panels-enabled', formatted],
            capture_output=True, timeout=5, check=True,
        )
        log.info("pin_panels_to_primary: %r -> %r (primary xinerama=%d, logical=%d)",
                 list(current), new_list, xmon, data.get('lmon', -1))
    except Exception as e:
        log.warning("pin_panels_to_primary: gsettings set failed: %s", e)


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
        if self._cinnamon_pid and _pid_is_cinnamon(self._cinnamon_pid):
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
            if _pid_is_cinnamon(self._cinnamon_pid):
                log.info("resuming Cinnamon (PID %d)", self._cinnamon_pid)
                try:
                    os.kill(self._cinnamon_pid, signal.SIGCONT)
                except OSError as e:
                    log.warning("failed to SIGCONT Cinnamon: %s", e)
            else:
                log.warning("PID %d is no longer Cinnamon, skipping SIGCONT",
                           self._cinnamon_pid)

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


def mutter_mode_list_matches_layout(splits_dict, xrandr_config):
    """Return True if Mutter's per-fake mode list contains a mode at the
    dimensions that splits_dict + xrandr_config would produce.

    Mutter caches the mode list per output and only refreshes when it
    receives certain RandR events. After splitrandr changes split
    geometry while Cinnamon is SIGSTOPped, Mutter's cache can end up
    stale: ApplyMonitorsConfig then rejects our new mode IDs as
    'Invalid mode'. When this returns False, the caller SHOULD restart
    Cinnamon (with LD_PRELOAD) so Mutter rebuilds its model from a
    fresh XRRGetScreenResources call.
    """
    from gi.repository import Gio
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        proxy = Gio.DBusProxy.new_sync(
            bus, Gio.DBusProxyFlags.NONE, None,
            'org.cinnamon.Muffin.DisplayConfig',
            '/org/cinnamon/Muffin/DisplayConfig',
            'org.cinnamon.Muffin.DisplayConfig',
            None,
        )
        state = proxy.call_sync(
            'GetCurrentState', None, Gio.DBusCallFlags.NONE, -1, None,
        )
    except Exception as e:
        log.warning("could not query Mutter for mode-list check: %s", e)
        return True  # don't trigger spurious restart on dbus failure

    _serial, mutter_monitors, _logical, _props = state.unpack()
    mutter_modes = {}
    for ((connector, _v, _p, _s), modes, _props) in mutter_monitors:
        # Each mode entry: (mode_id, w, h, refresh, scale, supported_scales, props)
        mutter_modes[connector] = {(int(m[1]), int(m[2])) for m in modes}

    for output_name, output_cfg in xrandr_config.outputs.items():
        if not output_cfg.active:
            continue
        ow, oh = output_cfg.size
        tree = splits_dict.get(output_name)
        if tree and not tree.is_leaf:
            for i, (rx, ry, rw, rh, _, _) in enumerate(
                    tree.leaf_regions(ow, oh)):
                connector = "%s~%d" % (output_name, i + 1)
                modes = mutter_modes.get(connector)
                if modes is None:
                    log.info("Mutter has no connector %s — needs reload", connector)
                    return False
                if (rw, rh) not in modes:
                    log.info("Mutter mode list for %s lacks %dx%d (has %s)",
                             connector, rw, rh, sorted(modes))
                    return False
        else:
            modes = mutter_modes.get(output_name)
            if modes is not None and (ow, oh) not in modes:
                log.info("Mutter mode list for %s lacks %dx%d", output_name, ow, oh)
                return False
    return True


def apply_monitors_via_dbus(splits_dict, xrandr_state, xrandr_config,
                            borders_dict=None, persistent=True):
    """Apply the layout to Cinnamon's Mutter directly via DBus
    (org.cinnamon.Muffin.DisplayConfig.ApplyMonitorsConfig).

    Bypasses csd-xrandr and the monitors.xml-trip — this is what
    cinnamon-settings-display calls internally when the user clicks
    Apply in its own UI. Required because csd-xrandr does not always
    reapply monitors.xml after our setmonitor commands while Mutter
    was SIGSTOPped, which leaves Mutter's cached mode list and
    logical_monitors stale on subsequent applies.

    Args:
        splits_dict, xrandr_state, xrandr_config, borders_dict: same
            shape as write_cinnamon_monitors_xml expects.
        persistent: True → method=2 (Mutter writes monitors.xml itself);
            False → method=1 (TEMPORARY, doesn't persist).

    Returns True if the apply succeeded, False if Mutter rejected.
    """
    from gi.repository import Gio, GLib

    rotation_to_transform = {
        'normal': 0, 'left': 1, 'inverted': 2, 'right': 3,
    }

    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        proxy = Gio.DBusProxy.new_sync(
            bus, Gio.DBusProxyFlags.NONE, None,
            'org.cinnamon.Muffin.DisplayConfig',
            '/org/cinnamon/Muffin/DisplayConfig',
            'org.cinnamon.Muffin.DisplayConfig',
            None,
        )
    except Exception as e:
        log.warning("DBus connection to Muffin.DisplayConfig failed: %s", e)
        return False

    # Pull current state for the serial AND for Mutter's known mode IDs.
    try:
        state = proxy.call_sync(
            'GetCurrentState', None, Gio.DBusCallFlags.NONE, -1, None,
        )
    except Exception as e:
        log.warning("GetCurrentState failed: %s", e)
        return False

    serial, mutter_monitors, _logical, _props = state.unpack()
    # Build connector → list-of-mode-ids dict from Mutter's view.
    mutter_modes = {}  # connector → [mode_id, ...]
    for ((connector, _vendor, _product, _serial), modes, _props) in mutter_monitors:
        mutter_modes[connector] = [m[0] for m in modes]

    def _pick_mode_id(connector, want_w, want_h):
        """Find a Mutter-known mode_id for connector matching want_w×want_h.
        Falls back to a plausible synthetic ID if Mutter has no match."""
        for mid in mutter_modes.get(connector, []):
            # Mutter mode IDs are typically 'WxH@RR'. Match on prefix.
            head = mid.split('@', 1)[0]
            try:
                w_s, h_s = head.split('x', 1)
                if int(w_s) == want_w and int(h_s) == want_h:
                    return mid
            except ValueError:
                continue
        return "%dx%d@60" % (want_w, want_h)

    logical_monitors = []

    for output_name, output_cfg in xrandr_config.outputs.items():
        if not output_cfg.active:
            continue
        ox, oy = output_cfg.position
        ow, oh = output_cfg.size
        rotation = output_cfg.rotation
        transform = rotation_to_transform.get(str(rotation), 0)
        tree = splits_dict.get(output_name)

        if tree and not tree.is_leaf:
            primary_leaf = tree.primary_leaf_index()
            if output_cfg.primary and primary_leaf is None:
                primary_leaf = 0
            elif not output_cfg.primary:
                primary_leaf = None
            for i, (rx, ry, rw, rh, _, _) in enumerate(
                    tree.leaf_regions(ow, oh)):
                connector = "%s~%d" % (output_name, i + 1)
                mode_id = _pick_mode_id(connector, rw, rh)
                logical_monitors.append((
                    ox + rx, oy + ry, 1.0, transform,
                    (i == primary_leaf),
                    [(connector, mode_id, {})],
                ))
        else:
            mode_id = _pick_mode_id(output_name, ow, oh)
            logical_monitors.append((
                ox, oy, 1.0, transform,
                bool(output_cfg.primary),
                [(output_name, mode_id, {})],
            ))

    # Mutter rejects an apply with zero primary monitors
    # ("Config is missing primary logical"). If the user hasn't picked
    # one, fall back to making the first logical monitor primary so the
    # apply at least lands.
    if logical_monitors and not any(lm[4] for lm in logical_monitors):
        first = logical_monitors[0]
        logical_monitors[0] = (first[0], first[1], first[2], first[3],
                               True, first[5])
        log.info("ApplyMonitorsConfig: no primary in input; defaulting %s",
                 first[5][0][0])

    method = 2 if persistent else 1
    args = GLib.Variant(
        '(uua(iiduba(ssa{sv}))a{sv})',
        (serial, method, logical_monitors, {}),
    )
    try:
        proxy.call_sync(
            'ApplyMonitorsConfig', args, Gio.DBusCallFlags.NONE, -1, None,
        )
        log.info("ApplyMonitorsConfig accepted (%d logical monitors, method=%d)",
                 len(logical_monitors), method)
        return True
    except GLib.Error as e:
        log.warning("ApplyMonitorsConfig rejected: %s", e.message)
        return False
    except Exception as e:
        log.warning("ApplyMonitorsConfig failed: %s", e)
        return False
