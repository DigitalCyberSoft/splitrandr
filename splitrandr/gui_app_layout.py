# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Mixin: build the main GTK window chrome and layout for ``Application``.

GNOME 3 HIG structure:

- :meth:`_build_headerbar` — CSD header bar with the profile menu button
  (left), the suggested-action Apply button and the hamburger menu
  (right). All secondary actions (Apply & Set Login Layout, Detect
  Displays, Reload Cinnamon, Reset to Defaults, tray toggle, About,
  Quit) live in the hamburger popover.
- :meth:`_build_page` — a problems-only status InfoBar, the large
  editable Proposed pane (fit-to-window), a thumbnail strip with the
  read-only Current (virtual) and Real (physical) panes, and a
  width-capped Settings-style listbox of per-monitor controls.

All widgets are stored on ``self`` and consumed by the other
``Application`` mixins; the ``self._*`` control attribute names are a
stable interface for ``gui_app_controls``.
"""

import logging

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib

from . import profiles
from .i18n import _

log = logging.getLogger('splitrandr')

# Response id for the InfoBar's Reload Cinnamon action button.
_RESPONSE_RELOAD = 1


class ApplicationLayoutMixin:

    #################### header bar ####################

    def _build_headerbar(self):
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.set_title(_("SplitRandR"))

        # Left: profile selector (document-selector pattern; switching a
        # profile loads it into the Proposed pane).
        self._profile_button = Gtk.MenuButton()
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._profile_button_label = Gtk.Label(label=_("Profiles"))
        btn_box.pack_start(self._profile_button_label, False, False, 0)
        btn_box.pack_start(
            Gtk.Image.new_from_icon_name('pan-down-symbolic',
                                         Gtk.IconSize.BUTTON),
            False, False, 0)
        self._profile_button.add(btn_box)

        self._profile_popover = Gtk.Popover()
        pop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        pop_box.set_margin_top(6)
        pop_box.set_margin_bottom(6)
        pop_box.set_margin_start(6)
        pop_box.set_margin_end(6)

        self._profile_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        pop_box.pack_start(self._profile_list_box, False, False, 0)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.set_margin_top(4)
        sep.set_margin_bottom(4)
        pop_box.pack_start(sep, False, False, 0)

        save_item = Gtk.ModelButton(text=_("Save Current As…"))
        save_item.connect(
            'clicked',
            lambda *_a: (self._profile_popover.popdown(),
                         self.do_save_profile()))
        pop_box.pack_start(save_item, False, False, 0)

        pop_box.show_all()
        self._profile_popover.add(pop_box)
        self._profile_button.set_popover(self._profile_popover)
        # Rebuild the list every time the popover opens so profile
        # changes made from the tray menu are picked up.
        self._profile_button.connect(
            'toggled',
            lambda b: self._refresh_profile_ui() if b.get_active() else None)
        hb.pack_start(self._profile_button)

        # Right: hamburger (packed first so it sits rightmost), then Apply.
        self._menu_button = Gtk.MenuButton()
        self._menu_button.add(Gtk.Image.new_from_icon_name(
            'open-menu-symbolic', Gtk.IconSize.BUTTON))
        self._menu_popover = self._build_menu_popover()
        self._menu_button.set_popover(self._menu_popover)
        hb.pack_end(self._menu_button)

        self._apply_btn = Gtk.Button(label=_("Apply"))
        self._apply_btn.get_style_context().add_class('suggested-action')
        self._apply_btn.connect('clicked', lambda b: self.do_apply())
        hb.pack_end(self._apply_btn)

        return hb

    def _build_menu_popover(self):
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        def item(text, callback):
            b = Gtk.ModelButton(text=text)
            # Pop down first so modal dialogs opened by the callback
            # don't fight the popover's grab.
            b.connect('clicked',
                      lambda *_a: (popover.popdown(), callback()))
            box.pack_start(b, False, False, 0)
            return b

        def separator():
            s = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            s.set_margin_top(4)
            s.set_margin_bottom(4)
            box.pack_start(s, False, False, 0)

        item(_("Apply & Set Login Layout"), self.do_apply_autostart)
        separator()
        item(_("Detect Displays"), self._on_detect_displays)
        item(_("Reload Cinnamon"), self._reload_cinnamon_ui)
        item(_("Reset to Defaults"), self._on_reset_defaults)
        separator()

        self._tray_check = Gtk.ModelButton(
            text=_("Show Tray Icon"), role=Gtk.ButtonRole.CHECK)
        self._tray_check.props.active = (
            profiles.get_setting('tray_enabled', 'true') == 'true')
        self._tray_check.connect(
            'clicked',
            lambda *_a: (popover.popdown(), self._on_tray_menu_toggled()))
        box.pack_start(self._tray_check, False, False, 0)

        separator()
        item(_("About SplitRandR"), self.about)
        item(_("Quit"), self._do_quit)

        box.show_all()
        popover.add(box)
        return popover

    #################### page layout ####################

    def _build_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        page.pack_start(self._build_status_infobar(), False, False, 0)

        def _dim_heading(text):
            lbl = Gtk.Label(label=text)
            lbl.set_halign(Gtk.Align.START)
            lbl.get_style_context().add_class('dim-label')
            return lbl

        # Proposed (editable) pane — expands, canvas fits its slot.
        proposed_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        proposed_box.set_margin_start(18)
        proposed_box.set_margin_end(18)
        proposed_box.set_margin_top(12)
        proposed_box.pack_start(_dim_heading(_("Proposed")), False, False, 0)

        self._proposed_slot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        # Reserve real estate for the editable canvas: without a floor,
        # the fixed-height children below define the window minimum and
        # the expanding slot is left with scraps.
        self._proposed_slot.set_size_request(-1, 320)
        self.widget.set_halign(Gtk.Align.CENTER)
        self.widget.set_valign(Gtk.Align.CENTER)
        self._proposed_slot.pack_start(self.widget, True, True, 0)
        self._last_slot_size = (0, 0)
        self._proposed_slot.connect('size-allocate',
                                    self._on_proposed_slot_allocated)
        proposed_box.pack_start(self._proposed_slot, True, True, 0)

        # Split actions live directly under the editable canvas — one
        # button per physical monitor, rebuilt to match the active
        # outputs (see _rebuild_split_buttons). Each names its own
        # monitor and, when that monitor already has splits, carries a
        # linked clear button.
        self._split_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._split_row.set_halign(Gtk.Align.CENTER)
        self._split_row.set_margin_top(6)
        self._split_buttons_sig = None
        proposed_box.pack_start(self._split_row, False, False, 0)

        page.pack_start(proposed_box, True, True, 0)

        # Thumbnail strip: Current (virtual) and Real (physical).
        thumbs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=36)
        thumbs.set_halign(Gtk.Align.CENTER)
        thumbs.set_margin_top(12)
        thumbs.set_margin_bottom(12)

        def _thumb(label_text, pane_widget):
            b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            b.pack_start(_dim_heading(label_text), False, False, 0)
            pane_widget.set_halign(Gtk.Align.CENTER)
            b.pack_start(pane_widget, False, False, 0)
            return b

        thumbs.pack_start(
            _thumb(_("Current (virtual)"), self.current_widget),
            False, False, 0)
        thumbs.pack_start(
            _thumb(_("Real (physical)"), self.original_widget),
            False, False, 0)
        page.pack_start(thumbs, False, False, 0)

        page.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
            False, False, 0)

        # Per-monitor controls — width-capped Settings-style rows.
        self._controls_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._controls_box.set_halign(Gtk.Align.CENTER)
        self._controls_box.set_size_request(620, -1)
        self._controls_box.set_margin_top(12)
        self._controls_box.set_margin_bottom(12)

        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._output_label = Gtk.Label()
        self._output_label.set_markup(
            "<b>" + _("No monitor selected") + "</b>")
        self._output_label.set_halign(Gtk.Align.START)
        header_row.pack_start(self._output_label, True, True, 0)

        active_label = Gtk.Label(label=_("Active"))
        header_row.pack_start(active_label, False, False, 0)
        self._active_switch = Gtk.Switch()
        self._active_switch.set_valign(Gtk.Align.CENTER)
        self._active_switch.connect(
            'notify::active', lambda s, p: self._on_active_toggled())
        header_row.pack_start(self._active_switch, False, False, 0)
        self._controls_box.pack_start(header_row, False, False, 0)

        frame = Gtk.Frame()
        rows = Gtk.ListBox()
        rows.set_selection_mode(Gtk.SelectionMode.NONE)
        rows.set_header_func(
            lambda row, before:
                row.set_header(Gtk.Separator() if before else None))

        def _row(label_text, control):
            r = Gtk.ListBoxRow()
            r.set_activatable(False)
            h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            h.set_margin_top(8)
            h.set_margin_bottom(8)
            h.set_margin_start(12)
            h.set_margin_end(12)
            lbl = Gtk.Label(label=label_text)
            lbl.set_halign(Gtk.Align.START)
            h.pack_start(lbl, True, True, 0)
            h.pack_end(control, False, False, 0)
            r.add(h)
            rows.add(r)
            return lbl

        self._res_combo = Gtk.ComboBoxText()
        self._res_combo.connect(
            'changed', lambda c: self._on_resolution_changed())
        _row(_("Resolution"), self._res_combo)

        self._rate_combo = Gtk.ComboBoxText()
        self._rate_combo.connect(
            'changed', lambda c: self._on_refresh_changed())
        _row(_("Refresh Rate"), self._rate_combo)

        self._rot_combo = Gtk.ComboBoxText()
        self._rot_combo.connect(
            'changed', lambda c: self._on_rotation_changed())
        _row(_("Rotation"), self._rot_combo)

        self._primary_btn = Gtk.Button(label=_("Set as Primary"))
        self._primary_btn.connect(
            'clicked', lambda b: self._on_primary_clicked())
        _row(_("Primary"), self._primary_btn)

        border_ctrl = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        adj = Gtk.Adjustment(value=0, lower=0, upper=50,
                             step_increment=1, page_increment=5)
        self._border_spin = Gtk.SpinButton(
            adjustment=adj, climb_rate=1, digits=0)
        self._border_spin.set_width_chars(3)
        self._border_spin.connect(
            'value-changed', lambda s: self._on_border_changed())
        border_ctrl.pack_start(self._border_spin, False, False, 0)
        self._border_px_label = Gtk.Label(label=_("px"))
        border_ctrl.pack_start(self._border_px_label, False, False, 0)
        self._border_label = _row(_("Split Border"), border_ctrl)

        frame.add(rows)
        self._controls_box.pack_start(frame, False, False, 0)

        page.pack_start(self._controls_box, False, False, 0)

        self._refresh_fxr_status()
        GLib.timeout_add_seconds(3, self._refresh_fxr_status_periodic)

        return page

    def _on_proposed_slot_allocated(self, _slot, alloc):
        # Guard against allocate feedback: only refit when the slot's
        # size actually changed.
        size = (alloc.width, alloc.height)
        if size == self._last_slot_size:
            return
        self._last_slot_size = size
        # Defer the refit out of the allocation pass: a size-request
        # change made inside size-allocate is coalesced away and the
        # widget never gets re-allocated until an unrelated resize.
        GLib.idle_add(self._refit_proposed, size)

    def _rebuild_split_buttons(self):
        """Repopulate the per-monitor split-button row to match the
        active physical outputs. Rebuilt only when the set of active
        outputs (or which of them have splits) changes, so the frequent
        selection-driven calls don't churn the buttons."""
        cfg = self.widget._xrandr.configuration
        active = [n for n in self.widget._xrandr.outputs
                  if n in cfg.outputs and cfg.outputs[n].active]
        sig = tuple((n, n in cfg.splits) for n in active)
        if sig == self._split_buttons_sig:
            return
        self._split_buttons_sig = sig

        for child in self._split_row.get_children():
            self._split_row.remove(child)

        if not active:
            self._split_row.hide()
            return

        for name in active:
            group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            group.get_style_context().add_class('linked')

            split_btn = Gtk.Button(label=_("Split %s…") % name)
            split_btn.connect(
                'clicked',
                lambda _b, n=name: self.widget._on_split_monitor(None, n))
            group.pack_start(split_btn, False, False, 0)

            if name in cfg.splits:
                clear_btn = Gtk.Button.new_from_icon_name(
                    'edit-clear-symbolic', Gtk.IconSize.BUTTON)
                clear_btn.set_tooltip_text(_("Remove splits from %s") % name)
                clear_btn.connect(
                    'clicked',
                    lambda _b, n=name: self.widget._on_remove_splits(None, n))
                group.pack_start(clear_btn, False, False, 0)

            self._split_row.pack_start(group, False, False, 0)

        self._split_row.show_all()

    def _refit_proposed(self, size):
        self.widget.set_fit_size(max(size[0] - 8, 1), max(size[1] - 8, 1))
        # Thumbnails scale gently with the window instead of staying
        # pinned at 150px on tall displays.
        win_h = self.window.get_allocated_height()
        thumb_h = max(150, min(int(win_h * 0.17), 320))
        self.current_widget.set_fit_height(thumb_h)
        self.original_widget.set_fit_height(thumb_h)
        return False

    #################### profile popover contents ####################

    def _refresh_profile_ui(self):
        """Rebuild the profile popover list and the header button label.

        ``self._shown_profile`` is the profile whose contents the
        Proposed pane currently reflects (None on fresh startup, where
        the pane holds live X state rather than any profile).
        """
        for child in self._profile_list_box.get_children():
            self._profile_list_box.remove(child)

        names = profiles.list_profiles()
        shown = self._shown_profile

        if not names:
            placeholder = Gtk.ModelButton(text=_("No profiles saved"))
            placeholder.set_sensitive(False)
            self._profile_list_box.pack_start(placeholder, False, False, 0)
        for name in names:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            select = Gtk.ModelButton(
                text=name, role=Gtk.ButtonRole.RADIO,
                active=(name == shown))
            select.set_hexpand(True)
            select.connect(
                'clicked',
                lambda _b, n=name: (self._profile_popover.popdown(),
                                    self._do_load_profile(n)))
            row.pack_start(select, True, True, 0)

            delete = Gtk.Button.new_from_icon_name(
                'user-trash-symbolic', Gtk.IconSize.MENU)
            delete.set_relief(Gtk.ReliefStyle.NONE)
            delete.set_tooltip_text(_("Delete this profile"))
            delete.connect(
                'clicked',
                lambda _b, n=name: (self._profile_popover.popdown(),
                                    self._do_delete_profile(n)))
            row.pack_start(delete, False, False, 0)
            self._profile_list_box.pack_start(row, False, False, 0)

        self._profile_list_box.show_all()
        self._profile_button_label.set_text(shown if shown else _("Profiles"))

    #################### libXrandr.so status infobar ####################

    def _build_status_infobar(self):
        ib = Gtk.InfoBar()
        # Hidden by default; window.show_all() must not reveal it.
        ib.set_no_show_all(True)

        self._status_label = Gtk.Label()
        self._status_label.set_line_wrap(True)
        self._status_label.set_xalign(0)
        content = ib.get_content_area()
        content.add(self._status_label)
        content.show_all()

        ib.add_button(_("Reload Cinnamon"), _RESPONSE_RELOAD)
        ib.get_action_area().show_all()
        ib.connect('response', self._on_infobar_response)

        self._status_infobar = ib
        return ib

    def _on_infobar_response(self, infobar, response):
        if response == _RESPONSE_RELOAD:
            infobar.hide()
            self._reload_cinnamon_ui()

    def _refresh_fxr_status(self):
        """Poll libXrandr.so state and drive the problems-only InfoBar."""
        from .fakexrandr_config import (
            is_cinnamon_fakexrandr_current,
            _get_cinnamon_fakexrandr_path,
            _get_so_config_version,
            _find_fakexrandr_lib,
        )

        state = 'ok'
        message = ''
        # Boundary containment: this runs on a 3s UI timer and the
        # helpers probe /proc, which can race process exits. A failed
        # poll must not take down the GUI; it reports as 'unknown' and
        # keeps the bar hidden rather than nagging on noise.
        try:
            loaded_path = _get_cinnamon_fakexrandr_path()
            ondisk_path = _find_fakexrandr_lib()
            ondisk_ver = _get_so_config_version(ondisk_path) if ondisk_path else 0
            if not loaded_path:
                state = 'missing'
                message = _(
                    "libXrandr.so is not loaded in Cinnamon — splits are "
                    "invisible to the window manager (v%d on disk).") % ondisk_ver
            elif not is_cinnamon_fakexrandr_current():
                loaded_ver = _get_so_config_version(loaded_path)
                state = 'stale'
                message = _(
                    "libXrandr v%d is loaded in Cinnamon, but v%d is on "
                    "disk.") % (loaded_ver, ondisk_ver)
        except Exception as exc:
            log.debug("libXrandr status poll failed: %s", exc)
            state = 'unknown'

        self._update_status_infobar(state, message)

    def _update_status_infobar(self, state, message):
        suppressed = self._apply_in_flight or self._reload_in_flight
        if state in ('ok', 'unknown') or suppressed:
            self._fxr_bad_streak = 0
            self._status_infobar.hide()
            return

        # Debounce: require two consecutive bad polls (~6s) so routine
        # restarts that slip past the in-flight flags don't flash the bar.
        self._fxr_bad_streak += 1
        if self._fxr_bad_streak < 2:
            return

        self._status_infobar.set_message_type(
            Gtk.MessageType.ERROR if state == 'missing'
            else Gtk.MessageType.WARNING)
        self._status_label.set_text(message)
        self._status_infobar.show()

    def _refresh_fxr_status_periodic(self):
        """GLib timeout callback — returns True to keep the timer alive."""
        self._refresh_fxr_status()
        return True
