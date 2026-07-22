# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Headless watcher: re-apply the active profile on screen-unlock,
suspend/wake, and display hotplug events.
"""

import os
import logging

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gdk, GLib, Gio

from . import profiles


_sw_log = logging.getLogger('splitrandr.screenwatcher')


class ScreenWatcher:
    """Watch for screen unlock and system wake events, re-apply layout.

    Listens on D-Bus for:
    - org.cinnamon.ScreenSaver ActiveChanged (Cinnamon lock/unlock)
    - org.freedesktop.ScreenSaver ActiveChanged (freedesktop lock/unlock)
    - org.gnome.ScreenSaver ActiveChanged (GNOME lock/unlock)
    - org.freedesktop.login1.Session Lock/Unlock (logind session)
    - org.freedesktop.login1.Manager PrepareForSleep (suspend/wake)

    Multiple signals firing in close succession are debounced into a
    single re-apply after REAPPLY_DELAY_SECS.
    """

    REAPPLY_DELAY_SECS = 3

    def __init__(self):
        self._subscriptions = []
        self._pending_reapply = None
        self._screen_signal_id = None
        self._setup_session_bus()
        self._setup_system_bus()
        self._setup_randr_monitor()

    def _sub(self, bus, sender, iface, signal, path):
        sub_id = bus.signal_subscribe(
            sender, iface, signal, path, None,
            Gio.DBusSignalFlags.NONE, self._on_signal)
        self._subscriptions.append((bus, sub_id))

    def _setup_session_bus(self):
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except Exception as e:
            _sw_log.warning("session bus unavailable: %s", e)
            return
        for svc, iface, path in [
            ('org.cinnamon.ScreenSaver',
             'org.cinnamon.ScreenSaver',
             '/org/cinnamon/ScreenSaver'),
            ('org.freedesktop.ScreenSaver',
             'org.freedesktop.ScreenSaver',
             '/org/freedesktop/ScreenSaver'),
            ('org.gnome.ScreenSaver',
             'org.gnome.ScreenSaver',
             '/org/gnome/ScreenSaver'),
        ]:
            self._sub(bus, svc, iface, 'ActiveChanged', path)
            _sw_log.info("subscribed to %s.ActiveChanged", iface)

    def _setup_system_bus(self):
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except Exception as e:
            _sw_log.warning("system bus unavailable: %s", e)
            return

        # Suspend/wake
        self._sub(bus, 'org.freedesktop.login1',
                  'org.freedesktop.login1.Manager',
                  'PrepareForSleep', '/org/freedesktop/login1')
        _sw_log.info("subscribed to logind PrepareForSleep")

        # Session Lock/Unlock
        try:
            result = bus.call_sync(
                'org.freedesktop.login1',
                '/org/freedesktop/login1',
                'org.freedesktop.login1.Manager',
                'GetSessionByPID',
                GLib.Variant('(u)', (os.getpid(),)),
                GLib.VariantType('(o)'),
                Gio.DBusCallFlags.NONE, -1, None)
            session_path = result.unpack()[0]
            for sig in ('Lock', 'Unlock'):
                self._sub(bus, 'org.freedesktop.login1',
                          'org.freedesktop.login1.Session',
                          sig, session_path)
            _sw_log.info("subscribed to logind session Lock/Unlock at %s",
                         session_path)
        except Exception as e:
            _sw_log.warning("logind session subscription failed: %s", e)

    def _setup_randr_monitor(self):
        """Watch for display hotplug events (monitor power loss/return)."""
        try:
            screen = Gdk.Screen.get_default()
            if screen:
                self._screen_signal_id = screen.connect(
                    'monitors-changed', self._on_monitors_changed)
                _sw_log.info("subscribed to Gdk monitors-changed")
            else:
                _sw_log.warning("no default GDK screen, skipping RandR monitor")
        except Exception as e:
            _sw_log.warning("GDK monitors-changed subscription failed: %s", e)

    def _on_monitors_changed(self, screen):
        _sw_log.info("display configuration changed (hotplug/power event)")
        # Drop the fakexrandr split VMs immediately. Otherwise Muffin
        # processes the incoming RandR hotplug with NAME~0..n still
        # registered, and the half-valid split set desyncs its
        # logical-monitor list: meta_monitor_manager_get_logical_monitor_
        # from_number asserts (index >= list length) and Cinnamon's JS
        # shell throws "monitor is undefined", wedging the session. The
        # debounced re-apply below re-establishes the split once the
        # monitor set stops flapping.
        self._teardown_splits_now()
        self._schedule_reapply()

    def _teardown_splits_now(self):
        """Delete the fakexrandr setmonitor VMs right now, so a monitor
        hotplug/power event reaches Muffin as the plain physical outputs
        instead of the split view.

        Muffin wedges on the split only during an *uncontrolled* RandR
        event -- a hardware disconnect it processes while NAME~0..n are
        registered. splitrandr's own setmonitor/delmonitor calls are
        already made safe by CinnamonSetMonitorGuard (muffin#532), so we
        reuse that guard here: enumerate the live VMs from the real
        (un-shimmed) xrandr and drop them with Cinnamon frozen.
        """
        import subprocess
        env = dict(os.environ)
        env.pop('LD_PRELOAD', None)  # bypass the shim -> see the real VMs
        try:
            out = subprocess.run(
                ['xrandr', '--listmonitors'],
                capture_output=True, text=True, timeout=5, env=env,
            ).stdout
        except Exception as e:
            _sw_log.warning("teardown: --listmonitors failed: %s", e)
            return
        vms = []
        for line in out.splitlines():
            # rows: " 0: HDMI-0~0 2496/786x648/204+3840+0  HDMI-0"
            parts = line.split()
            if len(parts) < 2 or not parts[0].rstrip(':').isdigit():
                continue
            name = parts[1].lstrip('+*')
            if '~' in name:
                vms.append(name)
        if not vms:
            _sw_log.info("teardown: no split VMs registered, nothing to drop")
            return
        _sw_log.info("teardown: dropping %d split VM(s) on hotplug: %s",
                     len(vms), ", ".join(vms))
        try:
            from .cinnamon_compat import CinnamonSetMonitorGuard
            with CinnamonSetMonitorGuard():
                for name in vms:
                    subprocess.run(
                        ['xrandr', '--delmonitor', name],
                        capture_output=True, timeout=5, env=env,
                    )
        except Exception as e:
            _sw_log.warning("teardown: delmonitor failed: %s", e)

    def _on_signal(self, conn, sender, path, iface, signal, params):
        if signal == 'ActiveChanged':
            active = params.unpack()[0]
            if active:
                # Screen locking — snapshot windows so we can restore
                # them when the user comes back. WMs sometimes shuffle
                # windows on lock screen activation.
                self._snapshot_windows()
            else:
                _sw_log.info("screen unlocked via %s", iface)
                self._schedule_reapply()
                self._restore_windows_after_delay()
        elif signal == 'PrepareForSleep':
            going_to_sleep = params.unpack()[0]
            if going_to_sleep:
                self._snapshot_windows()
            else:
                _sw_log.info("system waking from sleep")
                self._schedule_reapply()
                self._restore_windows_after_delay()
        elif signal == 'Lock':
            self._snapshot_windows()
        elif signal == 'Unlock':
            _sw_log.info("session unlocked via logind")
            self._schedule_reapply()
            self._restore_windows_after_delay()

    def _snapshot_windows(self):
        try:
            from . import window_layout
            self._window_snapshot = window_layout.capture()
        except Exception as e:
            _sw_log.warning("window snapshot failed: %s", e)
            self._window_snapshot = None

    def _restore_windows_after_delay(self):
        # Wait for the layout reapply (and any Cinnamon restart) to settle
        # before moving windows. Reapply timer is REAPPLY_DELAY_SECS; give
        # save_to_x another few seconds on top of that.
        snap = getattr(self, '_window_snapshot', None)
        if not snap:
            return
        delay = self.REAPPLY_DELAY_SECS + 5
        GLib.timeout_add_seconds(delay, self._do_restore_windows)

    def _do_restore_windows(self):
        snap = getattr(self, '_window_snapshot', None)
        if not snap:
            return False
        try:
            from . import window_layout
            window_layout.restore(snap)
        except Exception as e:
            _sw_log.warning("window restore failed: %s", e)
        self._window_snapshot = None
        return False  # one-shot timer

    def _schedule_reapply(self):
        if self._pending_reapply is not None:
            GLib.source_remove(self._pending_reapply)
        self._pending_reapply = GLib.timeout_add_seconds(
            self.REAPPLY_DELAY_SECS, self._do_reapply)

    def _do_reapply(self):
        self._pending_reapply = None
        active = profiles.get_active_profile()
        if not active:
            _sw_log.info("no active profile, skipping re-apply")
            return False
        if self._layout_matches(active):
            _sw_log.info("layout already correct, skipping re-apply")
            return False
        _sw_log.info("re-applying profile '%s'", active)
        try:
            profiles.apply_profile(active)
            _sw_log.info("profile '%s' re-applied successfully", active)
        except Exception as e:
            _sw_log.warning("failed to re-apply profile '%s': %s", active, e)
        return False

    @staticmethod
    def _layout_matches(profile_name):
        """Check if current X layout matches the profile without modifying anything.

        Both xrandr calls run with LD_PRELOAD stripped and therefore see
        the REAL server state. This function used to run a shimmed
        ``xrandr --query``: in a preloaded watcher process that query is
        synthesized from fakexrandr.bin, so it kept "matching" the
        profile even after ``_teardown_splits_now`` had deleted the real
        setmonitor VMs — the re-apply was skipped forever and every
        un-preloaded app (Evolution, anything D-Bus-activated) was left
        looking at unsplit monitors, popping menus on the wrong screen.
        The split VMs must be validated against the real server, where
        RandR emits no event for their absence (see
        nudge_gtk_monitor_refresh in fakexrandr_config).
        """
        import json, re, subprocess
        try:
            path = profiles.profile_path(profile_name)
            with open(path) as f:
                data = json.load(f)
        except Exception:
            return False

        expected_outputs = data.get('outputs', {})
        expected_splits = data.get('splits', {})

        env = dict(os.environ)
        env.pop('LD_PRELOAD', None)  # real server state, never the shim view

        # Query current output positions and modes
        try:
            raw = subprocess.run(
                ['xrandr', '--query'],
                capture_output=True, text=True, timeout=5, env=env,
            ).stdout
        except Exception:
            return False

        # Real setmonitor VMs, name -> (w, h, x, y). Rows look like
        # " 0: HDMI-0~0 2496/786x648/204+3840+0  HDMI-0".
        current_vms = {}
        try:
            mon_raw = subprocess.run(
                ['xrandr', '--listmonitors'],
                capture_output=True, text=True, timeout=5, env=env,
            ).stdout
            for line in mon_raw.splitlines():
                parts = line.split()
                if len(parts) < 3 or not parts[0].rstrip(':').isdigit():
                    continue
                m = re.match(r'(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)', parts[2])
                if m:
                    current_vms[parts[1].lstrip('+*')] = (
                        int(m.group(1)), int(m.group(2)),
                        int(m.group(3)), int(m.group(4)),
                    )
        except Exception:
            return False

        current = {}
        current_primary = None
        for line in raw.split('\n'):
            if line.startswith(('\t', ' ', 'Screen')):
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            name = parts[0]
            if 'primary' in parts:
                current_primary = name
            for p in parts[2:]:
                m = re.match(r'(\d+)x(\d+)\+(\d+)\+(\d+)', p)
                if m:
                    current[name] = (
                        int(m.group(1)), int(m.group(2)),
                        int(m.group(3)), int(m.group(4)),
                    )
                    break

        # Check each expected output's position and mode size against
        # the real query — the real server never hides split outputs,
        # so every active output must appear, split or not.
        for name, out_data in expected_outputs.items():
            if not out_data.get('active'):
                continue
            pos = out_data.get('position', [0, 0])
            mode = out_data.get('mode', '')
            try:
                mw, mh = mode.split('x')
                expected = (int(mw), int(mh), pos[0], pos[1])
            except (ValueError, AttributeError):
                return False
            if current.get(name) != expected:
                _sw_log.info("mismatch on %s: expected %s, got %s",
                            name, expected, current.get(name))
                return False

        # Check virtual outputs exist AND have the geometry the tree
        # would produce. If the user slid a split divider, the tree
        # changes but the fakes' existence doesn't — we must compare
        # actual geometry leaf-by-leaf.
        def _leaf_regions(tree_dict, w, h):
            """Yield (x, y, w, h) per leaf in spatial order (matches fakexrandr ~1, ~2, ...)."""
            if not tree_dict or not tree_dict.get('d'):
                yield (0, 0, w, h)
                return
            d = tree_dict['d']
            p = tree_dict.get('p', 0.5)
            if d == 'V':
                lw = int(round(w * p))
                yield from ((lx, ly, lwi, lh) for (lx, ly, lwi, lh)
                            in _leaf_regions(tree_dict.get('l'), lw, h))
                yield from ((lx + lw, ly, lwi, lh) for (lx, ly, lwi, lh)
                            in _leaf_regions(tree_dict.get('r'), w - lw, h))
            else:  # 'H'
                th = int(round(h * p))
                yield from ((lx, ly, lwi, lh) for (lx, ly, lwi, lh)
                            in _leaf_regions(tree_dict.get('l'), w, th))
                yield from ((lx, ly + th, lwi, lh) for (lx, ly, lwi, lh)
                            in _leaf_regions(tree_dict.get('r'), w, h - th))

        for output_name, tree_data in expected_splits.items():
            if not (tree_data and tree_data.get('d')):
                continue
            out_data = expected_outputs.get(output_name, {})
            try:
                mw, mh = out_data['mode'].split('x')
                pw, ph = int(mw), int(mh)
                px, py = out_data.get('position', [0, 0])
            except (KeyError, ValueError, AttributeError):
                continue
            for i, (lx, ly, lw, lh) in enumerate(_leaf_regions(tree_data, pw, ph)):
                # Real setmonitor VMs are 0-indexed: leaf 0 is NAME~0
                # (claiming the physical output), unlike the shim's
                # folded view where leaf 0 keeps the bare parent name.
                vm_name = "%s~%d" % (output_name, i)
                expected = (lw, lh, px + lx, py + ly)
                actual = current_vms.get(vm_name)
                if actual != expected:
                    _sw_log.info("split VM mismatch on %s: expected %s, got %s",
                                vm_name, expected, actual)
                    return False

        # Check primary output against the real query.
        expected_primary = next(
            (n for n, d in expected_outputs.items()
             if d.get('active') and d.get('primary')),
            None,
        )
        if expected_primary != current_primary:
            # Nvidia tiled-display hardware: --primary on a sub-tile
            # gets eaten by the driver's collapse/re-expand cycle,
            # so xrandr --query reports no primary at all. Treat
            # "X knows nothing about primary" as not-a-mismatch —
            # otherwise every monitors-changed event would loop us
            # back into a re-apply that can't make X agree anyway.
            if current_primary is None:
                return True
            _sw_log.info("primary mismatch: expected %s, got %s",
                        expected_primary, current_primary)
            return False

        return True

    def destroy(self):
        if self._pending_reapply is not None:
            GLib.source_remove(self._pending_reapply)
            self._pending_reapply = None
        for bus, sub_id in self._subscriptions:
            bus.signal_unsubscribe(sub_id)
        self._subscriptions.clear()
        if self._screen_signal_id is not None:
            screen = Gdk.Screen.get_default()
            if screen:
                screen.disconnect(self._screen_signal_id)
            self._screen_signal_id = None
