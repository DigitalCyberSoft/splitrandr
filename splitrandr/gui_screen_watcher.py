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
# Gdk must be pinned explicitly: this module imports Gdk without Gtk in the
# same statement, so when it is imported first (headless watcher, tests) Gdk
# resolves before Gtk has pulled in 3.0 and PyGI warns it may load Gdk 4.0.
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, GLib, Gio

from . import presence
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

    # Settle window after splitrandr's own RandR writes.
    #
    # _teardown_splits_now and apply_profile both issue --delmonitor /
    # --setmonitor (xrandr_save.py). RandR delivers those writes back to us
    # as Gdk 'monitors-changed', which _on_monitors_changed cannot tell apart
    # from a real hotplug -- so it tore the splits down again and re-applied,
    # forever. /tmp/splitrandr-gui.log recorded 448 laps of
    #   re-applied successfully -> monitors-changed -> teardown -> re-apply
    # each one SIGSTOPping Cinnamon for the setmonitor guard. Events arriving
    # while our own write is in flight, or within this window after it
    # finishes, are ours and MUST be ignored.
    SELF_EVENT_SETTLE_SECS = 5

    def __init__(self):
        self._subscriptions = []
        self._pending_reapply = None
        self._screen_signal_id = None
        self._self_write_depth = 0
        self._self_write_settle = None
        # (path, mtime_ns, size) -> parsed profile dict; the profile is
        # re-read on every event otherwise, and events come in bursts.
        self._profile_cache = None
        # Profile outputs currently missing from the real server; used
        # only to log degrade/restore transitions once instead of per
        # event. Behaviour is always derived from a fresh query.
        self._missing_prev = set()
        # Prime the state fingerprint so the first real event is
        # classified against the state at startup instead of always
        # counting as a change (one query subprocess, ~66 ms median on
        # this nvidia driver, off the hot path).
        self._last_state_fp, _ = presence.query_output_state()
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

    def _begin_self_randr_write(self):
        """Mark the start of a RandR write splitrandr is making itself."""
        self._self_write_depth += 1
        if self._self_write_settle is not None:
            GLib.source_remove(self._self_write_settle)
            self._self_write_settle = None

    def _end_self_randr_write(self):
        """Mark the end of our write and open the settle window.

        The window is required as well as the depth counter: RandR events are
        delivered asynchronously, so the last delmonitor/setmonitor of a batch
        is still in flight when the call that issued it returns.
        """
        if self._self_write_depth > 0:
            self._self_write_depth -= 1
        if self._self_write_depth:
            return
        if self._self_write_settle is not None:
            GLib.source_remove(self._self_write_settle)
        self._self_write_settle = GLib.timeout_add_seconds(
            self.SELF_EVENT_SETTLE_SECS, self._clear_self_write_settle)

    def _clear_self_write_settle(self):
        self._self_write_settle = None
        # Our own writes changed the real server state. Re-prime the
        # fingerprint now that they have settled, otherwise the next
        # external no-op event compares against the pre-apply state,
        # classifies itself as a change, and tears down the VMs we just
        # registered for nothing.
        self._last_state_fp, _ = presence.query_output_state()
        return False  # one-shot timer

    @property
    def _own_randr_write_pending(self):
        return self._self_write_depth > 0 or self._self_write_settle is not None

    def _on_monitors_changed(self, screen):
        if self._own_randr_write_pending:
            # Our own delmonitor/setmonitor coming back at us. Acting on it is
            # what produced the 448-lap teardown/re-apply loop; see
            # SELF_EVENT_SETTLE_SECS.
            _sw_log.info("display configuration changed -- ignored, this is "
                         "splitrandr's own RandR write settling")
            return
        # Classify the event before paying for it: one LD_PRELOAD-free
        # xrandr --query (measured 54-84 ms on this nvidia driver)
        # against the cached headline fingerprint. RandR delivers events whose output state is
        # byte-identical to the last one (output property spam, DPMS
        # EDID re-reads, other clients' no-op writes); reacting to those
        # used to delete all split VMs under a Cinnamon SIGSTOP and then
        # run a multi-second re-apply. Limitation: the fingerprint
        # covers outputs only, not the VM list -- an external client
        # deleting our VMs without touching outputs would be missed, but
        # nothing on this system does that and our own VM writes are
        # already filtered by the settle window above.
        fingerprint, _outputs = presence.query_output_state()
        if fingerprint is not None and fingerprint == self._last_state_fp:
            if self._pending_reapply is not None:
                # Mid-burst event with settled state: keep debouncing so
                # the re-apply fires after the last event, as before.
                _sw_log.info("display event, output state unchanged -- "
                             "re-arming pending re-apply")
                self._schedule_reapply()
            else:
                _sw_log.info("display event, output state unchanged -- "
                             "ignored")
            return
        self._last_state_fp = fingerprint
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
        self._begin_self_randr_write()
        try:
            from .cinnamon_compat import CinnamonSetMonitorGuard
            with CinnamonSetMonitorGuard():
                for name in vms:
                    subprocess.run(
                        ['xrandr', '--delmonitor', name],
                        capture_output=True, timeout=5, env=env,
                    )
        except Exception as e:
            _sw_log.warning("teardown: delmonitor failed: %s", e,
                            exc_info=True)
        finally:
            self._end_self_randr_write()

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
                # A lock cycle that went badly leaves pam-helpers / a
                # backup locker parented to systemd (two from the
                # 2026-08-02 12:40 geometry-flap lock failure were still
                # alive hours later). The reap is ppid-aware: helpers with
                # a live daemon parent are skipped, so this is safe on a
                # healthy unlock too.
                from .fakexrandr_config import reap_orphaned_lock_helpers
                reap_orphaned_lock_helpers()
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

    def _read_profile_cached(self, name):
        """Parsed profile dict, cached on (path, mtime, size).

        The watcher consults the profile on every debounced cycle;
        re-reading and re-parsing it each time is waste when it changes
        only on an explicit save. Returns None when missing/unreadable.
        """
        import json
        try:
            path = profiles.profile_path(name)
            stat = os.stat(path)
        except (OSError, ValueError) as e:
            _sw_log.warning("cannot stat profile '%s': %s", name, e)
            return None
        key = (path, stat.st_mtime_ns, stat.st_size)
        if self._profile_cache and self._profile_cache[0] == key:
            return self._profile_cache[1]
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            _sw_log.warning("cannot read profile '%s': %s", name, e)
            return None
        self._profile_cache = (key, data)
        return data

    def _log_presence_transition(self, missing):
        """Log degrade/restore transitions once, not on every event."""
        gone = set(missing)
        if gone == self._missing_prev:
            return
        if gone:
            _sw_log.warning(
                "profile output(s) no longer present: %s -- applying the "
                "layout without them until they return",
                ", ".join(sorted(gone)))
        else:
            _sw_log.info(
                "all profile outputs present again (%s returned) -- "
                "restoring the full layout",
                ", ".join(sorted(self._missing_prev)))
        self._missing_prev = gone

    @staticmethod
    def _apply_degraded(effective):
        """Apply a derived layout dict without touching any file on disk.

        Same pipeline as profiles.apply_profile minus the JSON file and
        minus set_active_profile: the active profile keeps the full
        layout (that is what restores it when the output returns), and
        the degraded layout MUST NOT be persisted -- a degraded state
        leaking into the profile is exactly the corruption class
        _safe_to_save_profile exists to prevent on the save side.
        """
        from .xrandr import XRandR
        xrandr = XRandR(force_version=True)
        xrandr.load_from_x()
        xrandr.load_from_dict(effective)
        xrandr.save_to_x(reason='watcher degraded apply')

    def _do_reapply(self):
        self._pending_reapply = None
        active = profiles.get_active_profile()
        if not active:
            _sw_log.info("no active profile, skipping re-apply")
            return False
        data = self._read_profile_cached(active)
        if data is None:
            _sw_log.warning("active profile '%s' unreadable, skipping "
                            "re-apply", active)
            return False
        fingerprint, outputs = presence.query_output_state()
        self._last_state_fp = fingerprint
        if not outputs:
            _sw_log.warning("cannot query output state, skipping re-apply")
            return False
        # A profile output that is unplugged or powered off (DP drops
        # its link) cannot be part of the applied layout: xrandr refuses
        # the mode, the apply dies, and every subsequent event retries
        # the same doomed command. Apply the profile minus the missing
        # outputs instead, and the full profile once they are all back.
        missing = presence.missing_profile_outputs(data, outputs)
        effective = presence.degrade_profile_data(data, missing) if missing \
            else data
        self._log_presence_transition(missing)
        if not any(o.get('active')
                   for o in effective.get('outputs', {}).values()):
            _sw_log.warning(
                "no profile output is connected (missing: %s) -- leaving "
                "display state untouched until one returns",
                ", ".join(missing))
            return False
        if self._layout_matches(effective, outputs, missing):
            # The layout is already right, so there is nothing to re-apply --
            # but that is exactly the case where windows come back from a
            # display dropout holding a stale front buffer and never redraw.
            # The repaint nudge is therefore NOT conditional on a re-apply.
            _sw_log.info("layout already correct, skipping re-apply "
                         "(still nudging windows to repaint)")
            self._nudge_repaint()
            return False
        if missing:
            _sw_log.info("re-applying profile '%s' without %s",
                         active, ", ".join(missing))
        else:
            _sw_log.info("re-applying profile '%s'", active)
        self._begin_self_randr_write()
        try:
            if missing:
                self._apply_degraded(effective)
                _sw_log.info("degraded layout applied successfully "
                             "(without %s)", ", ".join(missing))
            else:
                profiles.apply_profile(active)
                _sw_log.info("profile '%s' re-applied successfully", active)
        except Exception as e:
            _sw_log.warning("failed to re-apply profile '%s': %s", active, e,
                            exc_info=True)
        finally:
            # Opens the settle window BEFORE the setmonitor events land, so
            # they are recognised as ours instead of restarting the loop.
            self._end_self_randr_write()
        self._nudge_repaint()
        return False

    @staticmethod
    def _nudge_repaint():
        """Disabled. Do not re-arm without fixing the race described below.

        The intent is to force stale windows to redraw after a display dropout.
        The only mechanism that actually works is a resize (see
        window_layout.nudge_repaint), but running it unattended wrecked the
        live layout on 2026-07-25: 16 of 23 windows ended up the wrong size and
        position.

        Cause: maximized windows ignore an explicit resize, so the nudge
        removes maximized_vert/horz and re-adds it. Cinnamon applies the
        remembered *unmaximized* geometry asynchronously, and the re-add raced
        that, so windows settled at their small pre-maximize size. Recovery
        needed a per-window add-state, ~1.5s settle, then verify-and-retry.

        Re-arming requires that verify-and-retry loop per window, not a single
        batched write with one short settle. Until then this is a no-op --
        losing a repaint is an annoyance, scrambling a trading layout is not.
        Run it by hand instead: ``python3 -m splitrandr --nudge-repaint``.
        """
        return

    @staticmethod
    def _layout_matches(data, current_outputs, missing=()):
        """Check if current X layout matches a layout dict without modifying
        anything.

        ``data`` is a profile-shaped dict (the active profile, or the
        degraded variant from presence.degrade_profile_data), and
        ``current_outputs`` is presence.query_output_state's view of the
        REAL server -- the caller already paid for that query, so this
        function no longer runs its own. The one subprocess left here is
        ``xrandr --listmonitors`` for the setmonitor VMs, and only when
        ``data`` expects splits (or ``missing`` outputs could have stale
        VMs). It runs with LD_PRELOAD stripped: in a preloaded watcher
        process the shimmed view is synthesized from fakexrandr.bin, so
        it kept "matching" the profile even after ``_teardown_splits_now``
        had deleted the real setmonitor VMs — the re-apply was skipped
        forever and every un-preloaded app (Evolution, anything
        D-Bus-activated) was left looking at unsplit monitors, popping
        menus on the wrong screen. The split VMs must be validated
        against the real server, where RandR emits no event for their
        absence (see nudge_gtk_monitor_refresh in fakexrandr_config).
        """
        import re, subprocess

        expected_outputs = data.get('outputs', {})
        expected_splits = data.get('splits', {})

        current = {name: out['geometry']
                   for name, out in current_outputs.items()
                   if out['geometry'] is not None}
        current_primary = next(
            (name for name, out in current_outputs.items() if out['primary']),
            None,
        )

        # Real setmonitor VMs, name -> (w, h, x, y). Rows look like
        # " 0: HDMI-0~0 2496/786x648/204+3840+0  HDMI-0". Skipped when
        # no splits are expected and nothing is missing: the subprocess
        # only answers questions those two cases ask.
        need_vms = bool(missing) or any(
            tree and tree.get('d') for tree in expected_splits.values())
        current_vms = {}
        if need_vms:
            env = dict(os.environ)
            env.pop('LD_PRELOAD', None)  # real server state, never the shim
            try:
                mon_raw = subprocess.run(
                    ['xrandr', '--listmonitors'],
                    capture_output=True, text=True, timeout=5, env=env,
                ).stdout
            except (OSError, subprocess.SubprocessError):
                return False
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
            # A VM still claiming a missing output is a phantom monitor:
            # GTK places menus on it, windows land in dead space. Force
            # an apply; save_to_x deletes every ~-named VM up front.
            for vm_name in current_vms:
                if vm_name.rsplit('~', 1)[0] in missing:
                    _sw_log.info("stale VM %s claims a missing output",
                                 vm_name)
                    return False

        # Check each expected output's position and mode size against
        # the real query — the real server never hides split outputs,
        # so every active output must appear, split or not.
        for name, out_data in expected_outputs.items():
            if not out_data.get('active'):
                # An output deactivated because it vanished must really
                # be off: on nvidia a disconnected connector can keep
                # scanning out its old CRTC, which keeps the framebuffer
                # wide and lets the cursor and new windows drift into
                # dead space. Only enforced for `missing` -- outputs the
                # user turned off in the profile keep the old semantics
                # (not our business here).
                if name in missing and current.get(name) is not None:
                    _sw_log.info("missing output %s still has an active "
                                 "CRTC at %s", name, current.get(name))
                    return False
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
        if self._self_write_settle is not None:
            GLib.source_remove(self._self_write_settle)
            self._self_write_settle = None
        for bus, sub_id in self._subscriptions:
            bus.signal_unsubscribe(sub_id)
        self._subscriptions.clear()
        if self._screen_signal_id is not None:
            screen = Gdk.Screen.get_default()
            if screen:
                screen.disconnect(self._screen_signal_id)
            self._screen_signal_id = None
