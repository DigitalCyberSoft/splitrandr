# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Mixin: serialise the current configuration to a shellscript or push
it to the live X server. Intended to be mixed into ``XRandR``.
"""

import json
import os
import re
import shlex
import time
import logging

from .auxiliary import InadequateConfiguration
from .splits import SplitTree
from .i18n import _

log = logging.getLogger('splitrandr')


def _restart_sn_watcher():
    """Restart xapp-sn-watcher so it picks up the new monitor layout.

    The sn-watcher's GDK caches monitor geometry at startup. When
    setmonitor VMs change the layout, the cached model goes stale,
    causing AppIndicator3 menus to pop up on the wrong monitor.
    Killing the watcher lets D-Bus auto-restart it with fresh state.
    """
    import subprocess
    try:
        result = subprocess.run(
            ['pkill', '-x', 'xapp-sn-watcher'],
            capture_output=True, timeout=5
        )
        if result.returncode == 0:
            log.info("restarted xapp-sn-watcher for monitor layout update")
    except Exception:
        pass


class XRandRSaveMixin:

    def save_to_shellscript_string(self):
        template = '#!/bin/sh\n%(pre_commands)s\n%(clear_fakexrandr)s\n%(xrandr)s\n%(cinnamon_safe_setmonitors)s\n'

        # Build delmonitor + setmonitor commands
        del_lines = []
        set_lines = []
        for output_name, tree in self.configuration.splits.items():
            output_cfg = self.configuration.outputs.get(output_name)
            if not output_cfg or not output_cfg.active:
                continue

            output_state = self.state.outputs.get(output_name)
            w_mm = output_state.physical_w_mm if output_state else 0
            h_mm = output_state.physical_h_mm if output_state else 0
            border = self.configuration.borders.get(output_name, 0)
            commands = tree.to_setmonitor_commands(
                output_name,
                output_cfg.size[0], output_cfg.size[1],
                output_cfg.position[0], output_cfg.position[1],
                w_mm, h_mm, border
            )
            for mon_name, geom, out in commands:
                del_lines.append("env -u LD_PRELOAD xrandr --delmonitor %s 2>/dev/null || true" % shlex.quote(mon_name))
                set_lines.append("env -u LD_PRELOAD xrandr --setmonitor %s %s %s" % (shlex.quote(mon_name), shlex.quote(geom), shlex.quote(out)))

        # Generate setmonitor for unsplit outputs that have a border
        for output_name, border_val in self.configuration.borders.items():
            if border_val <= 0 or output_name in self.configuration.splits:
                continue
            output_cfg = self.configuration.outputs.get(output_name)
            if not output_cfg or not output_cfg.active:
                continue
            output_state = self.state.outputs.get(output_name)
            w_mm = output_state.physical_w_mm if output_state else 0
            h_mm = output_state.physical_h_mm if output_state else 0
            w, h = output_cfg.size
            ox, oy = output_cfg.position
            bw = max(w - 2 * border_val, 1)
            bh = max(h - 2 * border_val, 1)
            mon_name = "%s~0" % output_name
            geom = "%d/%dx%d/%d+%d+%d" % (bw, w_mm, bh, h_mm, ox + border_val, oy + border_val)
            del_lines.append("env -u LD_PRELOAD xrandr --delmonitor %s 2>/dev/null || true" % shlex.quote(mon_name))
            set_lines.append("env -u LD_PRELOAD xrandr --setmonitor %s %s %s" % (shlex.quote(mon_name), shlex.quote(geom), shlex.quote(output_name)))

        # Generate border comments for persistence
        border_comments = []
        for output_name, border_val in self.configuration.borders.items():
            if border_val > 0:
                border_comments.append(
                    '# splitrandr-border:%s=%d' % (output_name, border_val))

        # Generate Cinnamon-safe wrapper for setmonitor commands
        # Muffin >= 5.4.0 segfaults on setmonitor events, so we
        # SIGSTOP Cinnamon and disable csd-xrandr during these calls.
        if del_lines or set_lines:
            monitor_cmds = '\n'.join(del_lines + set_lines)
            cinnamon_safe = (
                '# Cinnamon safety: freeze Cinnamon during setmonitor calls\n'
                'CINNAMON_PID=$(pgrep -x cinnamon 2>/dev/null)\n'
                'if [ -n "$CINNAMON_PID" ]; then\n'
                '  gsettings set org.cinnamon.settings-daemon.plugins.xrandr active false 2>/dev/null\n'
                '  # Wait for gsettings to propagate\n'
                '  _i=0; while [ "$_i" -lt 20 ]; do\n'
                '    _v=$(gsettings get org.cinnamon.settings-daemon.plugins.xrandr active 2>/dev/null)\n'
                '    [ "$_v" = "false" ] && break\n'
                '    sleep 0.05; _i=$((_i+1))\n'
                '  done\n'
                '  kill -STOP "$CINNAMON_PID" 2>/dev/null\n'
                'fi\n'
                + monitor_cmds + '\n'
                + ('\n'.join(border_comments) + '\n' if border_comments else '')
                + 'if [ -n "$CINNAMON_PID" ]; then\n'
                '  # X server round-trip to flush pending RandR events\n'
                '  xrandr --listmonitors >/dev/null 2>&1\n'
                '  kill -CONT "$CINNAMON_PID" 2>/dev/null\n'
                'fi\n'
                '# Restart xapp-sn-watcher so AppIndicator3 menus use new monitor layout\n'
                'pkill -x xapp-sn-watcher 2>/dev/null || true\n'
                '# Write fakexrandr.bin and cinnamon-monitors.xml to match\n'
                'python3 -m splitrandr --update-configs 2>/dev/null || true'
            )
        else:
            cinnamon_safe = ''

        # Clear fakexrandr config so xrandr sees real physical outputs
        clear_fakexrandr = (
            'rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/fakexrandr.bin"'
        )

        pre_cmds = getattr(self.configuration, '_pre_commands', [])
        data = {
            'pre_commands': '\n'.join(pre_cmds) if pre_cmds else '',
            'clear_fakexrandr': clear_fakexrandr,
            'xrandr': "xrandr " + " ".join(shlex.quote(a) for a in self.configuration.commandlineargs()),
            'delmonitors': '\n'.join(del_lines),
            'setmonitors': '\n'.join(set_lines),
            'cinnamon_safe_setmonitors': cinnamon_safe,
        }
        result = template % data
        # Clean up empty lines from unused template markers
        result = '\n'.join(line for line in result.split('\n') if line.strip() != '') + '\n'
        return result

    def _log_tree(self, name, tree, indent="  "):
        if tree.is_leaf:
            log.info("%s%s: leaf", indent, name)
        else:
            log.info("%s%s: %s split at %.0f%%", indent, name, tree.direction, tree.proportion * 100)
            self._log_tree("left", tree.left, indent + "  ")
            self._log_tree("right", tree.right, indent + "  ")

    def save_to_x(self):
        self.check_configuration()

        log.info("=== save_to_x: starting ===")
        log.info("splits to apply: %s", list(self.configuration.splits.keys()))
        for name, tree in self.configuration.splits.items():
            self._log_tree(name, tree)

        # Disable csd-xrandr and freeze Cinnamon BEFORE any xrandr changes.
        # If we run the main xrandr command first, CSD-xrandr reacts to the
        # RandR event and re-applies the OLD monitors.xml, clobbering our
        # output positions.  Wrapping everything in the guard prevents this.
        from .cinnamon_compat import CinnamonSetMonitorGuard
        with CinnamonSetMonitorGuard():
            # NOTE: Earlier versions rm'd ~/.config/fakexrandr.bin here so
            # xrandr would see real outputs. That created a missing-bin
            # window; if the Guard happened to freeze the wrong cinnamon
            # (during a `cinnamon --replace` handoff, pgrep can return
            # the outgoing PID), the LD_PRELOAD'd new cinnamon would
            # observe "no config" on its first XRRGetMonitors and SEGV
            # in meta_display_logical_index_to_xinerama_index. We now
            # strip LD_PRELOAD inside _xrandr_env() so xrandr never
            # loads the .so and the rm is unnecessary; the bin can stay
            # in place throughout the apply.

            # Apply main configuration (before any setmonitor calls)
            log.info("applying main xrandr config")
            self._run(*self.configuration.commandlineargs())

            # The nvidia driver processes output changes asynchronously.
            # After the xrandr command returns, outputs may still be at
            # their old positions.  Wait briefly and re-apply if needed.
            self._verify_and_correct_positions(max_attempts=3, delay=0.5)

            # Refresh EDIDs from --verbose (always runs unhooked now,
            # so real EDIDs are visible regardless of fakexrandr state).
            self._refresh_edids()

            # Delete ALL existing virtual monitors (anything with ~ in the name)
            try:
                listmon_output = self._output("--listmonitors")
                log.info("current monitors:\n%s", listmon_output.strip())
                for line in listmon_output.strip().split('\n'):
                    line = line.strip()
                    if line.startswith('Monitors:'):
                        continue
                    m = re.match(r'\d+:\s+[+*]*(\S+)', line)
                    if m:
                        mon_name = m.group(1)
                        if '~' in mon_name:
                            log.info("deleting virtual monitor: %s", mon_name)
                            self._run_no_preload_ignore_error("--delmonitor", mon_name)
            except Exception as e:
                log.warning("listmonitors failed: %s", e)

            # Register `xrandr --setmonitor` virtual monitors for each
            # leaf so Cinnamon — which no longer runs with LD_PRELOAD
            # (see end of save_to_x for rationale) — can see the splits
            # via the X server's RandR 1.5 monitor list.  Cinnamon's
            # XRRGetMonitors call returns automatic monitors for each
            # parent output PLUS the setmonitor entries we register
            # here, and Mutter builds one MetaMonitor per entry.
            #
            # Cinnamon must be SIGSTOPped (CinnamonSetMonitorGuard
            # above) for the duration of these calls — Muffin >= 5.4.0
            # segfaults on the inbound RandR notifications when it
            # receives them while live.  The Guard freezes Cinnamon,
            # we register the monitors, the Guard resumes Cinnamon
            # which then processes the queued events as a single batch.
            for output_name, tree in self.configuration.splits.items():
                if tree.is_leaf:
                    continue
                out_cfg = self.configuration.outputs.get(output_name)
                if not out_cfg or not out_cfg.active:
                    continue
                w, h = out_cfg.size
                x, y = out_cfg.position
                state = self.state.outputs.get(output_name)
                w_mm = state.physical_w_mm if state else 0
                h_mm = state.physical_h_mm if state else 0
                border = self.configuration.borders.get(output_name, 0)
                commands = tree.to_setmonitor_commands(
                    output_name, w, h, x, y, w_mm, h_mm, border=border,
                )
                for mon_name, geom, owner in commands:
                    log.info("registering setmonitor: %s %s %s",
                             mon_name, geom, owner)
                    self._run_no_preload_ignore_error(
                        "--setmonitor", mon_name, geom, owner,
                    )

            # Borders on un-split outputs: register a single setmonitor
            # for the inset region so the dead-zone is actually enforced.
            for output_name, border in self.configuration.borders.items():
                if border <= 0:
                    continue
                tree = self.configuration.splits.get(output_name)
                if tree and not tree.is_leaf:
                    continue  # split case handled above (border applies per-leaf)
                out_cfg = self.configuration.outputs.get(output_name)
                if not out_cfg or not out_cfg.active:
                    continue
                w, h = out_cfg.size
                x, y = out_cfg.position
                state = self.state.outputs.get(output_name)
                w_mm = state.physical_w_mm if state else 0
                h_mm = state.physical_h_mm if state else 0
                rx = x + border
                ry = y + border
                rw = max(w - 2 * border, 1)
                rh = max(h - 2 * border, 1)
                rmm_w = max(w_mm - 2 * border * w_mm // w, 1) if w_mm else 0
                rmm_h = max(h_mm - 2 * border * h_mm // h, 1) if h_mm else 0
                geom = "%d/%dx%d/%d+%d+%d" % (rw, rmm_w, rh, rmm_h, rx, ry)
                mon_name = "%s~border" % output_name
                self._run_no_preload_ignore_error(
                    "--setmonitor", mon_name, geom, output_name,
                )

            # Write fakexrandr config and monitors.xml BEFORE Cinnamon
            # resumes, so it reads the new config when it processes the
            # queued RandR events.
            try:
                from .fakexrandr_config import write_fakexrandr_config
                write_fakexrandr_config(
                    self.configuration.splits, self.state, self.configuration,
                    self.configuration.borders
                )
            except Exception as e:
                log.warning("fakexrandr config write failed: %s", e)
            try:
                from .fakexrandr_config import write_cinnamon_monitors_xml
                write_cinnamon_monitors_xml(
                    self.configuration.splits, self.state, self.configuration,
                    self.configuration.borders
                )
            except Exception as e:
                log.warning("monitors.xml write failed: %s", e)

        # Verify result
        try:
            verify = self._output("--listmonitors")
            log.info("monitors after apply:\n%s", verify.strip())
        except Exception:
            pass

        # Force-refresh nudge: re-set X11 primary on the user's chosen
        # parent AFTER Cinnamon resumes.  Setmonitor add/delete events
        # fired while Cinnamon was SIGSTOPped inside the Guard get
        # queued, but Mutter's MetaMonitor refresh sometimes drops
        # them on coalesce; an extra RRPrimaryChangeNotify event after
        # the guard exits forces Cinnamon to re-call XRRGetMonitors,
        # which the .so then services from the freshly-written bin.
        # Without this, "remove splits then rebuild" silently no-ops
        # because the WM keeps serving its cached MetaMonitor list.
        primary = next(
            (n for n, o in self.configuration.outputs.items()
             if o.active and o.primary),
            None,
        )
        if primary:
            try:
                self._run_no_preload("--output", primary, "--primary")
                log.info("nudged Cinnamon refresh via --primary on %s", primary)
            except Exception as e:
                log.warning("failed to nudge primary: %s", e)

        # Mutter's MetaMonitorManager caches the result of the .so's
        # initial XRRGetMonitors call (the synthesized split tree) and
        # uses xcb-randr for subsequent state.  RRMonitorChangeNotify
        # events from --setmonitor don't reliably trigger a refresh:
        # GetCurrentState via DBus returns the cached view even after
        # the bin/setmonitors have changed.  ApplyMonitorsConfig is
        # documented as a crash-trigger on this hardware
        # (feedback_no_apply_monitors_config).  The reliable
        # alternative is `cinnamon --replace`: it spawns a new
        # cinnamon, mutter re-introspects from a clean slate, and the
        # current bin produces the current MetaMonitor list.
        #
        # Gate on bin content changes (hash) so we only pay the
        # restart cost when the user actually applied new splits.
        # Restart on EITHER: .so itself changed (loaded version drifts
        # from on-disk) OR the bin's content hash changed since the
        # last apply.
        try:
            import hashlib
            from .fakexrandr_config import (
                is_cinnamon_fakexrandr_loaded, is_cinnamon_fakexrandr_current,
                _find_fakexrandr_lib, CONFIG_PATH,
                restart_cinnamon_with_fakexrandr, restart_cinnamon_without_fakexrandr,
            )
            has_splits = any(
                not tree.is_leaf
                for tree in self.configuration.splits.values()
            )

            current_bin_hash = None
            try:
                with open(CONFIG_PATH, 'rb') as f:
                    current_bin_hash = hashlib.sha1(f.read()).hexdigest()
            except FileNotFoundError:
                current_bin_hash = ''
            last_hash = getattr(self, '_last_applied_bin_hash', None)
            bin_changed = current_bin_hash != last_hash

            so_stale = has_splits and not is_cinnamon_fakexrandr_current()

            if has_splits and (so_stale or bin_changed):
                lib_path = _find_fakexrandr_lib()
                if lib_path:
                    log.info("restarting Cinnamon to refresh MetaMonitor list "
                             "(so_stale=%s bin_changed=%s)", so_stale, bin_changed)
                    restart_cinnamon_with_fakexrandr(lib_path)
                    from .cinnamon_compat import _wait_cinnamon_on_dbus
                    if not _wait_cinnamon_on_dbus(timeout=15.0):
                        log.warning("Cinnamon did not respond on D-Bus within timeout")
                    log.info("Cinnamon restarted with LD_PRELOAD")
            elif not has_splits and is_cinnamon_fakexrandr_loaded():
                log.info("no splits active, restarting Cinnamon without fakexrandr")
                restart_cinnamon_without_fakexrandr()

            self._last_applied_bin_hash = current_bin_hash
        except Exception as e:
            log.warning("fakexrandr cinnamon restart failed: %s", e)

        # Final verification: after all guards have exited and Cinnamon
        # has resumed, poll for a few seconds to catch Muffin reverting
        # our layout.  Muffin processes queued RandR events asynchronously
        # and may take several seconds to re-apply its monitor config.
        needs_correction = False
        for check_round in range(4):
            time.sleep(1.0)
            final_positions = self._query_output_positions()
            for name, out_cfg in self.configuration.outputs.items():
                if not out_cfg.active:
                    continue
                # Skip split outputs: fakexrandr hides the physical output
                # (e.g. DP-5 becomes DP-5~1/~2/~3), so it won't appear in
                # xrandr --query.  Check non-split outputs only.
                if name in self.configuration.splits:
                    continue
                expected = (out_cfg.position[0], out_cfg.position[1])
                actual = final_positions.get(name)
                if actual is None or actual != expected:
                    log.warning("final check (round %d): %s at %s, expected %s",
                               check_round + 1, name, actual, expected)
                    needs_correction = True
            if needs_correction:
                break
        if needs_correction:
            log.info("positions drifted after Cinnamon resumed, re-applying")
            with CinnamonSetMonitorGuard():
                # No need to rm fakexrandr.bin: _xrandr_env() strips LD_PRELOAD
                # for all xrandr invocations, and rm'ing the bin would expose
                # an unfrozen Cinnamon to a "no config" window during the
                # Guard's PID race.
                self._run(*self.configuration.commandlineargs())
                self._verify_and_correct_positions(max_attempts=3, delay=0.5)

                # Re-create setmonitor VMs
                try:
                    listmon_output = self._output("--listmonitors")
                    for line in listmon_output.strip().split('\n'):
                        line = line.strip()
                        if line.startswith('Monitors:'):
                            continue
                        m_mon = re.match(r'\d+:\s+[+*]*(\S+)', line)
                        if m_mon and '~' in m_mon.group(1):
                            self._run_no_preload_ignore_error("--delmonitor", m_mon.group(1))
                except Exception:
                    pass

                # NOTE: setmonitor creation intentionally elided —
                # see the comment in the initial-apply block above.

                try:
                    from .fakexrandr_config import write_fakexrandr_config
                    write_fakexrandr_config(
                        self.configuration.splits, self.state, self.configuration,
                        self.configuration.borders
                    )
                except Exception:
                    pass
                try:
                    from .fakexrandr_config import write_cinnamon_monitors_xml
                    write_cinnamon_monitors_xml(
                        self.configuration.splits, self.state, self.configuration,
                        self.configuration.borders
                    )
                except Exception:
                    pass
            log.info("final correction applied")

        # Restart xapp-sn-watcher so it picks up the new monitor layout.
        # Its GDK caches monitor geometry and doesn't update on setmonitor
        # changes, causing AppIndicator3 menus to appear on the wrong monitor.
        _restart_sn_watcher()

        # NOTE: an `xrandr --output PARENT --primary` nudge USED to
        # live here (intended to wake csd-xrandr).  Removed
        # 2026-04-29 because, when the primary parent has splits,
        # setting X11 primary on the parent makes Mutter's
        # XRRGetOutputPrimary return an output that has no
        # MetaMonitor counterpart (libXrandr.so.2 replaces parents
        # with PARENT~1/~2/~3 fakes in XRRGetMonitors), Cinnamon JS
        # dereferences an undefined ``layoutManager.primaryMonitor``,
        # and the WM dies in main.js before paint.  Primary is now
        # carried solely by ``primary_connector_name`` in
        # fakexrandr.bin and surfaced via the .so's
        # XRRGetOutputPrimary intercept.
        #
        # We also deliberately do NOT call ApplyMonitorsConfig via
        # dbus here.  Triggering Mutter's MonitorsChanged a second
        # time after the xrandr wave races Cinnamon's JS
        # layoutManager handlers and the JS code SIGSEGVs in
        # meta_display_logical_index_to_xinerama_index().

        # Pin Cinnamon's panel(s) to the primary monitor's xinerama
        # index. The panels-enabled gsetting is xinerama-indexed but
        # the rest of Cinnamon's APIs use logical indexing; on Nvidia
        # tiled hardware the two orderings differ, so a hard-coded
        # gsetting can land the panel on the wrong tile. Done last so
        # that Cinnamon has had a chance to settle its primary state
        # after all the prior xrandr / restart activity.
        try:
            from .cinnamon_compat import pin_panels_to_primary
            pin_panels_to_primary()
        except Exception as e:
            log.warning("pin_panels_to_primary failed: %s", e)

        log.info("=== save_to_x: done ===")

    def save_to_json(self, path):
        data = self.configuration.to_dict()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)
            f.write('\n')

    def load_from_json(self, path):
        with open(path, 'r') as f:
            data = json.load(f)
        # Merge saved config onto current live state
        saved_cfg = self.Configuration.from_dict(data, self)
        for name, saved_out in saved_cfg.outputs.items():
            if name in self.configuration.outputs:
                self.configuration.outputs[name] = saved_out
        self.configuration.splits = saved_cfg.splits
        self.configuration.borders = saved_cfg.borders
        self.configuration._pre_commands = getattr(saved_cfg, '_pre_commands', [])

    def merge_splits_from_cinnamon(self):
        """Reconstruct split trees from Cinnamon's live MetaMonitor list and
        overlay them on the current configuration.

        Use case: GUI startup when layout.json has empty splits but
        Cinnamon (via the LD_PRELOAD'd .so) is currently rendering splits
        from fakexrandr.bin. Without this, the editable widget opens with
        an empty parent-only Proposed view while the Current pane shows
        the live splits — the layout.json/fakexrandr.bin
        out-of-sync state observed 2026-04-29.

        Strategy: for each active parent output in the live X
        configuration, find Cinnamon-reported monitors whose geometry
        falls inside the parent's bounding box, normalise to
        parent-relative coordinates, and feed to
        SplitTree.from_setmonitor_regions.

        Silent no-op if Cinnamon is not on D-Bus or returns no monitor
        list. Existing splits in self.configuration.splits are preserved
        for outputs that already had them.
        """
        from .cinnamon_compat import query_cinnamon_monitors
        from .splits import SplitTree
        monitors = query_cinnamon_monitors()
        if not monitors:
            return
        for parent_name, parent_cfg in self.configuration.outputs.items():
            if not parent_cfg.active:
                continue
            if parent_name in self.configuration.splits:
                continue  # don't clobber an already-loaded tree
            px, py = parent_cfg.position
            pw, ph = parent_cfg.size
            regions = []
            primary_region_local = None
            for m in monitors:
                mx, my = m['x'], m['y']
                mw, mh = m['width'], m['height']
                # Inclusion test: monitor fully contained in parent's box
                if (mx >= px and my >= py
                        and mx + mw <= px + pw
                        and my + mh <= py + ph):
                    region = (mx - px, my - py, mw, mh)
                    regions.append(region)
                    if m.get('primary'):
                        primary_region_local = region
            # Need at least 2 regions and they must be a non-trivial split
            # of the parent (a single full-cover region is "no split").
            if len(regions) < 2:
                continue
            tree = SplitTree.from_setmonitor_regions(
                regions, parent_name, pw, ph,
            )
            if tree.is_leaf:
                continue
            # Carry over the primary marker from Cinnamon's view: find the
            # leaf whose region matches the cinnamon-flagged primary
            # monitor. SplitTree.from_setmonitor_regions doesn't preserve
            # input ordering, so we match by geometry rather than by
            # index.
            if primary_region_local is not None:
                # leaf_regions yields (x, y, w, h, w_mm, h_mm) per leaf in
                # tree-traversal order. Match by geometry — splits.py's
                # from_setmonitor_regions doesn't preserve input ordering.
                target_idx = None
                for i, leaf in enumerate(tree.leaf_regions(pw, ph)):
                    if (leaf[0], leaf[1], leaf[2], leaf[3]) == primary_region_local:
                        target_idx = i
                        break
                if target_idx is not None:
                    tree.set_primary_at(target_idx)
            self.configuration.splits[parent_name] = tree

    def merge_splits_from_json(self, path):
        """Overlay splits + borders + leaf-primary from a saved layout.json
        without disturbing the current X-derived output positions/sizes/modes.

        Use case: GUI startup after load_from_x. Splits are no longer
        reconstructible from X state on this hardware (the .so synthesises
        fakes only inside LD_PRELOAD'd processes; splitrandr's own xrandr
        runs unhooked and sees only parent outputs). The canonical record
        of the user's split layout is `~/.config/splitrandr/layout.json`,
        which save_to_x keeps in sync. Reading that file and copying its
        splits/borders into the live configuration restores the editable
        widget's "Proposed" view to the user's intended layout.

        Silent no-op if the file is missing, unreadable, or describes
        outputs that don't exist in the current X state.
        """
        if not os.path.exists(path):
            return
        try:
            with open(path, 'r') as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log.info("merge_splits_from_json: failed to read %s: %s", path, e)
            return
        try:
            saved_cfg = self.Configuration.from_dict(data, self)
        except Exception as e:
            log.info("merge_splits_from_json: failed to parse %s: %s", path, e)
            return
        # Only adopt splits / borders for outputs that currently exist
        # AND don't already have a tree. Skip-if-present makes this
        # callable as a fallback after merge_splits_from_cinnamon
        # without clobbering Cinnamon's live truth.
        for name, tree in saved_cfg.splits.items():
            if (name in self.configuration.outputs
                    and name not in self.configuration.splits):
                self.configuration.splits[name] = tree
        for name, b in saved_cfg.borders.items():
            if (name in self.configuration.outputs
                    and name not in self.configuration.borders):
                self.configuration.borders[name] = b
        # Carry over per-output primary flag if the saved config marks one
        # explicitly and the current X state has no primary marked. This
        # mirrors load_from_x's _pending_primary handling for the same
        # Nvidia-tile case where xrandr --query reports no primary at all.
        any_x_primary = any(
            getattr(out, 'primary', False)
            for out in self.configuration.outputs.values()
        )
        if not any_x_primary:
            for name, saved_out in saved_cfg.outputs.items():
                if (name in self.configuration.outputs
                        and getattr(saved_out, 'primary', False)
                        and self.configuration.outputs[name].active):
                    self.configuration.outputs[name].primary = True

    def check_configuration(self):
        vmax = self.state.virtual.max

        for output_name in self.outputs:
            output_config = self.configuration.outputs[output_name]

            if not output_config.active:
                continue

            x = output_config.position[0] + output_config.size[0]
            y = output_config.position[1] + output_config.size[1]

            if x > vmax[0] or y > vmax[1]:
                raise InadequateConfiguration(
                    _("A part of an output is outside the virtual screen."))

            if output_config.position[0] < 0 or output_config.position[1] < 0:
                raise InadequateConfiguration(
                    _("An output is outside the virtual screen."))
