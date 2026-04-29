# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Mixin: parse xrandr --verbose / --listmonitors and populate
configuration + state. Intended to be mixed into ``XRandR``.
"""

import os
import re
import warnings
import logging
from functools import reduce

from .auxiliary import Size, Geometry, NamedSize, Rotation, ROTATIONS, NORMAL
from .splits import SplitTree
from .xrandr_types import Feature

log = logging.getLogger('splitrandr')


class XRandRLoadMixin:

    def load_from_x(self):
        # Preserve borders, output-level primary, and per-leaf primary
        # across reloads — none of these are reliably discoverable from
        # the X server state alone:
        #   - borders are pure user state
        #   - split trees are reconstructed from setmonitor geometry,
        #     which carries no primary marker
        #   - output primary: on Nvidia tiled-display setups, --primary
        #     on a sub-tile (e.g. DP-5~3) gets eaten by the driver's
        #     tile collapse/re-expand cycle, so xrandr --query reports
        #     no primary anywhere. Without this snapshot we'd clear the
        #     user's primary choice on every save_to_x's trailing
        #     load_from_x call, then the gui re-saves the cleared
        #     state into the active profile.
        prev = getattr(self, 'configuration', None)
        old_borders = dict(prev.borders) if prev else {}
        old_primary = set()
        old_leaf_primary = {}
        if prev:
            for name, out in prev.outputs.items():
                if getattr(out, 'primary', False):
                    old_primary.add(name)
            for name, tree in prev.splits.items():
                idx = tree.primary_leaf_index()
                if idx is not None:
                    old_leaf_primary[name] = idx
        self.configuration = self.Configuration(self)
        self.configuration.borders = old_borders
        self._pending_primary = old_primary
        self._pending_leaf_primary = old_leaf_primary
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

        # Restore output-level primary (preserved across reload). The
        # X server may not report any primary at all on Nvidia tiled
        # setups; in that case we trust the user's prior choice rather
        # than silently dropping it. Done BEFORE leaf primary so the
        # leaf-primary guard below sees the restored output.primary.
        any_x_primary = any(
            getattr(out, 'primary', False)
            for out in self.configuration.outputs.values()
        )
        if not any_x_primary and self._pending_primary:
            for name in self._pending_primary:
                cfg_out = self.configuration.outputs.get(name)
                if cfg_out and cfg_out.active:
                    cfg_out.primary = True
        self._pending_primary = set()

        # Restore per-leaf primary (preserved across reload). Only honor
        # entries whose physical output is still present, primary, and
        # split with the same leaf count.
        for name, idx in self._pending_leaf_primary.items():
            cfg_out = self.configuration.outputs.get(name)
            tree = self.configuration.splits.get(name)
            if not cfg_out or not cfg_out.primary or not tree or tree.is_leaf:
                continue
            leaves = list(tree.iter_leaves())
            if 0 <= idx < len(leaves):
                tree.set_primary_at(idx)
        self._pending_leaf_primary = {}

        # On hardware that surfaces splits via fakexrandr LD_PRELOAD only
        # (no setmonitor VMs at the X server level — see
        # feedback_synthesis_xor_setmonitors), splitrandr's own xrandr
        # invocations strip LD_PRELOAD and therefore see only parent
        # outputs. The X-side reconstruction above (`_load_monitors`,
        # `_pending_leaf_primary`) cannot recover split trees in that
        # case. Layer two fallbacks, both skip-if-present:
        #   1. Cinnamon's live MetaMonitor list — what the user is
        #      currently looking at, derived by the .so from
        #      fakexrandr.bin. Authoritative for "what's on screen now".
        #   2. layout.json — canonical saved state, fills any output
        #      Cinnamon doesn't currently surface (e.g. an unplugged
        #      output that the user has saved splits for).
        # Both calls are idempotent: they don't clobber splits that
        # X-side reconstruction already populated, and don't clobber
        # each other. load_from_json (full replacement) ignores these
        # pre-fills because it rebuilds configuration from the file.
        try:
            self.merge_splits_from_cinnamon()
        except Exception:
            # cinnamon DBus failure: tolerate, fall through to json.
            pass
        try:
            self.merge_splits_from_json(
                os.path.expanduser('~/.config/splitrandr/layout.json')
            )
        except Exception:
            pass

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
