# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Wrapper around command line xrandr with --setmonitor support"""

import os
import re
import subprocess
import warnings
import logging
from functools import reduce

log = logging.getLogger('splitrandr')

from .auxiliary import (
    BetterList, Size, Position, Geometry, FileLoadError, FileSyntaxError,
    InadequateConfiguration, Rotation, ROTATIONS, NORMAL, NamedSize,
    MonitorGeometry,
)
from .splits import SplitTree
from .i18n import _

SHELLSHEBANG = '#!/bin/sh'


class Feature:
    PRIMARY = 1


class XRandR:
    DEFAULTTEMPLATE = [SHELLSHEBANG, '%(xrandr)s', '%(cinnamon_safe_setmonitors)s']

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

    #################### loading ####################

    def load_from_string(self, data):
        data = data.replace("%", "%%")
        lines = data.split("\n")
        if lines[-1] == '':
            lines.pop()

        if lines[0] != SHELLSHEBANG:
            raise FileLoadError('Not a shell script.')

        xrandrlines = [i for i, l in enumerate(
            lines) if l.strip().startswith('xrandr ')]
        if not xrandrlines:
            raise FileLoadError('No recognized xrandr command in this shell script.')

        # Find the main xrandr --output line (has --output in it)
        main_line_idx = None
        setmonitor_idxs = []
        delmonitor_idxs = []
        for idx in xrandrlines:
            line = lines[idx].strip()
            if '--setmonitor' in line:
                setmonitor_idxs.append(idx)
            elif '--delmonitor' in line:
                delmonitor_idxs.append(idx)
            elif '--output' in line:
                if main_line_idx is not None:
                    raise FileLoadError('More than one xrandr --output line in this shell script.')
                main_line_idx = idx

        if main_line_idx is None:
            raise FileLoadError('No xrandr --output line found in this shell script.')

        self._load_from_commandlineargs(lines[main_line_idx].strip())
        lines[main_line_idx] = '%(xrandr)s'

        # Parse --setmonitor lines to reconstruct splits
        self._load_splits_from_setmonitor_lines(
            [lines[i].strip() for i in setmonitor_idxs])

        # Remove setmonitor/delmonitor lines and Cinnamon-safety wrapper lines
        cinnamon_wrapper_patterns = [
            'CINNAMON_PID=', 'pgrep -x cinnamon',
            'gsettings set org.cinnamon.settings-daemon.plugins.xrandr',
            'kill -STOP "$CINNAMON_PID"', 'kill -CONT "$CINNAMON_PID"',
            'if [ -n "$CINNAMON_PID" ]', 'sleep 0.', 'fi',
            '# Cinnamon safety',
            # Readiness polling constructs (replacing blind sleeps)
            'gsettings get', '_i=0', '_v=$(', 'done',
            'xrandr --listmonitors >/dev/null',
            '# Wait for gsettings', '# X server round-trip',
        ]
        removal_idxs = set(setmonitor_idxs + delmonitor_idxs)
        for i, line in enumerate(lines):
            stripped = line.strip()
            if any(pat in stripped for pat in cinnamon_wrapper_patterns):
                removal_idxs.add(i)
        for idx in sorted(removal_idxs, reverse=True):
            if idx < len(lines) and lines[idx] != '%(xrandr)s':
                lines.pop(idx)

        # Ensure template has the combined setmonitor marker
        xrandr_idx = lines.index('%(xrandr)s')
        if '%(cinnamon_safe_setmonitors)s' not in lines:
            lines.insert(xrandr_idx + 1, '%(cinnamon_safe_setmonitors)s')

        return lines

    def _load_splits_from_setmonitor_lines(self, setmonitor_lines):
        """Parse xrandr --setmonitor lines and reconstruct SplitTree for each output."""
        # Group by base output name
        monitors_by_output = {}
        for line in setmonitor_lines:
            # xrandr --setmonitor NAME WIDTHpx/WIDTHmm x HEIGHTpx/HEIGHTmm + X + Y OUTPUT
            # or: xrandr --setmonitor NAME W/Wmm*H/Hmm+X+Y OUTPUT
            parts = line.split()
            if len(parts) < 4 or parts[0] != 'xrandr' or parts[1] != '--setmonitor':
                continue

            mon_name = parts[2]
            geom_str = parts[3]
            output = parts[4] if len(parts) > 4 else 'none'

            # Parse monitor name: OUTPUT~INDEX
            if '~' not in mon_name:
                continue
            base_output = mon_name.rsplit('~', 1)[0]

            # Parse geometry: W/Wmm x H/Hmm + X + Y
            # Format: "1920/605x2160/680+0+0"
            m = re.match(r'(\d+)/(\d+)x(\d+)/(\d+)\+(\d+)\+(\d+)', geom_str)
            if not m:
                continue

            w, w_mm, h, h_mm, x, y = [int(g) for g in m.groups()]

            if base_output not in monitors_by_output:
                monitors_by_output[base_output] = []
            monitors_by_output[base_output].append((x, y, w, h))

        # Reconstruct split trees
        for output_name, regions in monitors_by_output.items():
            if output_name not in self.configuration.outputs:
                continue
            output_cfg = self.configuration.outputs[output_name]
            if not output_cfg.active:
                continue

            total_w = output_cfg.size[0]
            total_h = output_cfg.size[1]

            # Normalize regions relative to output position
            ox, oy = output_cfg.position
            normalized = [(r[0] - ox, r[1] - oy, r[2], r[3]) for r in regions]

            tree = SplitTree.from_setmonitor_regions(normalized, output_name, total_w, total_h)
            if not tree.is_leaf:
                self.configuration.splits[output_name] = tree

    def _load_from_commandlineargs(self, commandline):
        self.load_from_x()

        args = BetterList(commandline.split(" "))
        if args.pop(0) != 'xrandr':
            raise FileSyntaxError()
        options = dict((a[0], a[1:]) for a in args.split('--output') if a)

        for output_name, output_argument in options.items():
            output = self.configuration.outputs[output_name]
            output_state = self.state.outputs[output_name]
            output.primary = False
            if output_argument == ['--off']:
                output.active = False
            else:
                if '--primary' in output_argument:
                    if Feature.PRIMARY in self.features:
                        output.primary = True
                    output_argument.remove('--primary')
                if len(output_argument) % 2 != 0:
                    raise FileSyntaxError()
                parts = [
                    (output_argument[2 * i], output_argument[2 * i + 1])
                    for i in range(len(output_argument) // 2)
                ]
                pending_rate = None
                for part in parts:
                    if part[0] == '--mode':
                        for namedmode in output_state.modes:
                            if namedmode.name == part[1]:
                                output.mode = namedmode
                                break
                        else:
                            raise FileLoadError("Not a known mode: %s" % part[1])
                    elif part[0] == '--rate':
                        pending_rate = float(part[1])
                    elif part[0] == '--pos':
                        output.position = Position(part[1])
                    elif part[0] == '--rotate':
                        if part[1] not in ROTATIONS:
                            raise FileSyntaxError()
                        output.rotation = Rotation(part[1])
                    else:
                        raise FileSyntaxError()
                # If a rate was specified, try to find the exact mode with that rate
                if pending_rate is not None and hasattr(output, 'mode'):
                    for namedmode in output_state.modes:
                        if namedmode.name == output.mode.name and namedmode.refresh_rate is not None:
                            if abs(namedmode.refresh_rate - pending_rate) < 0.1:
                                output.mode = namedmode
                                break
                output.active = True

    def load_from_x(self):
        self.configuration = self.Configuration(self)
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

        # Load existing virtual monitors from X
        self._load_monitors()

    def _load_monitors(self):
        """Parse xrandr --listmonitors and reconstruct splits for existing virtual monitors."""
        try:
            output = self._output("--listmonitors")
        except Exception:
            return

        # Parse lines like:
        #  0: +*DP-5 3840/1210x2160/680+0+0  DP-5
        #  1: +DP-5~0 1920/605x2160/680+0+0  DP-5
        monitors_by_output = {}
        for line in output.strip().split('\n'):
            line = line.strip()
            if line.startswith('Monitors:'):
                continue
            m = re.match(
                r'\d+:\s+[+*]*(\S+)\s+(\d+)/(\d+)x(\d+)/(\d+)\+(\d+)\+(\d+)(?:\s+(\S+))?',
                line
            )
            if not m:
                continue
            mon_name = m.group(1)
            w, w_mm, h, h_mm, x, y = [int(m.group(i)) for i in range(2, 8)]
            real_output = m.group(8) or ""

            # Only look at virtual monitors matching OUTPUT~N pattern
            if '~' not in mon_name:
                continue
            base_output = mon_name.rsplit('~', 1)[0]
            try:
                idx = int(mon_name.rsplit('~', 1)[1])
            except ValueError:
                continue

            if base_output not in monitors_by_output:
                monitors_by_output[base_output] = []
            monitors_by_output[base_output].append((x, y, w, h))

        for output_name, regions in monitors_by_output.items():
            if output_name not in self.configuration.outputs:
                continue
            output_cfg = self.configuration.outputs[output_name]
            if not output_cfg.active:
                continue

            total_w = output_cfg.size[0]
            total_h = output_cfg.size[1]
            ox, oy = output_cfg.position
            normalized = [(r[0] - ox, r[1] - oy, r[2], r[3]) for r in regions]

            tree = SplitTree.from_setmonitor_regions(normalized, output_name, total_w, total_h)
            if not tree.is_leaf:
                self.configuration.splits[output_name] = tree

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

    def save_to_shellscript_string(self, template=None, additional=None):
        if not template:
            template = self.DEFAULTTEMPLATE
        template = '\n'.join(template) + '\n'

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
            commands = tree.to_setmonitor_commands(
                output_name,
                output_cfg.size[0], output_cfg.size[1],
                output_cfg.position[0], output_cfg.position[1],
                w_mm, h_mm
            )
            for mon_name, geom, out in commands:
                del_lines.append("xrandr --delmonitor %s 2>/dev/null || true" % mon_name)
                set_lines.append("xrandr --setmonitor %s %s %s" % (mon_name, geom, out))

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
                'if [ -n "$CINNAMON_PID" ]; then\n'
                '  # X server round-trip to flush pending RandR events\n'
                '  xrandr --listmonitors >/dev/null 2>&1\n'
                '  kill -CONT "$CINNAMON_PID" 2>/dev/null\n'
                'fi'
            )
        else:
            cinnamon_safe = ''

        data = {
            'xrandr': "xrandr " + " ".join(self.configuration.commandlineargs()),
            'delmonitors': '\n'.join(del_lines),
            'setmonitors': '\n'.join(set_lines),
            'cinnamon_safe_setmonitors': cinnamon_safe,
        }
        if additional:
            data.update(additional)

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

        # Apply main configuration (before any setmonitor calls)
        log.info("applying main xrandr config")
        self._run(*self.configuration.commandlineargs())

        # Wrap delmonitor + setmonitor in Cinnamon safety guard.
        # Muffin >= 5.4.0 segfaults on setmonitor events, so we
        # SIGSTOP Cinnamon and disable csd-xrandr during these calls.
        from .cinnamon_compat import CinnamonSetMonitorGuard
        with CinnamonSetMonitorGuard():
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
                            self._run_ignore_error("--delmonitor", mon_name)
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
                commands = tree.to_setmonitor_commands(
                    output_name,
                    output_cfg.size[0], output_cfg.size[1],
                    output_cfg.position[0], output_cfg.position[1],
                    w_mm, h_mm
                )
                log.info("creating %d virtual monitors for %s", len(commands), output_name)
                for mon_name, geom, out in commands:
                    log.info("  setmonitor %s %s %s", mon_name, geom, out)
                    self._run("--setmonitor", mon_name, geom, out)

            # Write fakexrandr config BEFORE Cinnamon resumes, so it
            # reads the new config when it processes the queued RandR events.
            try:
                from .fakexrandr_config import write_fakexrandr_config
                write_fakexrandr_config(
                    self.configuration.splits, self.state, self.configuration
                )
            except Exception as e:
                log.warning("fakexrandr config write failed: %s", e)

        # Nudge Muffin to re-read screen resources by re-applying
        # the xrandr config. This generates CrtcChange events; when
        # Muffin handles them it calls XRRGetScreenResources, which
        # triggers fakexrandr to re-read the updated config file.
        try:
            from .fakexrandr_config import is_cinnamon_fakexrandr_loaded
            if is_cinnamon_fakexrandr_loaded():
                log.info("nudging Muffin to re-read fakexrandr config")
                # X server round-trip to flush pending events
                try:
                    self._output("--listmonitors")
                except Exception:
                    import time
                    time.sleep(0.3)  # fallback
                self._run(*self.configuration.commandlineargs())
        except Exception as e:
            log.warning("fakexrandr nudge failed: %s", e)

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
                is_cinnamon_fakexrandr_loaded, _find_fakexrandr_lib,
                restart_cinnamon_with_fakexrandr, restart_cinnamon_without_fakexrandr,
                write_cinnamon_monitors_xml,
            )
            has_splits = any(
                not tree.is_leaf
                for tree in self.configuration.splits.values()
            )

            # Always write monitors.xml so Muffin preserves our
            # display settings (positions, modes, primary) on restart.
            try:
                write_cinnamon_monitors_xml(
                    self.configuration.splits, self.state, self.configuration
                )
            except Exception as e:
                log.warning("monitors.xml write failed: %s", e)

            if has_splits and not is_cinnamon_fakexrandr_loaded():
                lib_path = _find_fakexrandr_lib()
                if lib_path:
                    log.info("Cinnamon doesn't have fakexrandr loaded, restarting")
                    restart_cinnamon_with_fakexrandr(lib_path)

                    # Wait for Cinnamon to be ready on D-Bus
                    log.info("waiting for Cinnamon to settle")
                    from .cinnamon_compat import _wait_cinnamon_on_dbus
                    if not _wait_cinnamon_on_dbus(timeout=15.0):
                        log.warning("Cinnamon did not respond on D-Bus within timeout, proceeding anyway")
                    log.info("re-applying xrandr config after Cinnamon restart")
                    self._run(*self.configuration.commandlineargs())

                    with CinnamonSetMonitorGuard():
                        try:
                            listmon_output = self._output("--listmonitors")
                            for line in listmon_output.strip().split('\n'):
                                line = line.strip()
                                if line.startswith('Monitors:'):
                                    continue
                                m_mon = re.match(r'\d+:\s+[+*]*(\S+)', line)
                                if m_mon and '~' in m_mon.group(1):
                                    self._run_ignore_error("--delmonitor", m_mon.group(1))
                        except Exception:
                            pass

                        for output_name, tree in self.configuration.splits.items():
                            output_cfg = self.configuration.outputs.get(output_name)
                            if not output_cfg or not output_cfg.active:
                                continue
                            output_state = self.state.outputs.get(output_name)
                            w_mm = output_state.physical_w_mm if output_state else 0
                            h_mm = output_state.physical_h_mm if output_state else 0
                            commands = tree.to_setmonitor_commands(
                                output_name,
                                output_cfg.size[0], output_cfg.size[1],
                                output_cfg.position[0], output_cfg.position[1],
                                w_mm, h_mm
                            )
                            for mon_name, geom, out in commands:
                                self._run("--setmonitor", mon_name, geom, out)

                        from .fakexrandr_config import write_fakexrandr_config
                        write_fakexrandr_config(
                            self.configuration.splits, self.state, self.configuration
                        )

                    log.info("re-applied config after Cinnamon restart")

            elif not has_splits and is_cinnamon_fakexrandr_loaded():
                log.info("no splits active, restarting Cinnamon without fakexrandr")
                restart_cinnamon_without_fakexrandr()
        except Exception as e:
            log.warning("fakexrandr integration failed: %s", e)

        log.info("=== save_to_x: done ===")

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
            self._xrandr = xrandr

        def __repr__(self):
            return '<%s for %d Outputs, %d active>' % (
                type(self).__name__, len(self.outputs),
                len([x for x in self.outputs.values() if x.active])
            )

        def commandlineargs(self):
            args = []
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
