# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""System tray icon for SplitRandR.

Tries XApp.StatusIcon (Cinnamon-native), then Gtk.StatusIcon,
then AppIndicator3 as fallback.
"""

import subprocess

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib

from . import profiles
from .i18n import _


def _create_backend():
    """Create the best available tray icon backend."""
    # Try XApp.StatusIcon first (Cinnamon-native, correct positioning)
    try:
        gi.require_version('XApp', '1.0')
        from gi.repository import XApp
        return _XAppBackend()
    except (ValueError, ImportError):
        pass

    # Try Gtk.StatusIcon (deprecated but works on most DEs)
    try:
        return _GtkStatusIconBackend()
    except Exception:
        pass

    # Fall back to AppIndicator3
    gi.require_version('AppIndicator3', '0.1')
    from gi.repository import AppIndicator3
    return _AppIndicatorBackend()


class _XAppBackend:
    def __init__(self):
        from gi.repository import XApp
        self.icon = XApp.StatusIcon()
        self.icon.set_icon_name('video-display')
        self.icon.set_tooltip_text('SplitRandR')
        self.icon.set_name('splitrandr')

    def set_menu(self, menu):
        self.icon.set_primary_menu(menu)
        self.icon.set_secondary_menu(menu)

    def set_activate_callback(self, callback):
        self.icon.connect('activate', lambda icon, button, time: callback())

    def destroy(self):
        self.icon.set_visible(False)


class _GtkStatusIconBackend:
    def __init__(self):
        self.icon = Gtk.StatusIcon()
        self.icon.set_from_icon_name('video-display')
        self.icon.set_tooltip_text('SplitRandR')
        self._menu = None

    def set_menu(self, menu):
        self._menu = menu
        try:
            self.icon.disconnect_by_func(self._on_popup)
        except TypeError:
            pass
        self.icon.connect('popup-menu', self._on_popup)

    def _on_popup(self, icon, button, time):
        if self._menu:
            self._menu.popup(None, None,
                             Gtk.StatusIcon.position_menu, icon,
                             button, time)

    def set_activate_callback(self, callback):
        self.icon.connect('activate', lambda icon: callback())

    def destroy(self):
        self.icon.set_visible(False)


class _AppIndicatorBackend:
    def __init__(self):
        from gi.repository import AppIndicator3
        self.indicator = AppIndicator3.Indicator.new(
            'splitrandr', 'video-display',
            AppIndicator3.IndicatorCategory.HARDWARE,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

    def set_menu(self, menu):
        self.indicator.set_menu(menu)

    def set_activate_callback(self, callback):
        pass  # AppIndicator3 doesn't support left-click activate

    def destroy(self):
        from gi.repository import AppIndicator3
        self.indicator.set_status(AppIndicator3.IndicatorStatus.PASSIVE)


class SplitRandRTray:
    def __init__(self, app=None):
        self.app = app
        self._backend = _create_backend()
        self._backend.set_activate_callback(self._on_activate)
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
        self._backend.set_menu(menu)

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

    def _on_activate(self):
        self._on_open_editor(None)

    def _on_open_editor(self, _item):
        if self.app and hasattr(self.app, 'window'):
            self.app.window.present()
        else:
            subprocess.Popen(['splitrandr'])

    def _on_quit(self, _item):
        Gtk.main_quit()

    def destroy(self):
        self._backend.destroy()
