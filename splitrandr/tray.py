# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""System tray indicator for SplitRandR using AppIndicator3."""

import subprocess

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('AppIndicator3', '0.1')
from gi.repository import Gtk, GLib, AppIndicator3

from . import profiles
from .i18n import _


class SplitRandRTray:
    def __init__(self, app=None):
        self.app = app
        self.indicator = AppIndicator3.Indicator.new(
            'splitrandr',
            'video-display',
            AppIndicator3.IndicatorCategory.HARDWARE,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self._build_menu()

    def _build_menu(self):
        menu = Gtk.Menu()
        active = profiles.get_active_profile()
        names = profiles.list_profiles()

        group = []
        for name in names:
            item = Gtk.CheckMenuItem(label=name)
            item.set_draw_as_radio(True)
            if name == active:
                item.set_active(True)
            item.connect('toggled', self._on_profile_toggled, name)
            if group:
                item.join_group(group[0])
            group.append(item)
            menu.append(item)

        if names:
            menu.append(Gtk.SeparatorMenuItem())

        open_item = Gtk.MenuItem(label=_("Open Editor"))
        open_item.connect('activate', self._on_open_editor)
        menu.append(open_item)

        quit_item = Gtk.MenuItem(label=_("Quit"))
        quit_item.connect('activate', self._on_quit)
        menu.append(quit_item)

        menu.show_all()
        self.indicator.set_menu(menu)

    def refresh_menu(self):
        self._build_menu()

    def _confirm_or_revert(self, revert_script, previous_active):
        """Show a GNOME-style confirmation countdown dialog.

        Returns True if the user kept changes, False if reverted.
        """
        COUNTDOWN = 30
        state = {'remaining': COUNTDOWN, 'timer_id': None}

        dialog = Gtk.MessageDialog(
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

        if response != Gtk.ResponseType.ACCEPT:
            subprocess.Popen(['sh', '-c', revert_script])
            profiles.set_active_profile(previous_active)
            return False
        return True

    def _on_profile_toggled(self, item, name):
        if item.get_active():
            previous_active = profiles.get_active_profile()
            revert_script = None
            if self.app and hasattr(self.app, 'widget'):
                revert_script = self.app.widget._xrandr.save_to_shellscript_string()
            profiles.apply_profile(name)
            if revert_script is not None:
                if not self._confirm_or_revert(revert_script, previous_active):
                    self._build_menu()
                    return
            self._build_menu()

    def _on_open_editor(self, _item):
        if self.app and hasattr(self.app, 'window'):
            self.app.window.present()
        else:
            import subprocess
            subprocess.Popen(['splitrandr'])

    def _on_quit(self, _item):
        Gtk.main_quit()

    def destroy(self):
        self.indicator.set_status(AppIndicator3.IndicatorStatus.PASSIVE)
