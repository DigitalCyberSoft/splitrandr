# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Mixin: build the main GTK window layout for ``Application``.

Holds :meth:`Application._build_page`, which wires together the three
preview panes (Proposed/Current/Real), the per-monitor controls box, the
profile + tray + zoom settings row, and the bottom action bar. All of
the widgets it creates are stored on ``self`` and consumed by the other
``Application`` mixins.
"""

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk

from . import profiles
from .i18n import _


class ApplicationLayoutMixin:

    def _build_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Three preview panes stacked vertically inside one scrollable
        # area: Proposed (editable) on top, then Virtual (current
        # Cinnamon view), then Real (physical layout, no virtual).
        def _labelled_pane(label_text, pane_widget):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            label = Gtk.Label()
            label.set_markup("<b>" + label_text + "</b>")
            label.set_margin_top(6)
            label.set_margin_bottom(2)
            label.set_halign(Gtk.Align.START)
            label.set_margin_start(8)
            box.pack_start(label, False, False, 0)
            frame = Gtk.Frame()
            frame.set_shadow_type(Gtk.ShadowType.IN)
            inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            inner.set_halign(Gtk.Align.CENTER)
            inner.pack_start(pane_widget, False, False, 0)
            frame.add(inner)
            box.pack_start(frame, False, False, 0)
            return box

        previews_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        previews_box.set_margin_start(8)
        previews_box.set_margin_end(8)
        previews_box.set_margin_top(4)
        previews_box.set_margin_bottom(4)
        previews_box.pack_start(
            _labelled_pane(_("Proposed"), self.widget), False, False, 0)
        previews_box.pack_start(
            _labelled_pane(_("Current (virtual)"), self.current_widget),
            False, False, 0)
        previews_box.pack_start(
            _labelled_pane(_("Real (no virtual)"), self.original_widget),
            False, False, 0)

        previews_scroll = Gtk.ScrolledWindow()
        previews_scroll.set_policy(
            Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        previews_scroll.add(previews_box)
        # Reserve enough vertical room that the user can see at least
        # one pane without scrolling but can scroll for the rest.
        previews_scroll.set_min_content_height(420)

        page.pack_start(previews_scroll, True, True, 0)

        page.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

        # Controls box (below preview) — monitor-specific settings
        self._controls_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._controls_box.set_margin_start(12)
        self._controls_box.set_margin_end(12)
        self._controls_box.set_margin_top(8)
        self._controls_box.set_margin_bottom(4)

        # Row 1: Output selector label + Primary button + Active switch
        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self._output_label = Gtk.Label()
        self._output_label.set_markup("<b>" + _("No monitor selected") + "</b>")
        self._output_label.set_halign(Gtk.Align.START)
        row1.pack_start(self._output_label, True, True, 0)

        self._primary_btn = Gtk.Button(label=_("Set as Primary"))
        self._primary_btn.connect('clicked', lambda b: self._on_primary_clicked())
        row1.pack_start(self._primary_btn, False, False, 0)

        active_label = Gtk.Label(label=_("Active"))
        row1.pack_start(active_label, False, False, 0)
        self._active_switch = Gtk.Switch()
        self._active_switch.connect('notify::active', lambda s, p: self._on_active_toggled())
        row1.pack_start(self._active_switch, False, False, 0)

        self._controls_box.pack_start(row1, False, False, 0)

        # Row 2: Resolution + Refresh Rate + Rotation
        row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row2.set_homogeneous(True)

        # Resolution
        res_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        res_box.pack_start(Gtk.Label(label=_("Resolution"), halign=Gtk.Align.START), False, False, 0)
        self._res_combo = Gtk.ComboBoxText()
        self._res_combo.connect('changed', lambda c: self._on_resolution_changed())
        res_box.pack_start(self._res_combo, False, False, 0)
        row2.pack_start(res_box, True, True, 0)

        # Refresh Rate
        rate_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        rate_box.pack_start(Gtk.Label(label=_("Refresh Rate"), halign=Gtk.Align.START), False, False, 0)
        self._rate_combo = Gtk.ComboBoxText()
        self._rate_combo.connect('changed', lambda c: self._on_refresh_changed())
        rate_box.pack_start(self._rate_combo, False, False, 0)
        row2.pack_start(rate_box, True, True, 0)

        # Rotation
        rot_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        rot_box.pack_start(Gtk.Label(label=_("Rotation"), halign=Gtk.Align.START), False, False, 0)
        self._rot_combo = Gtk.ComboBoxText()
        self._rot_combo.connect('changed', lambda c: self._on_rotation_changed())
        rot_box.pack_start(self._rot_combo, False, False, 0)
        row2.pack_start(rot_box, True, True, 0)

        self._controls_box.pack_start(row2, False, False, 0)

        # Row 3: Split buttons
        row3 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self._split_btn = Gtk.Button(label=_("Split Monitor..."))
        self._split_btn.connect('clicked', lambda b: self._on_split_clicked())
        row3.pack_start(self._split_btn, False, False, 0)

        self._remove_splits_btn = Gtk.Button(label=_("Remove Splits"))
        self._remove_splits_btn.connect('clicked', lambda b: self._on_remove_splits_clicked())
        row3.pack_start(self._remove_splits_btn, False, False, 0)

        row3.pack_start(Gtk.Box(), True, True, 0)  # spacer

        self._border_label = Gtk.Label(label=_("Border:"))
        row3.pack_start(self._border_label, False, False, 0)
        adj = Gtk.Adjustment(value=0, lower=0, upper=50, step_increment=1, page_increment=5)
        self._border_spin = Gtk.SpinButton(adjustment=adj, climb_rate=1, digits=0)
        self._border_spin.set_width_chars(3)
        self._border_spin.connect('value-changed', lambda s: self._on_border_changed())
        row3.pack_start(self._border_spin, False, False, 0)
        self._border_px_label = Gtk.Label(label=_("px"))
        row3.pack_start(self._border_px_label, False, False, 0)

        self._controls_box.pack_start(row3, False, False, 0)

        page.pack_start(self._controls_box, False, False, 0)

        page.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

        # Settings section (profiles, tray, layout files, zoom)
        settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        settings_box.set_margin_start(12)
        settings_box.set_margin_end(12)
        settings_box.set_margin_top(6)
        settings_box.set_margin_bottom(4)

        # Profiles row
        prof_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        prof_row.pack_start(Gtk.Label(label=_("Profile:"), halign=Gtk.Align.START), False, False, 0)
        self._profile_combo = Gtk.ComboBoxText()
        self._profile_combo.connect('changed', lambda c: self._on_profile_combo_changed())
        prof_row.pack_start(self._profile_combo, True, True, 0)

        save_btn = Gtk.Button(label=_("Save..."))
        save_btn.connect('clicked', lambda b: self.do_save_profile())
        prof_row.pack_start(save_btn, False, False, 0)

        delete_btn = Gtk.Button(label=_("Delete"))
        delete_btn.connect('clicked', lambda b: self._on_delete_selected_profile())
        prof_row.pack_start(delete_btn, False, False, 0)

        apply_login_btn = Gtk.Button(label=_("Apply && Autostart"))
        apply_login_btn.connect('clicked', lambda b: self.do_apply_autostart())
        prof_row.pack_start(apply_login_btn, False, False, 0)

        settings_box.pack_start(prof_row, False, False, 0)

        # Tray + Zoom row
        misc_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)

        # Tray switch
        tray_sub = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        tray_sub.pack_start(Gtk.Label(label=_("Tray icon:")), False, False, 0)
        self._tray_switch = Gtk.Switch()
        self._tray_switch.set_active(
            profiles.get_setting('tray_enabled', 'false') == 'true'
        )
        self._tray_switch.connect('notify::active', lambda s, p: self._on_tray_toggled_switch())
        tray_sub.pack_start(self._tray_switch, False, False, 0)
        misc_row.pack_start(tray_sub, False, False, 0)

        # Zoom radios
        zoom_sub = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        zoom_sub.pack_start(Gtk.Label(label=_("Zoom:")), False, False, 0)
        self._zoom_radios = {}
        prev = None
        for val in (4, 8, 16):
            label = "1:%d" % val
            if prev is None:
                rb = Gtk.RadioButton.new_with_label(None, label)
            else:
                rb = Gtk.RadioButton.new_with_label_from_widget(prev, label)
            if val == self.widget.factor:
                rb.set_active(True)
            rb.connect('toggled', self._on_zoom_toggled, val)
            zoom_sub.pack_start(rb, False, False, 0)
            self._zoom_radios[val] = rb
            prev = rb
        misc_row.pack_start(zoom_sub, False, False, 0)

        settings_box.pack_start(misc_row, False, False, 0)

        page.pack_start(settings_box, False, False, 0)

        page.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

        # Bottom action bar
        action_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_bar.set_margin_start(12)
        action_bar.set_margin_end(12)
        action_bar.set_margin_top(8)
        action_bar.set_margin_bottom(8)

        detect_btn = Gtk.Button(label=_("Detect Displays"))
        detect_btn.connect('clicked', lambda b: self._on_detect_displays())
        action_bar.pack_start(detect_btn, False, False, 0)

        reload_cin_btn = Gtk.Button(label=_("Reload Cinnamon"))
        reload_cin_btn.connect('clicked', lambda b: self._on_reload_cinnamon())
        action_bar.pack_start(reload_cin_btn, False, False, 0)

        about_btn = Gtk.Button(label=_("About"))
        about_btn.connect('clicked', lambda b: self.about())
        action_bar.pack_start(about_btn, False, False, 0)

        # Explicit exit — bypasses the close-to-tray behaviour of the
        # window's X button so the user can actually quit splitrandr
        # without dropping to a terminal.
        exit_btn = Gtk.Button(label=_("Exit"))
        exit_btn.connect('clicked', lambda b: self._do_quit())
        action_bar.pack_start(exit_btn, False, False, 0)

        # libXrandr.so status indicator — colored dot + version label.
        # Refreshes every 3s so the user can SEE when a Cinnamon
        # restart actually picks up the .so.
        self._fxr_status_label = Gtk.Label()
        self._fxr_status_label.set_use_markup(True)
        self._fxr_status_label.set_margin_start(12)
        self._fxr_status_label.set_tooltip_text(
            _("libXrandr.so status in the running Cinnamon process.\n"
              "Green: loaded and current.\n"
              "Yellow: loaded but stale (Reload Cinnamon to refresh).\n"
              "Red: not loaded — splits not visible to the WM.")
        )
        action_bar.pack_start(self._fxr_status_label, False, False, 0)
        self._refresh_fxr_status()
        from gi.repository import GLib
        GLib.timeout_add_seconds(3, self._refresh_fxr_status_periodic)

        action_bar.pack_start(Gtk.Box(), True, True, 0)  # spacer

        reset_btn = Gtk.Button(label=_("Reset to Defaults"))
        reset_btn.connect('clicked', lambda b: self._on_reset_defaults())
        action_bar.pack_start(reset_btn, False, False, 0)

        apply_btn = Gtk.Button(label=_("Apply"))
        apply_btn.get_style_context().add_class('suggested-action')
        apply_btn.connect('clicked', lambda b: self.do_apply())
        action_bar.pack_start(apply_btn, False, False, 0)

        page.pack_start(action_bar, False, False, 0)

        return page

    def _refresh_fxr_status(self):
        """Update the libXrandr.so status indicator label."""
        from .fakexrandr_config import (
            is_cinnamon_fakexrandr_loaded,
            is_cinnamon_fakexrandr_current,
            _get_cinnamon_fakexrandr_path,
            _get_so_config_version,
            _find_fakexrandr_lib,
        )

        try:
            loaded_path = _get_cinnamon_fakexrandr_path()
            ondisk_path = _find_fakexrandr_lib()
            ondisk_ver = _get_so_config_version(ondisk_path) if ondisk_path else 0

            if not loaded_path:
                # Not loaded: red dot
                markup = (
                    '<span foreground="#e64949">●</span> '
                    '<span size="small">libXrandr.so '
                    '<b>not loaded</b> in Cinnamon (v%d on disk)</span>'
                    % ondisk_ver
                )
            else:
                loaded_ver = _get_so_config_version(loaded_path)
                if is_cinnamon_fakexrandr_current():
                    # Loaded and current: green dot
                    markup = (
                        '<span foreground="#3ca64a">●</span> '
                        '<span size="small">libXrandr.so '
                        '<b>v%d loaded</b></span>'
                        % loaded_ver
                    )
                else:
                    # Loaded but stale: yellow dot
                    markup = (
                        '<span foreground="#e6a949">●</span> '
                        '<span size="small">libXrandr.so '
                        '<b>v%d loaded</b> (v%d on disk — '
                        'Reload Cinnamon)</span>'
                        % (loaded_ver, ondisk_ver)
                    )
        except Exception:
            markup = ('<span foreground="#888888">●</span> '
                      '<span size="small">libXrandr.so status unknown</span>')

        if hasattr(self, '_fxr_status_label'):
            self._fxr_status_label.set_markup(markup)

    def _refresh_fxr_status_periodic(self):
        """GLib timeout callback — returns True to keep the timer alive."""
        self._refresh_fxr_status()
        return True
