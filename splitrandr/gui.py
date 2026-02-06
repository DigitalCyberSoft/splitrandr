# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Main GUI for SplitRandR"""

import os
import optparse

import subprocess

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib

from . import widget
from . import profiles
from .auxiliary import InadequateConfiguration, ROTATIONS, Rotation
from .xrandr import Feature
from .i18n import _
from .meta import (
    __version__, TRANSLATORS, COPYRIGHT, PROGRAMNAME, PROGRAMDESCRIPTION,
)


class Application:

    AUTOSTART_SCRIPT = os.path.expanduser('~/.config/splitrandr/layout.sh')
    AUTOSTART_DESKTOP = os.path.expanduser('~/.config/autostart/splitrandr.desktop')

    def __init__(self, file=None, randr_display=None, force_version=False):
        self.window = window = Gtk.Window()
        window.props.title = _("Display")
        window.connect('delete-event', self._on_delete_event)

        self._updating_controls = False

        # Keyboard shortcuts
        accel = Gtk.AccelGroup()
        key, mod = Gtk.accelerator_parse('<Control>Return')
        accel.connect(key, mod, 0, lambda *a: self.do_apply())
        key, mod = Gtk.accelerator_parse('<Control><Shift>Return')
        accel.connect(key, mod, 0, lambda *a: self.do_apply_autostart())
        window.add_accel_group(accel)

        # Widget
        self.widget = widget.ARandRWidget(
            display=randr_display, force_version=force_version,
            window=self.window
        )
        if file is None:
            self.filetemplate = self.widget.load_from_x()
        else:
            self.filetemplate = self.widget.load_from_file(file)

        self.widget.connect('selection-changed', self._on_selection_changed)
        self.widget.connect('changed', self._on_widget_changed)

        # Size window to 80% of the monitor the pointer is on.
        display = Gdk.Display.get_default()
        seat = display.get_default_seat()
        pointer = seat.get_pointer()
        _screen, px, py = pointer.get_position()
        monitor = display.get_monitor_at_point(px, py)
        workarea = monitor.get_workarea()
        win_w = min(int(workarea.width * 0.8), 1200)
        win_h = min(int(workarea.height * 0.8), 900)

        # Set default size and max size hint so the preview widget
        # cannot force the window taller than the target.
        window.set_default_size(win_w, win_h)
        hints = Gdk.Geometry()
        hints.max_width = workarea.width
        hints.max_height = workarea.height
        window.set_geometry_hints(None, hints, Gdk.WindowHints.MAX_SIZE)

        # Single page layout (no notebook)
        main_page = self._build_page()
        window.add(main_page)
        window.show_all()

        # Center in workarea
        x = workarea.x + (workarea.width - win_w) // 2
        y = workarea.y + (workarea.height - win_h) // 2
        window.move(x, y)

        self._tray = None

        # First-run dialog
        if profiles.is_first_run():
            dialog = Gtk.MessageDialog(
                self.window, Gtk.DialogFlags.MODAL,
                Gtk.MessageType.QUESTION, Gtk.ButtonsType.YES_NO,
                _("Would you like SplitRandR to show a system tray icon "
                  "for quick profile switching?"),
            )
            dialog.set_title(_("System Tray"))
            response = dialog.run()
            dialog.destroy()
            tray_choice = 'true' if response == Gtk.ResponseType.YES else 'false'
            profiles.set_setting('tray_enabled', tray_choice)

        # Start tray if enabled
        if profiles.get_setting('tray_enabled', 'false') == 'true':
            self._start_tray()

        # Initial control state
        self._update_controls_for_selection()
        self._populate_profiles_combo()

    #################### page layout ####################

    def _build_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Monitor preview area in a scrollable frame
        preview_frame = Gtk.Frame()
        preview_frame.set_shadow_type(Gtk.ShadowType.IN)
        preview_scroll = Gtk.ScrolledWindow()
        preview_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        preview_scroll.add(self.widget)
        preview_frame.add(preview_scroll)
        page.pack_start(preview_frame, True, True, 0)

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

        load_btn = Gtk.Button(label=_("Load"))
        load_btn.connect('clicked', lambda b: self._on_load_profile())
        prof_row.pack_start(load_btn, False, False, 0)

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

        # Tray + Zoom + Layout Files row (compact)
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

        # Layout file buttons
        files_sub = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        files_sub.pack_start(Gtk.Label(label=_("Layout:")), False, False, 0)

        new_btn = Gtk.Button(label=_("New"))
        new_btn.connect('clicked', lambda b: self.do_new())
        files_sub.pack_start(new_btn, False, False, 0)

        open_btn = Gtk.Button(label=_("Open..."))
        open_btn.connect('clicked', lambda b: self.do_open())
        files_sub.pack_start(open_btn, False, False, 0)

        save_as_btn = Gtk.Button(label=_("Save As..."))
        save_as_btn.connect('clicked', lambda b: self.do_save_as())
        files_sub.pack_start(save_as_btn, False, False, 0)

        misc_row.pack_start(files_sub, False, False, 0)

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

        about_btn = Gtk.Button(label=_("About"))
        about_btn.connect('clicked', lambda b: self.about())
        action_bar.pack_start(about_btn, False, False, 0)

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

    #################### control panel sync ####################

    def _on_selection_changed(self, _widget):
        self._update_controls_for_selection()

    def _on_widget_changed(self, _widget):
        self._update_controls_for_selection()

    def _update_controls_for_selection(self):
        self._updating_controls = True
        try:
            name = self.widget.selected_output
            xrandr = self.widget._xrandr

            if name is None or name not in xrandr.configuration.outputs:
                self._output_label.set_markup(
                    "<b>" + _("No monitor selected") + "</b>"
                )
                self._controls_box.set_sensitive(False)
                self._output_label.set_sensitive(True)
                return

            self._controls_box.set_sensitive(True)
            output_cfg = xrandr.configuration.outputs[name]
            output_state = xrandr.state.outputs[name]

            # Label
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
                self._res_combo.remove_all()
                self._rate_combo.remove_all()
                self._rot_combo.remove_all()
                return

            # Primary button
            if Feature.PRIMARY in xrandr.features:
                self._primary_btn.set_sensitive(True)
                if output_cfg.primary:
                    self._primary_btn.set_label(_("Is Primary"))
                else:
                    self._primary_btn.set_label(_("Set as Primary"))
            else:
                self._primary_btn.set_sensitive(False)

            # Resolution combo
            self._res_combo.set_sensitive(True)
            self._res_combo.remove_all()
            modes_grouped = output_state.modes_by_resolution()
            # Sort resolutions by total pixels descending
            sorted_res = sorted(modes_grouped.keys(), key=lambda k: k[0] * k[1], reverse=True)
            current_res_idx = -1
            for i, (w, h) in enumerate(sorted_res):
                label = "%dx%d" % (w, h)
                self._res_combo.append(label, label)
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
            has_splits = name in xrandr.configuration.splits
            self._remove_splits_btn.set_sensitive(has_splits)

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
        res_id = self._res_combo.get_active_id()
        if not res_id:
            return

        xrandr = self.widget._xrandr
        output_state = xrandr.state.outputs[name]
        output_cfg = xrandr.configuration.outputs[name]

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
            self.widget.set_resolution(name, best_mode)
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
        rate_id = self._rate_combo.get_active_id()
        if rate_id is None:
            return

        xrandr = self.widget._xrandr
        output_state = xrandr.state.outputs[name]
        output_cfg = xrandr.configuration.outputs[name]
        current_w, current_h = output_cfg.mode.width, output_cfg.mode.height

        modes_grouped = output_state.modes_by_resolution()
        modes_for_res = modes_grouped.get((current_w, current_h), [])

        idx = int(rate_id)
        if 0 <= idx < len(modes_for_res):
            mode = modes_for_res[idx]
            try:
                self.widget.set_resolution(name, mode)
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
        rot_id = self._rot_combo.get_active_id()
        if not rot_id:
            return
        try:
            self.widget.set_rotation(name, Rotation(rot_id))
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
        active = self._active_switch.get_active()
        try:
            self.widget.set_active(name, active)
        except InadequateConfiguration as exc:
            self.widget.error_message(str(exc))

    def _on_primary_clicked(self):
        name = self.widget.selected_output
        if not name:
            return
        output_cfg = self.widget._xrandr.configuration.outputs[name]
        self.widget.set_primary(name, not output_cfg.primary)

    def _on_split_clicked(self):
        name = self.widget.selected_output
        if not name:
            return
        self.widget._on_split_monitor(None, name)

    def _on_remove_splits_clicked(self):
        name = self.widget.selected_output
        if not name:
            return
        self.widget._on_remove_splits(None, name)

    def _on_detect_displays(self):
        self.filetemplate = self.widget.load_from_x()

    def _on_reset_defaults(self):
        self.filetemplate = self.widget.load_from_x()

    #################### apply / revert ####################

    def _capture_revert_script(self):
        """Capture the current X state (not in-memory config) as a revert script."""
        from .xrandr import XRandR
        snap = XRandR(force_version=True)
        snap.load_from_x()
        return snap.save_to_shellscript_string()

    def _confirm_or_revert(self, revert_script):
        """Show a GNOME-style confirmation countdown dialog.

        Returns True if the user kept changes, False if reverted.
        """
        COUNTDOWN = 30
        state = {'remaining': COUNTDOWN, 'timer_id': None}

        dialog = Gtk.MessageDialog(
            transient_for=self.window,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            text=_("Does the display look OK?"),
        )
        dialog.format_secondary_text(
            _("Reverting in %d seconds\u2026") % state['remaining']
        )
        dialog.add_button(_("Revert Settings"), Gtk.ResponseType.REJECT)
        dialog.add_button(_("Keep Changes"), Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)

        secondary_label = dialog.get_message_area().get_children()[1]

        def tick():
            state['remaining'] -= 1
            if state['remaining'] <= 0:
                dialog.response(Gtk.ResponseType.REJECT)
                return False
            secondary_label.set_text(
                _("Reverting in %d seconds\u2026") % state['remaining']
            )
            return True

        state['timer_id'] = GLib.timeout_add_seconds(1, tick)

        response = dialog.run()
        GLib.source_remove(state['timer_id'])
        dialog.destroy()

        import logging
        log = logging.getLogger('splitrandr')
        if response != Gtk.ResponseType.ACCEPT:
            log.info("REVERTING: running revert script")
            log.info("revert script:\n%s", revert_script)
            subprocess.Popen(['sh', '-c', revert_script])
            self.widget.load_from_x()
            return False
        log.info("KEEPING changes")
        return True

    def do_apply(self):
        if self.widget.abort_if_unsafe():
            return

        revert_script = self._capture_revert_script()

        try:
            self.widget.save_to_x()
        except Exception as exc:
            dialog = Gtk.MessageDialog(
                None, Gtk.DialogFlags.MODAL, Gtk.MessageType.ERROR,
                Gtk.ButtonsType.OK, _("XRandR failed:\n%s") % exc
            )
            dialog.run()
            dialog.destroy()
            return

        self._confirm_or_revert(revert_script)

    def do_apply_autostart(self):
        if self.widget.abort_if_unsafe():
            return

        revert_script = self._capture_revert_script()

        try:
            self.widget.save_to_x()
        except Exception as exc:
            dialog = Gtk.MessageDialog(
                None, Gtk.DialogFlags.MODAL, Gtk.MessageType.ERROR,
                Gtk.ButtonsType.OK, _("XRandR failed:\n%s") % exc
            )
            dialog.run()
            dialog.destroy()
            return

        if not self._confirm_or_revert(revert_script):
            return

        script_dir = os.path.dirname(self.AUTOSTART_SCRIPT)
        os.makedirs(script_dir, exist_ok=True)
        self.widget.save_to_file(self.AUTOSTART_SCRIPT, self.filetemplate)

        active = profiles.get_active_profile()
        if active:
            script = "\n".join(self.filetemplate)
            profiles.save_profile(active, script)

        autostart_dir = os.path.dirname(self.AUTOSTART_DESKTOP)
        os.makedirs(autostart_dir, exist_ok=True)

        desktop_entry = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=SplitRandR Layout\n"
            "Comment=Restore monitor layout and virtual splits\n"
            "Exec=%s\n"
            "X-GNOME-Autostart-enabled=true\n"
        ) % self.AUTOSTART_SCRIPT

        with open(self.AUTOSTART_DESKTOP, 'w') as f:
            f.write(desktop_entry)

        dialog = Gtk.MessageDialog(
            None, Gtk.DialogFlags.MODAL, Gtk.MessageType.INFO,
            Gtk.ButtonsType.OK,
            _("Layout applied and saved for autostart.\n\n"
              "Script: %s\n"
              "Autostart: %s") % (self.AUTOSTART_SCRIPT, self.AUTOSTART_DESKTOP)
        )
        dialog.run()
        dialog.destroy()

    #################### layout file operations ####################

    def do_new(self):
        self.filetemplate = self.widget.load_from_x()

    def do_open(self):
        dialog = self._new_file_dialog(
            _("Open Layout"), Gtk.FileChooserAction.OPEN, _("Open")
        )

        result = dialog.run()
        filenames = dialog.get_filenames()
        dialog.destroy()
        if result == Gtk.ResponseType.ACCEPT:
            assert len(filenames) == 1
            filename = filenames[0]
            self.filetemplate = self.widget.load_from_file(filename)

    def do_save_as(self):
        dialog = self._new_file_dialog(
            _("Save Layout"), Gtk.FileChooserAction.SAVE, _("Save")
        )
        dialog.props.do_overwrite_confirmation = True

        result = dialog.run()
        filenames = dialog.get_filenames()
        dialog.destroy()
        if result == Gtk.ResponseType.ACCEPT:
            assert len(filenames) == 1
            filename = filenames[0]
            if not filename.endswith('.sh'):
                filename = filename + '.sh'
            self.widget.save_to_file(filename, self.filetemplate)

    def _new_file_dialog(self, title, dialog_type, buttonlabel):
        dialog = Gtk.FileChooserDialog(title, None, dialog_type)
        dialog.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        dialog.add_button(buttonlabel, Gtk.ResponseType.ACCEPT)

        layoutdir = os.path.expanduser('~/.screenlayout/')
        try:
            os.makedirs(layoutdir)
        except OSError:
            pass
        dialog.set_current_folder(layoutdir)

        file_filter = Gtk.FileFilter()
        file_filter.set_name('Shell script (Layout file)')
        file_filter.add_pattern('*.sh')
        dialog.add_filter(file_filter)

        return dialog

    #################### profiles & tray ####################

    def _populate_profiles_combo(self):
        self._updating_controls = True
        try:
            self._profile_combo.remove_all()
            names = profiles.list_profiles()
            active = profiles.get_active_profile()
            active_idx = -1
            for i, name in enumerate(names):
                self._profile_combo.append(name, name)
                if name == active:
                    active_idx = i
            if active_idx >= 0:
                self._profile_combo.set_active(active_idx)
        finally:
            self._updating_controls = False

    def _on_profile_combo_changed(self):
        pass  # selection only - Load button triggers action

    def _on_load_profile(self):
        name = self._profile_combo.get_active_id()
        if name:
            self._do_load_profile(name)

    def _on_delete_selected_profile(self):
        name = self._profile_combo.get_active_id()
        if name:
            self._do_delete_profile(name)

    def do_save_profile(self):
        dialog = Gtk.Dialog(
            _("Save Profile"), self.window, Gtk.DialogFlags.MODAL,
            (_("Cancel"), Gtk.ResponseType.CANCEL,
             _("Save"), Gtk.ResponseType.ACCEPT),
        )
        box = dialog.get_content_area()
        label = Gtk.Label(label=_("Profile name:"))
        box.pack_start(label, False, False, 4)
        entry = Gtk.Entry()
        active = profiles.get_active_profile()
        if active:
            entry.set_text(active)
        box.pack_start(entry, False, False, 4)
        box.show_all()

        if dialog.run() == Gtk.ResponseType.ACCEPT:
            name = entry.get_text().strip()
            if name:
                script = "\n".join(self.filetemplate)
                profiles.save_profile(name, script)
                profiles.set_active_profile(name)
                self._populate_profiles_combo()
                self._notify_tray()
        dialog.destroy()

    def _do_load_profile(self, name):
        path = profiles.profile_path(name)
        if os.path.exists(path):
            self.filetemplate = self.widget.load_from_file(path)
            profiles.set_active_profile(name)
            self._populate_profiles_combo()
            self._notify_tray()

    def _do_delete_profile(self, name):
        dialog = Gtk.MessageDialog(
            self.window, Gtk.DialogFlags.MODAL,
            Gtk.MessageType.QUESTION, Gtk.ButtonsType.YES_NO,
            _("Delete profile '%s'?") % name,
        )
        if dialog.run() == Gtk.ResponseType.YES:
            profiles.delete_profile(name)
            self._populate_profiles_combo()
            self._notify_tray()
        dialog.destroy()

    def _start_tray(self):
        from .tray import SplitRandRTray
        self._tray = SplitRandRTray(app=self)

    def _stop_tray(self):
        if self._tray:
            self._tray.destroy()
            self._tray = None

    def _on_tray_toggled_switch(self):
        if self._tray_switch.get_active():
            profiles.set_setting('tray_enabled', 'true')
            if not self._tray:
                self._start_tray()
        else:
            profiles.set_setting('tray_enabled', 'false')
            self._stop_tray()

    def _notify_tray(self):
        if self._tray:
            self._tray.refresh_menu()

    #################### zoom ####################

    def _on_zoom_toggled(self, button, value):
        if button.get_active():
            self.widget.factor = value

    #################### window management ####################

    def _on_delete_event(self, _window, _event):
        if self._tray:
            self.window.hide()
            return True
        Gtk.main_quit()
        return False

    def _do_quit(self, *_args):
        Gtk.main_quit()

    #################### about ####################

    def about(self):
        dialog = Gtk.AboutDialog()
        dialog.props.program_name = PROGRAMNAME
        dialog.props.version = __version__
        dialog.props.translator_credits = "\n".join(TRANSLATORS) if TRANSLATORS else ""
        dialog.props.copyright = COPYRIGHT
        dialog.props.comments = PROGRAMDESCRIPTION
        licensetext = open(os.path.join(os.path.dirname(
            __file__), 'data', 'gpl-3.txt')).read()
        dialog.props.license = licensetext.replace(
            '<', u'\u2329 ').replace('>', u' \u232a')
        dialog.props.logo_icon_name = 'video-display'
        dialog.run()
        dialog.destroy()

    def run(self):
        Gtk.main()


def main():
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(name)s: %(message)s',
    )

    parser = optparse.OptionParser(
        usage="%prog [savedfile]",
        description="Monitor Layout Editor with Virtual Monitor Splitting",
        version="%%prog %s" % __version__
    )
    parser.add_option(
        '--randr-display',
        help=(
            'Use D as display for xrandr '
            '(but still show the GUI on the display from the environment; '
            'e.g. `localhost:10.0`)'
        ),
        metavar='D'
    )
    parser.add_option(
        '--force-version',
        help='Even run with untested XRandR versions',
        action='store_true'
    )

    (options, args) = parser.parse_args()
    if not args:
        file_to_open = None
    elif len(args) == 1:
        file_to_open = args[0]
    else:
        parser.usage()

    app = Application(
        file=file_to_open,
        randr_display=options.randr_display,
        force_version=options.force_version
    )
    app.run()


if __name__ == '__main__':
    main()
