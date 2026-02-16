# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Wrapper around command line xrandr with --setmonitor support"""

import json
import os
import re
import subprocess
import time
import warnings
import logging
from functools import reduce

log = logging.getLogger('splitrandr')

from .auxiliary import (
    Size, Position, Geometry,
    InadequateConfiguration, Rotation, ROTATIONS, NORMAL, NamedSize,
)
from .splits import SplitTree
from .i18n import _

def _restart_sn_watcher():
    """Restart xapp-sn-watcher so it picks up the new monitor layout.

    The sn-watcher's GDK caches monitor geometry at startup. When
    setmonitor VMs change the layout, the cached model goes stale,
    causing AppIndicator3 menus to pop up on the wrong monitor.
    Killing the watcher lets D-Bus auto-restart it with fresh state.
    """
    try:
        result = subprocess.run(
            ['pkill', '-x', 'xapp-sn-watcher'],
            capture_output=True, timeout=5
        )
        if result.returncode == 0:
            log.info("restarted xapp-sn-watcher for monitor layout update")
    except Exception:
        pass


class Feature:
    PRIMARY = 1


class XRandR:
    configuration = None
    state = None

    def __init__(self, display=None, force_version=False):
        self.environ = dict(os.environ)
        if display:
            self.environ['DISPLAY'] = display

        version_output = self._output("--version")
        supported_versions = ["1.2", "1.3", "1.4", "1.5"]
        if not any(x in version_output for x in supported_versions) and not force_version:
            raise Exception("XRandR %s required." %
                            "/".join(supported_versions))

        self.features = set()
        if " 1.2" not in version_output:
            self.features.add(Feature.PRIMARY)

    def _get_outputs(self):
        assert self.state.outputs.keys() == self.configuration.outputs.keys()
        return self.state.outputs.keys()
    outputs = property(_get_outputs)

    #################### calling xrandr ####################

    def _output(self, *args):
        log.info("xrandr %s", " ".join(args))
        proc = subprocess.Popen(
            ("xrandr",) + args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.environ
        )
        ret, err = proc.communicate()
        status = proc.wait()
        if status != 0:
            log.error("xrandr exit %d stderr: %s", status, err.decode('utf-8', errors='replace'))
            raise Exception("XRandR returned error code %d: %s" %
                            (status, err))
        if err:
            log.warning("xrandr stderr (no error): %s", err.decode('utf-8', errors='replace'))
            warnings.warn(
                "XRandR wrote to stderr, but did not report an error (Message was: %r)" % err)
        return ret.decode('utf-8')

    def _run(self, *args):
        self._output(*args)

    def _run_ignore_error(self, *args):
        """Run xrandr, ignoring errors (used for --delmonitor which may fail)."""
        log.info("xrandr (ignore-error) %s", " ".join(args))
        proc = subprocess.Popen(
            ("xrandr",) + args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.environ
        )
        out, err = proc.communicate()
        status = proc.wait()
        if status != 0:
            log.warning("xrandr (ignored) exit %d stderr: %s", status, err.decode('utf-8', errors='replace'))

    def _run_no_preload(self, *args):
        """Run xrandr with LD_PRELOAD stripped (for setmonitor commands)."""
        env = {k: v for k, v in self.environ.items() if k != 'LD_PRELOAD'}
        log.info("xrandr (no-preload) %s", " ".join(args))
        proc = subprocess.Popen(
            ("xrandr",) + args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        )
        out, err = proc.communicate()
        status = proc.wait()
        if status != 0:
            log.error("xrandr (no-preload) exit %d stderr: %s", status, err.decode('utf-8', errors='replace'))
            raise Exception("XRandR returned error code %d: %s" % (status, err))

    def _refresh_edids(self):
        """Re-read EDIDs from the X server and update state.

        Must be called when fakexrandr.bin is cleared (pass-through mode)
        so that real physical outputs are visible with their EDIDs.
        Virtual outputs created by fakexrandr have no EDID, so if we
        loaded state while fakexrandr was active, EDIDs will be empty.
        """
        try:
            verbose = self._output("--verbose")
        except Exception:
            return
        current_output = None
        in_edid = False
        edid_lines = []
        for line in verbose.split('\n'):
            if not line.startswith(('\t', ' ')):
                # Flush pending EDID
                if in_edid and current_output and edid_lines:
                    edid_hex = ''.join(edid_lines)
                    out_state = self.state.outputs.get(current_output)
                    if out_state and not out_state.edid_hex:
                        out_state.edid_hex = edid_hex
                in_edid = False
                edid_lines = []
                # Parse output name from headline
                parts = line.split()
                if len(parts) >= 2 and parts[1] in ('connected', 'disconnected', 'unknown'):
                    current_output = parts[0]
                else:
                    current_output = None
            elif line.startswith('\t'):
                stripped = line.strip()
                if stripped == 'EDID:':
                    in_edid = True
                    edid_lines = []
                elif in_edid:
                    if re.match(r'^[0-9a-f]+$', stripped):
                        edid_lines.append(stripped)
                    else:
                        if current_output and edid_lines:
                            edid_hex = ''.join(edid_lines)
                            out_state = self.state.outputs.get(current_output)
                            if out_state and not out_state.edid_hex:
                                out_state.edid_hex = edid_hex
                        in_edid = False
                        edid_lines = []
        # Flush final EDID
        if in_edid and current_output and edid_lines:
            edid_hex = ''.join(edid_lines)
            out_state = self.state.outputs.get(current_output)
            if out_state and not out_state.edid_hex:
                out_state.edid_hex = edid_hex

    def _run_no_preload_ignore_error(self, *args):
        """Run xrandr with LD_PRELOAD stripped, ignoring errors."""
        env = {k: v for k, v in self.environ.items() if k != 'LD_PRELOAD'}
        log.info("xrandr (no-preload, ignore-error) %s", " ".join(args))
        proc = subprocess.Popen(
            ("xrandr",) + args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        )
        out, err = proc.communicate()
        status = proc.wait()
        if status != 0:
            log.warning("xrandr (no-preload, ignored) exit %d stderr: %s", status, err.decode('utf-8', errors='replace'))

    #################### loading ####################

    def load_from_x(self):
        # Preserve borders across reloads — borders are set by the user
        # and not discoverable from the X server state alone.
        old_borders = getattr(self, 'configuration', None)
        old_borders = old_borders.borders if old_borders else {}
        self.configuration = self.Configuration(self)
        self.configuration.borders = dict(old_borders)
        self.state = self.State()

        screenline, items = self._load_raw_lines()

        self._load_parse_screenline(screenline)

        for item in items:
            headline = item[0]
            details = item[1]
            edid_hex = item[2] if len(item) > 2 else ""
            if headline.startswith("  "):
                continue
            if headline == "":
                continue

            headline = headline.replace(
                'unknown connection', 'unknown-connection')
            hsplit = headline.split(" ")
            output = self.state.Output(hsplit[0])
            assert hsplit[1] in (
                "connected", "disconnected", 'unknown-connection')

            output.connected = (hsplit[1] in ('connected', 'unknown-connection'))
            output.edid_hex = edid_hex

            # Parse physical dimensions (e.g. "1210mm x 680mm")
            mm_match = re.search(r'(\d+)mm\s+x\s+(\d+)mm', headline)
            if mm_match:
                output.physical_w_mm = int(mm_match.group(1))
                output.physical_h_mm = int(mm_match.group(2))

            primary = False
            if 'primary' in hsplit:
                if Feature.PRIMARY in self.features:
                    primary = True
                hsplit.remove('primary')

            if not hsplit[2].startswith("("):
                active = True

                geometry = Geometry(hsplit[2])

                if hsplit[4] in ROTATIONS:
                    current_rotation = Rotation(hsplit[4])
                else:
                    current_rotation = NORMAL
            else:
                active = False
                geometry = None
                current_rotation = None

            output.rotations = set()
            for rotation in ROTATIONS:
                if rotation in headline:
                    output.rotations.add(rotation)

            currentname = None
            current_rate = None
            for tokens, w, h, refresh_rate in details:
                name, _mode_raw = tokens[0:2]
                mode_id = _mode_raw.strip("()")
                try:
                    size = Size([int(w), int(h)])
                except ValueError:
                    raise Exception(
                        "Output %s parse error: modename %s modeid %s." % (output.name, name, mode_id)
                    )
                if "*current" in tokens:
                    currentname = name
                    current_rate = refresh_rate
                if "+preferred" in tokens and output.preferred_resolution is None:
                    output.preferred_resolution = (int(w), int(h))
                for x in ["+preferred", "*current"]:
                    if x in tokens:
                        tokens.remove(x)

                for old_mode in output.modes:
                    if old_mode.name == name and old_mode.refresh_rate == refresh_rate:
                        if tuple(old_mode) != tuple(size):
                            warnings.warn((
                                "Supressing duplicate mode %s even "
                                "though it has different resolutions (%s, %s)."
                            ) % (name, size, old_mode))
                        break
                else:
                    output.modes.append(NamedSize(size, name=name, refresh_rate=refresh_rate))

            self.state.outputs[output.name] = output
            self.configuration.outputs[output.name] = self.configuration.OutputConfiguration(
                active, primary, geometry, current_rotation, currentname, current_rate
            )

        # Load existing virtual monitors from X and merge virtual outputs
        # (DP-5~0, ~1, ~2) back into their physical parent (DP-5).
        self._load_monitors()

    def _load_monitors(self):
        """Reconstruct splits and merge virtual outputs into physical outputs.

        When fakexrandr is active, xrandr --verbose only reports virtual
        outputs (DP-5~1, ~2, ~3) — the physical DP-5 disappears.  We use
        --listmonitors for the physical geometry and the already-loaded
        virtual outputs (from --verbose) for the split regions.
        """
        # ── 1. Parse --listmonitors for physical and virtual geometry ──
        physical_geom = {}  # name -> (w, h, x, y, w_mm, h_mm)
        vm_regions = {}     # base_name -> [(x, y, w, h), ...]
        try:
            listmon = self._output("--listmonitors")
            for line in listmon.strip().split('\n'):
                line = line.strip()
                if line.startswith('Monitors:'):
                    continue
                m = re.match(
                    r'\d+:\s+[+*]*(\S+)\s+(\d+)/(\d+)x(\d+)/(\d+)\+(\d+)\+(\d+)',
                    line
                )
                if not m:
                    continue
                mon_name = m.group(1)
                w, w_mm, h, h_mm, x, y = [int(m.group(i)) for i in range(2, 8)]
                if '~' not in mon_name:
                    physical_geom[mon_name] = (w, h, x, y, w_mm, h_mm)
                else:
                    base = mon_name.rsplit('~', 1)[0]
                    try:
                        int(mon_name.rsplit('~', 1)[1])
                    except ValueError:
                        continue
                    if base not in vm_regions:
                        vm_regions[base] = []
                    vm_regions[base].append((x, y, w, h))
        except Exception:
            pass

        # ── 2. Group virtual outputs by base name ────────────────────
        # Use the outputs already loaded from --verbose, which has ALL
        # virtual outputs (--listmonitors can be missing some).
        virt_groups = {}  # base_name -> [virt_name, ...]
        for name in list(self.configuration.outputs.keys()):
            if '~' not in name:
                continue
            base = name.rsplit('~', 1)[0]
            try:
                int(name.rsplit('~', 1)[1])
            except ValueError:
                continue
            if base not in virt_groups:
                virt_groups[base] = []
            virt_groups[base].append(name)

        # ── 3. For each group, reconstruct splits ────────────────────
        for base_name, virt_names in virt_groups.items():

            # Case A: physical output already exists (fakexrandr not
            # intercepting xrandr, so DP-5 shows up alongside DP-5~N).
            if base_name in self.configuration.outputs:
                output_cfg = self.configuration.outputs[base_name]
                if not output_cfg.active:
                    continue
                ox, oy = output_cfg.position
                total_w, total_h = output_cfg.size[0], output_cfg.size[1]
                regions = []
                for vn in virt_names:
                    vc = self.configuration.outputs[vn]
                    if vc.active:
                        regions.append((vc.position[0], vc.position[1],
                                        vc.size[0], vc.size[1]))
                normalized = [(r[0] - ox, r[1] - oy, r[2], r[3])
                              for r in regions]
                tree = SplitTree.from_setmonitor_regions(
                    normalized, base_name, total_w, total_h)
                if not tree.is_leaf:
                    self.configuration.splits[base_name] = tree
                # Remove virtual outputs — splits are in the overlay
                for vn in virt_names:
                    self.configuration.outputs.pop(vn, None)
                    self.state.outputs.pop(vn, None)
                continue

            # Case B: physical output missing (fakexrandr active).
            # Collect regions from the virtual outputs' geometry.
            regions = []
            first_virt = virt_names[0]
            for vn in virt_names:
                vc = self.configuration.outputs.get(vn)
                if vc and vc.active:
                    regions.append((vc.position[0], vc.position[1],
                                    vc.size[0], vc.size[1]))
            if not regions:
                continue

            # Physical geometry from --listmonitors, or bounding box
            if base_name in physical_geom:
                pw, ph, px, py, pw_mm, ph_mm = physical_geom[base_name]
            else:
                px = min(r[0] for r in regions)
                py = min(r[1] for r in regions)
                pw = max(r[0] + r[2] for r in regions) - px
                ph = max(r[1] + r[3] for r in regions) - py
                pw_mm = ph_mm = 0

            # Find the physical mode from the virtual output's mode list
            virt_state = self.state.outputs[first_virt]
            virt_cfg = self.configuration.outputs[first_virt]
            phys_mode = None
            for mode in virt_state.modes:
                if mode.width == pw and mode.height == ph:
                    phys_mode = mode
                    break
            if phys_mode is None:
                mode_name = "%dx%d" % (pw, ph)
                phys_mode = NamedSize(Size([pw, ph]), name=mode_name,
                                      refresh_rate=60.0)

            # Build physical output state
            phys_state = self.State.Output(base_name)
            phys_state.connected = True
            phys_state.modes = list(virt_state.modes)
            # Ensure the physical resolution is available as a mode
            if not any(m.name == phys_mode.name for m in phys_state.modes):
                phys_state.modes.append(phys_mode)
            phys_state.rotations = virt_state.rotations
            phys_state.edid_hex = virt_state.edid_hex
            phys_state.physical_w_mm = pw_mm
            phys_state.physical_h_mm = ph_mm
            phys_state.preferred_resolution = virt_state.preferred_resolution or (pw, ph)

            # Build physical output configuration
            phys_rotation = virt_cfg.rotation if virt_cfg.rotation else NORMAL
            any_primary = any(
                self.configuration.outputs[vn].primary
                for vn in virt_names
                if self.configuration.outputs.get(vn)
                and self.configuration.outputs[vn].active
            )
            phys_cfg_obj = self.Configuration.OutputConfiguration(
                active=True, primary=any_primary,
                geometry=Geometry(pw, ph, px, py),
                rotation=phys_rotation,
                modename=phys_mode.name,
                refresh_rate=phys_mode.refresh_rate,
            )

            # Insert physical, remove virtual outputs
            self.state.outputs[base_name] = phys_state
            self.configuration.outputs[base_name] = phys_cfg_obj
            for vn in virt_names:
                self.configuration.outputs.pop(vn, None)
                self.state.outputs.pop(vn, None)

            # Reconstruct split tree from virtual output regions
            normalized = [(r[0] - px, r[1] - py, r[2], r[3])
                          for r in regions]
            tree = SplitTree.from_setmonitor_regions(
                normalized, base_name, pw, ph)
            if not tree.is_leaf:
                self.configuration.splits[base_name] = tree

        # ── 4. Case C: setmonitor virtual monitors (no fakexrandr) ──
        # If --listmonitors shows ~-named virtual monitors but they
        # weren't in --verbose outputs (fakexrandr not active),
        # reconstruct splits from the listmonitors regions.
        for base_name, regions in vm_regions.items():
            if base_name in virt_groups:
                continue  # already handled in step 3
            if base_name not in self.configuration.outputs:
                continue
            if base_name in self.configuration.splits:
                continue  # already has splits
            output_cfg = self.configuration.outputs[base_name]
            if not output_cfg.active:
                continue
            ox, oy = output_cfg.position
            total_w, total_h = output_cfg.size[0], output_cfg.size[1]
            normalized = [(r[0] - ox, r[1] - oy, r[2], r[3])
                          for r in regions]
            tree = SplitTree.from_setmonitor_regions(
                normalized, base_name, total_w, total_h)
            if not tree.is_leaf:
                self.configuration.splits[base_name] = tree

    def _load_raw_lines(self):
        output = self._output("--verbose")
        items = []
        screenline = None
        in_edid = False
        edid_lines = []
        current_edid_item = None
        for line in output.split('\n'):
            if line.startswith("Screen "):
                assert screenline is None
                screenline = line
            elif line.startswith('\t'):
                # Check for EDID property start
                stripped = line.strip()
                if stripped == 'EDID:':
                    in_edid = True
                    edid_lines = []
                    current_edid_item = items[-1] if items else None
                    continue
                if in_edid:
                    if re.match(r'^[0-9a-f]+$', stripped):
                        edid_lines.append(stripped)
                        continue
                    else:
                        # EDID block ended, store it
                        if current_edid_item is not None and edid_lines:
                            edid_hex = ''.join(edid_lines)
                            if len(current_edid_item) < 3:
                                current_edid_item.append(edid_hex)
                            else:
                                current_edid_item[2] = edid_hex
                        in_edid = False
                        edid_lines = []
                        current_edid_item = None
                continue
            elif line.startswith(2 * ' '):
                line = line.strip()
                if reduce(bool.__or__, [line.startswith(x + ':') for x in "hv"]):
                    is_vline = line.startswith('v:')
                    refresh_rate = None
                    if is_vline:
                        rate_match = re.search(r'clock\s+([\d.]+)\s*Hz', line)
                        if rate_match:
                            refresh_rate = float(rate_match.group(1))
                    line = line[-len(line):line.index(" start") - len(line)]
                    items[-1][1][-1].append(line[line.rindex(' '):])
                    if is_vline:
                        items[-1][1][-1].append(refresh_rate)
                else:
                    items[-1][1].append([line.split()])
            else:
                # Flush any pending EDID before starting new output
                if in_edid and current_edid_item is not None and edid_lines:
                    edid_hex = ''.join(edid_lines)
                    if len(current_edid_item) < 3:
                        current_edid_item.append(edid_hex)
                    else:
                        current_edid_item[2] = edid_hex
                    in_edid = False
                    edid_lines = []
                    current_edid_item = None
                items.append([line, []])
        # Flush any remaining EDID at end of output
        if in_edid and current_edid_item is not None and edid_lines:
            edid_hex = ''.join(edid_lines)
            if len(current_edid_item) < 3:
                current_edid_item.append(edid_hex)
            else:
                current_edid_item[2] = edid_hex
        return screenline, items

    def _load_parse_screenline(self, screenline):
        assert screenline is not None
        ssplit = screenline.split(" ")

        ssplit_expect = ["Screen", None, "minimum", None, "x", None,
                         "current", None, "x", None, "maximum", None, "x", None]
        assert all(a == b for (a, b) in zip(
            ssplit, ssplit_expect) if b is not None)

        self.state.virtual = self.state.Virtual(
            min_mode=Size((int(ssplit[3]), int(ssplit[5][:-1]))),
            max_mode=Size((int(ssplit[11]), int(ssplit[13])))
        )
        self.configuration.virtual = Size(
            (int(ssplit[7]), int(ssplit[9][:-1]))
        )

    #################### saving ####################

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
                del_lines.append("env -u LD_PRELOAD xrandr --delmonitor %s 2>/dev/null || true" % mon_name)
                set_lines.append("env -u LD_PRELOAD xrandr --setmonitor %s %s %s" % (mon_name, geom, out))

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
            del_lines.append("env -u LD_PRELOAD xrandr --delmonitor %s 2>/dev/null || true" % mon_name)
            set_lines.append("env -u LD_PRELOAD xrandr --setmonitor %s %s %s" % (mon_name, geom, output_name))

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
            'xrandr': "xrandr " + " ".join(self.configuration.commandlineargs()),
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

    def _query_output_positions(self):
        """Query xrandr for current output positions. Returns {name: (x, y)} for active outputs."""
        positions = {}
        try:
            output = self._output("--query")
            for line in output.split('\n'):
                if line.startswith(('\t', ' ', 'Screen')):
                    continue
                parts = line.split()
                if len(parts) < 3 or parts[1] not in ('connected', 'disconnected', 'unknown-connection'):
                    continue
                name = parts[0]
                # Find geometry (WxH+X+Y)
                for p in parts[2:]:
                    m = re.match(r'\d+x\d+\+(\d+)\+(\d+)', p)
                    if m:
                        positions[name] = (int(m.group(1)), int(m.group(2)))
                        break
        except Exception as e:
            log.warning("failed to query output positions: %s", e)
        return positions

    def _verify_and_correct_positions(self, max_attempts=3, delay=0.5):
        """Verify output positions match configuration, re-apply if not.

        The nvidia driver processes mode changes asynchronously.  Even after
        xrandr returns success, outputs may not yet be at their requested
        positions.  This method waits and re-applies until positions match.
        """
        for attempt in range(max_attempts):
            time.sleep(delay)
            current = self._query_output_positions()
            mismatched = []
            for name, out_cfg in self.configuration.outputs.items():
                if not out_cfg.active:
                    continue
                expected = (out_cfg.position[0], out_cfg.position[1])
                actual = current.get(name)
                if actual is None:
                    continue
                if actual != expected:
                    mismatched.append((name, expected, actual))
            if not mismatched:
                log.info("output positions verified correct (attempt %d)", attempt + 1)
                return
            for name, expected, actual in mismatched:
                log.warning("position mismatch for %s: expected %s, got %s (attempt %d)",
                           name, expected, actual, attempt + 1)
            log.info("re-applying xrandr config to correct positions")
            self._run(*self.configuration.commandlineargs())
        # Final check
        current = self._query_output_positions()
        for name, out_cfg in self.configuration.outputs.items():
            if not out_cfg.active:
                continue
            expected = (out_cfg.position[0], out_cfg.position[1])
            actual = current.get(name)
            if actual and actual != expected:
                log.error("position still wrong for %s after %d attempts: expected %s, got %s",
                         name, max_attempts, expected, actual)

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
            # Clear fakexrandr config so the real physical outputs become
            # visible to xrandr.  When fakexrandr is active, it replaces
            # DP-5 with DP-5~1/~2/~3 — clearing the config makes it pass
            # through, restoring the real DP-5 for the commands below.
            try:
                from .fakexrandr_config import CONFIG_PATH
                if os.path.exists(CONFIG_PATH):
                    os.remove(CONFIG_PATH)
                    log.info("cleared fakexrandr config to expose real outputs")
            except Exception as e:
                log.warning("failed to clear fakexrandr config: %s", e)

            # Apply main configuration (before any setmonitor calls)
            log.info("applying main xrandr config")
            self._run(*self.configuration.commandlineargs())

            # The nvidia driver processes output changes asynchronously.
            # After the xrandr command returns, outputs may still be at
            # their old positions.  Wait briefly and re-apply if needed.
            self._verify_and_correct_positions(max_attempts=3, delay=0.5)

            # Refresh EDIDs while fakexrandr is in pass-through mode.
            # When loaded with fakexrandr active, virtual outputs have no EDID.
            # Now that fakexrandr.bin is cleared, real outputs are visible.
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

            # Create new virtual monitors
            for output_name, tree in self.configuration.splits.items():
                output_cfg = self.configuration.outputs.get(output_name)
                if not output_cfg or not output_cfg.active:
                    log.info("skipping splits for %s (not active)", output_name)
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
                log.info("creating %d virtual monitors for %s", len(commands), output_name)
                for mon_name, geom, out in commands:
                    log.info("  setmonitor %s %s %s", mon_name, geom, out)
                    self._run_no_preload("--setmonitor", mon_name, geom, out)

            # Create setmonitor for unsplit outputs that have a border
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
                log.info("creating border setmonitor for unsplit %s: %s %s", output_name, mon_name, geom)
                self._run_no_preload("--setmonitor", mon_name, geom, output_name)

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

        # If Cinnamon doesn't have fakexrandr loaded yet, restart it
        # and then re-apply display config (Muffin applies its own
        # monitors.xml on startup which clobbers our layout).
        try:
            from .fakexrandr_config import (
                is_cinnamon_fakexrandr_loaded, is_cinnamon_fakexrandr_current,
                _find_fakexrandr_lib,
                restart_cinnamon_with_fakexrandr, restart_cinnamon_without_fakexrandr,
                write_cinnamon_monitors_xml,
            )
            has_splits = any(
                not tree.is_leaf
                for tree in self.configuration.splits.values()
            )

            if has_splits and not is_cinnamon_fakexrandr_current():
                lib_path = _find_fakexrandr_lib()
                if lib_path:
                    log.info("Cinnamon doesn't have fakexrandr loaded, restarting")
                    restart_cinnamon_with_fakexrandr(lib_path)

                    # Wait for Cinnamon to be ready on D-Bus
                    log.info("waiting for Cinnamon to settle")
                    from .cinnamon_compat import _wait_cinnamon_on_dbus
                    if not _wait_cinnamon_on_dbus(timeout=15.0):
                        log.warning("Cinnamon did not respond on D-Bus within timeout, proceeding anyway")

                    with CinnamonSetMonitorGuard():
                        # Clear fakexrandr config so xrandr sees real outputs
                        try:
                            if os.path.exists(CONFIG_PATH):
                                os.remove(CONFIG_PATH)
                        except Exception:
                            pass

                        log.info("re-applying xrandr config after Cinnamon restart")
                        self._run(*self.configuration.commandlineargs())
                        self._verify_and_correct_positions(max_attempts=3, delay=0.5)

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
                                self._run_no_preload("--setmonitor", mon_name, geom, out)

                        # Unsplit outputs with border
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
                            self._run_no_preload("--setmonitor", mon_name, geom, output_name)

                        from .fakexrandr_config import write_fakexrandr_config
                        write_fakexrandr_config(
                            self.configuration.splits, self.state, self.configuration,
                            self.configuration.borders
                        )
                        write_cinnamon_monitors_xml(
                            self.configuration.splits, self.state, self.configuration,
                            self.configuration.borders
                        )

                    log.info("re-applied config after Cinnamon restart")

            elif not has_splits and is_cinnamon_fakexrandr_loaded():
                log.info("no splits active, restarting Cinnamon without fakexrandr")
                restart_cinnamon_without_fakexrandr()
        except Exception as e:
            log.warning("fakexrandr integration failed: %s", e)

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
                try:
                    from .fakexrandr_config import CONFIG_PATH
                    if os.path.exists(CONFIG_PATH):
                        os.remove(CONFIG_PATH)
                except Exception:
                    pass
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
                        self._run_no_preload("--setmonitor", mon_name, geom, out)

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
                    self._run_no_preload("--setmonitor", mon_name, geom, output_name)

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

    #################### sub objects ####################

    class State:
        """Represents everything that can not be set by xrandr."""

        virtual = None

        def __init__(self):
            self.outputs = {}

        def __repr__(self):
            return '<%s for %d Outputs, %d connected>' % (
                type(self).__name__, len(self.outputs),
                len([x for x in self.outputs.values() if x.connected])
            )

        class Virtual:
            def __init__(self, min_mode, max_mode):
                self.min = min_mode
                self.max = max_mode

        class Output:
            rotations = None
            connected = None
            physical_w_mm = 0
            physical_h_mm = 0
            edid_hex = ""

            def __init__(self, name):
                self.name = name
                self.modes = []
                self.preferred_resolution = None  # (w, h) tuple

            def __repr__(self):
                return '<%s %r (%d modes)>' % (type(self).__name__, self.name, len(self.modes))

            def modes_by_resolution(self):
                """Return {(w,h): [NamedSize, ...]} grouped by resolution, sorted by rate desc."""
                grouped = {}
                for mode in self.modes:
                    key = (mode.width, mode.height)
                    if key not in grouped:
                        grouped[key] = []
                    grouped[key].append(mode)
                for key in grouped:
                    grouped[key].sort(
                        key=lambda m: m.refresh_rate if m.refresh_rate is not None else 0,
                        reverse=True
                    )
                return grouped

    class Configuration:
        """Represents everything that can be set by xrandr."""

        virtual = None

        def __init__(self, xrandr):
            self.outputs = {}
            self.splits = {}
            self.borders = {}
            self._xrandr = xrandr

        def __repr__(self):
            return '<%s for %d Outputs, %d active>' % (
                type(self).__name__, len(self.outputs),
                len([x for x in self.outputs.values() if x.active])
            )

        def commandlineargs(self):
            args = []
            # Pre-set the framebuffer size so the nvidia driver doesn't
            # misplace outputs when resizing the screen.  Without this,
            # nvidia processes outputs sequentially and may temporarily
            # shrink the screen, making later output positions invalid.
            fb_w = 0
            fb_h = 0
            for output in self.outputs.values():
                if output.active:
                    fb_w = max(fb_w, output.position[0] + output.size[0])
                    fb_h = max(fb_h, output.position[1] + output.size[1])
            if fb_w > 0 and fb_h > 0:
                args.extend(["--fb", "%dx%d" % (fb_w, fb_h)])
            for output_name, output in self.outputs.items():
                args.append("--output")
                args.append(output_name)
                if not output.active:
                    args.append("--off")
                else:
                    if Feature.PRIMARY in self._xrandr.features:
                        if output.primary:
                            args.append("--primary")
                    args.append("--mode")
                    args.append(str(output.mode.name))
                    if output.mode.refresh_rate is not None:
                        args.append("--rate")
                        args.append("%.2f" % output.mode.refresh_rate)
                    args.append("--pos")
                    args.append(str(output.position))
                    args.append("--rotate")
                    args.append(output.rotation)
            return args

        def to_dict(self):
            outputs = {}
            for name, out in self.outputs.items():
                d = {'active': out.active, 'primary': out.primary}
                if out.active:
                    d['mode'] = out.mode.name
                    d['refresh_rate'] = out.mode.refresh_rate
                    d['position'] = list(out.position)
                    d['rotation'] = str(out.rotation)
                outputs[name] = d
            splits = {}
            for name, tree in self.splits.items():
                splits[name] = tree.to_dict()
            return {
                'outputs': outputs,
                'splits': splits,
                'borders': dict(self.borders),
                'pre_commands': getattr(self, '_pre_commands', []),
            }

        @classmethod
        def from_dict(cls, data, xrandr):
            cfg = cls(xrandr)
            for name, out_data in data.get('outputs', {}).items():
                active = out_data['active']
                primary = out_data.get('primary', False)
                if active:
                    mode_name = out_data['mode']
                    refresh_rate = out_data.get('refresh_rate')
                    pos = out_data['position']
                    rotation = Rotation(out_data.get('rotation', 'normal'))
                    # Find mode from state if available
                    mode = None
                    state_out = xrandr.state.outputs.get(name) if xrandr.state else None
                    if state_out:
                        for m in state_out.modes:
                            if m.name == mode_name:
                                if refresh_rate is not None and m.refresh_rate is not None:
                                    if abs(m.refresh_rate - refresh_rate) < 0.1:
                                        mode = m
                                        break
                                elif mode is None:
                                    mode = m
                    if mode is None:
                        # Parse resolution from mode name (e.g. "3840x2160")
                        parts = mode_name.split('x')
                        if len(parts) == 2:
                            try:
                                w, h = int(parts[0]), int(parts[1])
                                mode = NamedSize(Size([w, h]), name=mode_name,
                                                 refresh_rate=refresh_rate)
                            except ValueError:
                                mode = NamedSize(Size([1920, 1080]), name=mode_name,
                                                 refresh_rate=refresh_rate)
                        else:
                            mode = NamedSize(Size([1920, 1080]), name=mode_name,
                                             refresh_rate=refresh_rate)
                    geometry = Geometry(mode[0], mode[1], pos[0], pos[1])
                    if rotation.is_odd:
                        geometry = Geometry(mode[1], mode[0], pos[0], pos[1])
                    oc = cls.OutputConfiguration(
                        active=True, primary=primary,
                        geometry=geometry, rotation=rotation,
                        modename=mode_name, refresh_rate=refresh_rate,
                    )
                else:
                    oc = cls.OutputConfiguration(
                        active=False, primary=False,
                        geometry=None, rotation=None,
                        modename=None, refresh_rate=None,
                    )
                cfg.outputs[name] = oc
            for name, tree_data in data.get('splits', {}).items():
                cfg.splits[name] = SplitTree.from_dict(tree_data)
            cfg.borders = data.get('borders', {})
            cfg._pre_commands = data.get('pre_commands', [])
            return cfg

        class OutputConfiguration:

            def __init__(self, active, primary, geometry, rotation, modename, refresh_rate=None):
                self.active = active
                self.primary = primary
                if active:
                    self.position = geometry.position
                    self.rotation = rotation
                    if rotation.is_odd:
                        self.mode = NamedSize(
                            Size(reversed(geometry.size)), name=modename, refresh_rate=refresh_rate)
                    else:
                        self.mode = NamedSize(geometry.size, name=modename, refresh_rate=refresh_rate)

            size = property(lambda self: NamedSize(
                Size(reversed(self.mode)), name=self.mode.name
            ) if self.rotation.is_odd else self.mode)
