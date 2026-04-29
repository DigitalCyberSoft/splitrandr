# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Mixin: profile combo box, save/load/delete dialogs, tray helpers, and
top-level window plumbing (delete-event, quit, About) for ``Application``.
"""

import os

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk

from . import profiles
from .i18n import _
from .meta import (
    __version__, TRANSLATORS, COPYRIGHT, PROGRAMNAME, PROGRAMDESCRIPTION,
)


class ApplicationProfilesMixin:

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
        if self._updating_controls:
            return
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
                profiles.save_profile(name,
                                      self.widget._xrandr.configuration.to_dict())
                profiles.set_active_profile(name)
                self._populate_profiles_combo()
                self._notify_tray()
        dialog.destroy()

    def _do_load_profile(self, name):
        path = profiles.profile_path(name)
        if os.path.exists(path):
            self.widget._xrandr.load_from_json(path)
            self.widget._xrandr_was_reloaded()
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

    #################### window management ####################

    def _on_delete_event(self, _window, _event):
        if self._tray:
            self.window.hide()
            return True
        Gtk.main_quit()
        return False

    def _do_quit(self, *_args):
        if hasattr(self, '_screen_watcher'):
            self._screen_watcher.destroy()
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
            '<', u'〈 ').replace('>', u' 〉')
        dialog.props.logo_icon_name = 'video-display'
        dialog.run()
        dialog.destroy()

    def run(self):
        Gtk.main()
