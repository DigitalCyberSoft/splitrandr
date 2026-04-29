# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Mixin: per-monitor controls panel for ``Application``.

Owns the Resolution/Refresh-rate/Rotation/Active/Primary/Splits/Border
widgets created by :mod:`gui_app_layout`. Each callback reads the
current selection from ``self.widget`` and pushes the change back into
the in-memory configuration via the appropriate ``self.widget.set_*``
helper. ``_update_controls_for_selection`` is the single point where
combo state is recomputed from configuration.
"""

import math

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import GLib

from .auxiliary import InadequateConfiguration, ROTATIONS, Rotation
from .xrandr import Feature
from .i18n import _


class ApplicationControlsMixin:

    #################### control panel sync ####################

    def _on_selection_changed(self, _widget):
        # Mirror selection to the current widget via geometry matching
        name = self.widget.selected_output
        mirror_name = None
        if name and self.current_widget._is_cinnamon:
            # Match by geometry: find a Cinnamon monitor at the same position/size
            cfg = self.widget._xrandr.configuration.outputs.get(name)
            if cfg and cfg.active:
                px, py = cfg.position
                pw, ph = cfg.size
                for mon in self.current_widget._monitors:
                    if (mon['x'] == px and mon['y'] == py and
                            mon['w'] == pw and mon['h'] == ph):
                        mirror_name = mon['name']
                        break
        elif name:
            # Fallback: direct name match (xrandr-mode current widget)
            mirror_name = name

        if self.current_widget._selected_output != mirror_name:
            self.current_widget._selected_output = mirror_name
            self.current_widget._force_repaint()
        self._update_controls_for_selection()

    def _on_widget_changed(self, _widget):
        self._update_controls_for_selection()

    def _update_controls_for_selection(self):
        self._updating_controls = True
        try:
            name = self.widget.selected_output
            xrandr = self.widget._xrandr

            phys_name, leaf_idx = self.widget.parse_virtual_name(name) if name else (None, None)
            if phys_name is None or phys_name not in xrandr.configuration.outputs:
                self._output_label.set_markup(
                    "<b>" + _("No monitor selected") + "</b>"
                )
                self._controls_box.set_sensitive(False)
                self._output_label.set_sensitive(True)
                return

            self._controls_box.set_sensitive(True)
            output_cfg = xrandr.configuration.outputs[phys_name]
            output_state = xrandr.state.outputs[phys_name]

            # Label — show full virtual name when a leaf is selected
            self._output_label.set_markup("<b>%s</b>" % GLib.markup_escape_text(name))

            # Active switch
            self._active_switch.set_active(output_cfg.active)

            if not output_cfg.active:
                self._primary_btn.set_sensitive(False)
                self._res_combo.set_sensitive(False)
                self._rate_combo.set_sensitive(False)
                self._rot_combo.set_sensitive(False)
                self._split_btn.set_sensitive(False)
                self._remove_splits_btn.set_sensitive(False)
                self._border_label.set_sensitive(False)
                self._border_spin.set_sensitive(False)
                self._border_px_label.set_sensitive(False)
                self._res_combo.remove_all()
                self._rate_combo.remove_all()
                self._rot_combo.remove_all()
                return

            # Primary button — when a virtual leaf is selected, the button
            # tracks the leaf's primary state, not just the parent's.
            if Feature.PRIMARY in xrandr.features:
                self._primary_btn.set_sensitive(True)
                if leaf_idx is not None:
                    tree = xrandr.configuration.splits.get(phys_name)
                    leaf_primary = (output_cfg.primary and tree is not None
                                    and tree.primary_leaf_index() == leaf_idx)
                    is_primary = leaf_primary
                else:
                    is_primary = output_cfg.primary
                self._primary_btn.set_label(
                    _("Is Primary") if is_primary else _("Set as Primary"))
            else:
                self._primary_btn.set_sensitive(False)

            # Resolution combo
            self._res_combo.set_sensitive(True)
            self._res_combo.remove_all()
            modes_grouped = output_state.modes_by_resolution()

            # Compute aspect ratio string for each resolution
            def _aspect_ratio(w, h):
                g = math.gcd(w, h)
                return "%d:%d" % (w // g, h // g)

            # Determine native aspect ratio from preferred mode
            pref = output_state.preferred_resolution
            if pref:
                native_ratio = _aspect_ratio(*pref)
            else:
                # Fallback: use the highest-pixel resolution
                biggest = max(modes_grouped.keys(), key=lambda k: k[0] * k[1])
                native_ratio = _aspect_ratio(*biggest)

            # Sort: native ratio group first, then others; within each group by pixels desc
            sorted_res = sorted(
                modes_grouped.keys(),
                key=lambda k: (0 if _aspect_ratio(*k) == native_ratio else 1, -(k[0] * k[1]))
            )

            current_res_idx = -1
            for i, (w, h) in enumerate(sorted_res):
                ratio = _aspect_ratio(w, h)
                label = "%dx%d (%s)" % (w, h, ratio)
                self._res_combo.append("%dx%d" % (w, h), label)
                if output_cfg.mode.width == w and output_cfg.mode.height == h:
                    current_res_idx = i
            if current_res_idx >= 0:
                self._res_combo.set_active(current_res_idx)

            # Refresh rate combo (populated based on current resolution)
            self._populate_rate_combo(output_state, output_cfg)

            # Rotation combo
            self._rot_combo.set_sensitive(True)
            self._rot_combo.remove_all()
            current_rot_idx = -1
            for i, rot in enumerate(ROTATIONS):
                self._rot_combo.append(str(rot), str(rot).capitalize())
                if output_cfg.rotation == rot:
                    current_rot_idx = i
            if current_rot_idx >= 0:
                self._rot_combo.set_active(current_rot_idx)

            # Split buttons
            self._split_btn.set_sensitive(True)
            has_splits = phys_name in xrandr.configuration.splits
            self._remove_splits_btn.set_sensitive(has_splits)

            # Border spin — sensitive for any active output
            self._border_label.set_sensitive(True)
            self._border_spin.set_sensitive(True)
            self._border_px_label.set_sensitive(True)
            self._border_spin.set_value(
                xrandr.configuration.borders.get(phys_name, 0)
            )

        finally:
            self._updating_controls = False

    def _populate_rate_combo(self, output_state, output_cfg):
        self._rate_combo.set_sensitive(True)
        self._rate_combo.remove_all()
        current_w, current_h = output_cfg.mode.width, output_cfg.mode.height
        modes_grouped = output_state.modes_by_resolution()
        rates = modes_grouped.get((current_w, current_h), [])

        current_rate_idx = -1
        has_any_rate = False
        for i, mode in enumerate(rates):
            if mode.refresh_rate is not None:
                has_any_rate = True
                label = "%.2f Hz" % mode.refresh_rate
                self._rate_combo.append(str(i), label)
                if (output_cfg.mode.refresh_rate is not None and
                        abs(mode.refresh_rate - output_cfg.mode.refresh_rate) < 0.1):
                    current_rate_idx = i
            else:
                label = mode.name
                self._rate_combo.append(str(i), label)
                if mode.name == output_cfg.mode.name:
                    current_rate_idx = i

        if not has_any_rate and len(rates) <= 1:
            self._rate_combo.set_sensitive(False)

        if current_rate_idx >= 0:
            self._rate_combo.set_active(current_rate_idx)

    #################### control callbacks ####################

    def _on_resolution_changed(self):
        if self._updating_controls:
            return
        name = self.widget.selected_output
        if not name:
            return
        phys, _leaf = self.widget.parse_virtual_name(name)
        res_id = self._res_combo.get_active_id()
        if not res_id:
            return

        xrandr = self.widget._xrandr
        output_state = xrandr.state.outputs[phys]

        # Parse WxH from id
        parts = res_id.split('x')
        w, h = int(parts[0]), int(parts[1])

        modes_grouped = output_state.modes_by_resolution()
        modes_for_res = modes_grouped.get((w, h), [])
        if not modes_for_res:
            return

        # Pick the highest refresh rate mode by default
        best_mode = modes_for_res[0]
        try:
            self.widget.set_resolution(phys, best_mode)
        except InadequateConfiguration as exc:
            self.widget.error_message(
                _("Setting this resolution is not possible here: %s") % exc
            )

    def _on_refresh_changed(self):
        if self._updating_controls:
            return
        name = self.widget.selected_output
        if not name:
            return
        phys, _leaf = self.widget.parse_virtual_name(name)
        rate_id = self._rate_combo.get_active_id()
        if rate_id is None:
            return

        xrandr = self.widget._xrandr
        output_state = xrandr.state.outputs[phys]
        output_cfg = xrandr.configuration.outputs[phys]
        current_w, current_h = output_cfg.mode.width, output_cfg.mode.height

        modes_grouped = output_state.modes_by_resolution()
        modes_for_res = modes_grouped.get((current_w, current_h), [])

        idx = int(rate_id)
        if 0 <= idx < len(modes_for_res):
            mode = modes_for_res[idx]
            try:
                self.widget.set_resolution(phys, mode)
            except InadequateConfiguration as exc:
                self.widget.error_message(
                    _("Setting this refresh rate is not possible here: %s") % exc
                )

    def _on_rotation_changed(self):
        if self._updating_controls:
            return
        name = self.widget.selected_output
        if not name:
            return
        phys, _leaf = self.widget.parse_virtual_name(name)
        rot_id = self._rot_combo.get_active_id()
        if not rot_id:
            return
        try:
            self.widget.set_rotation(phys, Rotation(rot_id))
        except InadequateConfiguration as exc:
            self.widget.error_message(
                _("This orientation is not possible here: %s") % exc
            )

    def _on_active_toggled(self):
        if self._updating_controls:
            return
        name = self.widget.selected_output
        if not name:
            return
        phys, _leaf = self.widget.parse_virtual_name(name)
        active = self._active_switch.get_active()
        try:
            self.widget.set_active(phys, active)
        except InadequateConfiguration as exc:
            self.widget.error_message(str(exc))

    def _on_primary_clicked(self):
        name = self.widget.selected_output
        if not name:
            return
        phys, leaf_idx = self.widget.parse_virtual_name(name)
        cfg = self.widget._xrandr.configuration
        output_cfg = cfg.outputs.get(phys)
        if not output_cfg:
            return
        if leaf_idx is None:
            self.widget.set_primary(phys, not output_cfg.primary)
            return
        tree = cfg.splits.get(phys)
        currently = (output_cfg.primary and tree is not None
                     and tree.primary_leaf_index() == leaf_idx)
        self.widget.set_primary(phys, not currently,
                                leaf_idx=None if currently else leaf_idx)

    def _resolve_split_target(self):
        """Resolve which physical output to operate on.

        Picks, in order: the currently-selected output's physical
        parent; the primary; then the first active monitor.  Returns
        the physical connector name, or None if the configuration has
        no active outputs.
        """
        name = self.widget.selected_output
        if name:
            phys, _ = self.widget.parse_virtual_name(name)
            if phys in self.widget._xrandr.configuration.outputs:
                return phys
        cfg = self.widget._xrandr.configuration
        for n, out in cfg.outputs.items():
            if out.active and out.primary:
                return n
        for n, out in cfg.outputs.items():
            if out.active:
                return n
        return None

    def _on_split_clicked(self):
        phys = self._resolve_split_target()
        if not phys:
            return
        self.widget._on_split_monitor(None, phys)

    def _on_remove_splits_clicked(self):
        phys = self._resolve_split_target()
        if not phys:
            return
        self.widget._on_remove_splits(None, phys)

    def _on_border_changed(self):
        if self._updating_controls:
            return
        name = self.widget.selected_output
        if not name:
            return
        phys, _ = self.widget.parse_virtual_name(name)
        xrandr = self.widget._xrandr
        val = int(self._border_spin.get_value())
        if val > 0:
            xrandr.configuration.borders[phys] = val
        else:
            xrandr.configuration.borders.pop(phys, None)
        self.widget._force_repaint()

    def _on_detect_displays(self):
        self.current_widget.load_from_cinnamon()
        self.widget.load_from_x()

    def _on_reset_defaults(self):
        self.widget.load_from_x()

    #################### zoom ####################

    def _on_zoom_toggled(self, button, value):
        if button.get_active():
            self.widget.factor = value
            self.current_widget.factor = value
