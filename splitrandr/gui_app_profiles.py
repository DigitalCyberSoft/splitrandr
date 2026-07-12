# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Mixin: profile load/save/delete, tray helpers, and top-level window
plumbing (delete-event, quit, About) for ``Application``.

The profile popover UI itself is built in ``gui_app_layout``
(:meth:`_refresh_profile_ui`); this mixin owns the actions behind it.
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

    def do_save_profile(self):
        dialog = Gtk.Dialog(
            title=_("Save Profile"), transient_for=self.window,
            modal=True, use_header_bar=1,
        )
        dialog.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        save_btn = dialog.add_button(_("Save"), Gtk.ResponseType.ACCEPT)
        save_btn.get_style_context().add_class('suggested-action')
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)

        box = dialog.get_content_area()
        box.set_spacing(6)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        label = Gtk.Label(label=_("Profile name:"), halign=Gtk.Align.START)
        box.pack_start(label, False, False, 0)
        entry = Gtk.Entry()
        entry.set_activates_default(True)
        active = profiles.get_active_profile()
        if active:
            entry.set_text(active)
        box.pack_start(entry, False, False, 0)
        box.show_all()

        if dialog.run() == Gtk.ResponseType.ACCEPT:
            name = entry.get_text().strip()
            if name:
                profiles.save_profile(name,
                                      self.widget._xrandr.configuration.to_dict())
                profiles.set_active_profile(name)
                self._shown_profile = name
                self._refresh_profile_ui()
                self._notify_tray()
        dialog.destroy()

    def _do_load_profile(self, name):
        path = profiles.profile_path(name)
        if os.path.exists(path):
            self.widget._xrandr.load_from_json(path)
            self.widget._xrandr_was_reloaded()
            profiles.set_active_profile(name)
            self._shown_profile = name
            self._refresh_profile_ui()
            self._notify_tray()

    def _do_delete_profile(self, name):
        dialog = Gtk.MessageDialog(
            self.window, Gtk.DialogFlags.MODAL,
            Gtk.MessageType.QUESTION, Gtk.ButtonsType.YES_NO,
            _("Delete profile '%s'?") % name,
        )
        if dialog.run() == Gtk.ResponseType.YES:
            profiles.delete_profile(name)
            if self._shown_profile == name:
                self._shown_profile = None
            self._refresh_profile_ui()
            self._notify_tray()
        dialog.destroy()

    def _start_tray(self):
        from .tray import SplitRandRTray
        self._tray = SplitRandRTray(app=self)

    def _stop_tray(self):
        if self._tray:
            self._tray.destroy()
            self._tray = None

    def _on_tray_menu_toggled(self):
        enabled = not (
            profiles.get_setting('tray_enabled', 'true') == 'true')
        profiles.set_setting('tray_enabled', 'true' if enabled else 'false')
        self._tray_check.props.active = enabled
        if enabled:
            if not self._tray:
                self._start_tray()
        else:
            self._stop_tray()

    def _notify_tray(self):
        if self._tray:
            self._tray.refresh_menu()

    #################### window management ####################

    def _on_delete_event(self, _window, _event):
        if self._tray:
            self.window.hide()
            return True
        self._do_quit()
        return False

    def _do_quit(self, *_args):
        if hasattr(self, '_screen_watcher'):
            self._screen_watcher.destroy()
        Gtk.main_quit()

    #################### about ####################

    def _fxr_version_line(self):
        """One-line libXrandr.so status for the About dialog. The
        version display moved here from the old always-on status dot."""
        from .fakexrandr_config import (
            _get_cinnamon_fakexrandr_path,
            _get_so_config_version,
            _find_fakexrandr_lib,
        )
        # Same /proc-probing helpers as the status poll; a failure here
        # must not block the About dialog.
        try:
            loaded_path = _get_cinnamon_fakexrandr_path()
            ondisk_path = _find_fakexrandr_lib()
            ondisk_ver = _get_so_config_version(ondisk_path) if ondisk_path else 0
            if loaded_path:
                loaded_ver = _get_so_config_version(loaded_path)
                return _("libXrandr shim: v%d loaded, v%d on disk") % (
                    loaded_ver, ondisk_ver)
            return _("libXrandr shim: not loaded (v%d on disk)") % ondisk_ver
        except Exception:
            return _("libXrandr shim: status unknown")

    def about(self):
        dialog = Gtk.AboutDialog()
        dialog.set_transient_for(self.window)
        dialog.props.program_name = PROGRAMNAME
        dialog.props.version = __version__
        dialog.props.translator_credits = "\n".join(TRANSLATORS) if TRANSLATORS else ""
        dialog.props.copyright = COPYRIGHT
        dialog.props.comments = "%s\n\n%s" % (
            PROGRAMDESCRIPTION, self._fxr_version_line())
        dialog.props.logo_icon_name = 'video-display'
        licensetext = open(os.path.join(os.path.dirname(
            __file__), 'data', 'gpl-3.txt')).read()
        dialog.props.license = licensetext.replace(
            '<', u'〈 ').replace('>', u' 〉')
        dialog.run()
        dialog.destroy()

    def run(self):
        Gtk.main()
