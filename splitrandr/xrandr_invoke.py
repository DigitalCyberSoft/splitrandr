# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Mixin: shell out to xrandr (and helpers around its output).

All methods access ``self.environ`` and ``self.state``; intended to be
mixed into ``XRandR``.
"""

import re
import subprocess
import time
import warnings
import logging

log = logging.getLogger('splitrandr')


class XRandRInvokeMixin:

    def _xrandr_env(self):
        """Environ for invoking xrandr — always with LD_PRELOAD stripped.

        splitrandr never wants the xrandr binary itself to load the
        fakexrandr .so; the binary should always operate on the real
        X server state. If splitrandr was launched by an LD_PRELOAD'd
        cinnamon-session, our environ inherits LD_PRELOAD, and naive
        Popen(env=self.environ) would propagate it into xrandr — at
        which point xrandr starts seeing fake outputs and synthesised
        monitors, which previously forced us to rm fakexrandr.bin
        before each xrandr command (and that rm window is what
        SEGV'd Cinnamon when the freeze targeted the wrong PID).
        """
        env = dict(self.environ)
        env.pop('LD_PRELOAD', None)
        return env

    def _output(self, *args):
        log.info("xrandr %s", " ".join(args))
        proc = subprocess.Popen(
            ("xrandr",) + args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self._xrandr_env(),
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
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self._xrandr_env(),
        )
        out, err = proc.communicate()
        status = proc.wait()
        if status != 0:
            log.warning("xrandr (ignored) exit %d stderr: %s", status, err.decode('utf-8', errors='replace'))

    def _run_no_preload(self, *args):
        """Alias for _run; LD_PRELOAD is now stripped for ALL xrandr calls."""
        log.info("xrandr (no-preload) %s", " ".join(args))
        proc = subprocess.Popen(
            ("xrandr",) + args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self._xrandr_env(),
        )
        out, err = proc.communicate()
        status = proc.wait()
        if status != 0:
            log.error("xrandr (no-preload) exit %d stderr: %s", status, err.decode('utf-8', errors='replace'))
            raise Exception("XRandR returned error code %d: %s" % (status, err))

    def _refresh_edids(self):
        """Re-read EDIDs from the X server and update state.

        `xrandr --verbose` runs unhooked because `_xrandr_env()` strips
        LD_PRELOAD, so real physical outputs and their EDIDs are always
        visible regardless of whether fakexrandr.bin is present.
        Earlier versions required the bin to be rm'd first; that's no
        longer necessary.
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
        """Alias for _run_ignore_error; LD_PRELOAD is now stripped for ALL xrandr calls."""
        log.info("xrandr (no-preload, ignore-error) %s", " ".join(args))
        proc = subprocess.Popen(
            ("xrandr",) + args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self._xrandr_env(),
        )
        out, err = proc.communicate()
        status = proc.wait()
        if status != 0:
            log.warning("xrandr (no-preload, ignored) exit %d stderr: %s", status, err.decode('utf-8', errors='replace'))

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
