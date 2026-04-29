# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Main GUI for SplitRandR.

The :class:`Application` class is composed via mixins from
``gui_app_layout``, ``gui_app_controls``, ``gui_app_apply`` and
``gui_app_profiles``; the headless screen-watcher and the
singleton-lock helper live in ``gui_screen_watcher`` and ``gui_lock``;
the CLI entry points (``--apply``, ``--watch`` etc.) live in
``gui_cli``. ``main()`` is small and deliberately stays here so
``python -m splitrandr`` keeps resolving to the same target.
"""

import os
import optparse
import logging

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Gdk

from . import widget
from . import profiles
from .i18n import _
from .meta import __version__
from .gui_lock import _acquire_singleton_lock, _signal_existing_instance
from .gui_screen_watcher import ScreenWatcher
from .gui_app_layout import ApplicationLayoutMixin
from .gui_app_controls import ApplicationControlsMixin
from .gui_app_apply import ApplicationApplyMixin
from .gui_app_profiles import ApplicationProfilesMixin
from .gui_cli import (
    _run_watch, _apply_config, _regenerate_config, _update_configs,
)


log = logging.getLogger('splitrandr')


class Application(
    ApplicationLayoutMixin,
    ApplicationControlsMixin,
    ApplicationApplyMixin,
    ApplicationProfilesMixin,
):

    LAYOUT_JSON = os.path.expanduser('~/.config/splitrandr/layout.json')
    AUTOSTART_DESKTOP = os.path.expanduser('~/.config/autostart/splitrandr.desktop')

    def __init__(self, randr_display=None, force_version=False):
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

        # Current (read-only) widget — shows Cinnamon's actual layout
        self.current_widget = widget.MonitorWidget(
            display=randr_display, force_version=force_version,
            window=self.window, readonly=True
        )
        self.current_widget.load_from_x()

        # Proposed (editable) widget
        self.widget = widget.MonitorWidget(
            display=randr_display, force_version=force_version,
            window=self.window
        )
        self.widget.load_from_x()
        # NOTE: split-tree overlay from cinnamon + layout.json is now
        # handled inside XRandR.load_from_x itself, so the editable
        # widget's Proposed pane is filled correctly without any
        # explicit work here.

        self.widget.connect('selection-changed', self._on_selection_changed)
        self.widget.connect('changed', self._on_widget_changed)

        # Real (no-virtual) read-only widget. Shares the Proposed
        # widget's XRandR so it always renders the same physical
        # outputs, just with splits/borders stripped — i.e. what xrandr
        # would see without --setmonitor or fakexrandr.
        self.original_widget = widget.MonitorWidget(
            display=randr_display, force_version=force_version,
            window=self.window, readonly=True, show_splits=False,
            share_xrandr_with=self.widget,
        )
        self.original_widget._sync_monitors()
        self.original_widget._update_size_request()
        # Refresh whenever the editable widget changes so positions /
        # primary in the Real pane stay in lockstep.
        self.widget.connect(
            'changed',
            lambda _w: (self.original_widget._sync_monitors(),
                        self.original_widget._update_size_request(),
                        self.original_widget._force_repaint()),
        )

        # Single page layout (no notebook)
        main_page = self._build_page()
        window.add(main_page)

        # Request maximize BEFORE mapping so the WM honors it as
        # part of the initial geometry — calling after show_all()
        # races Muffin's map handler and is frequently ignored.
        window.maximize()
        window.show_all()

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

        # Watch for screen unlock / wake to re-apply layout
        self._screen_watcher = ScreenWatcher()

        # SIGUSR1: a second-launch attempt (e.g. user clicks the
        # launcher while a tray instance is running) signals us to
        # raise the window.  The signal handler runs in arbitrary
        # context — defer the GTK calls to the main loop via
        # GLib.idle_add to avoid races with paint / event handlers.
        import signal
        signal.signal(signal.SIGUSR1, lambda *a: GLib.idle_add(self._raise_window))

        # Initial control state
        self._update_controls_for_selection()
        self._populate_profiles_combo()
        # No profile is active on fresh startup (we loaded from X, not a profile)
        self._profile_combo.set_active(-1)

        # Upgrade current widget to Cinnamon view after initial draw completes.
        # Done via idle_add so the DBUS call doesn't block the first paint.
        GLib.idle_add(self._upgrade_current_to_cinnamon)

    def _upgrade_current_to_cinnamon(self):
        self.current_widget.load_from_cinnamon()
        return False

    def _raise_window(self):
        """Bring the GUI window back from hidden/iconified state.
        Invoked from the SIGUSR1 handler when a second-launch attempt
        signals the running instance.  Called from the main loop via
        GLib.idle_add."""
        try:
            self.window.show()
            self.window.deiconify()
            self.window.present_with_time(Gdk.CURRENT_TIME)
        except Exception:
            pass
        return False  # one-shot idle callback


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(name)s: %(message)s',
    )

    parser = optparse.OptionParser(
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
    parser.add_option(
        '--apply',
        help='Apply layout from JSON config (default: ~/.config/splitrandr/layout.json), then exit',
        action='store_true'
    )
    parser.add_option(
        '--regenerate',
        help='Regenerate autostart config and active profile from current X state, then exit',
        action='store_true'
    )
    parser.add_option(
        '--update-configs',
        help='Write fakexrandr.bin and cinnamon-monitors.xml from current X state, then exit',
        action='store_true'
    )
    parser.add_option(
        '--watch',
        help='Run headless, re-applying active profile on screen unlock or wake from suspend',
        action='store_true'
    )

    (options, args) = parser.parse_args()

    # Block any second splitrandr in this session. Two instances racing
    # on ~/.config/fakexrandr.bin is what kicked off the crash chain on
    # 2026-04-29. Acquired AFTER argparse so --help/--version still work.
    if not _acquire_singleton_lock():
        # If the existing instance is the GUI sitting in the tray, ask
        # it to raise its window so the user's launch attempt
        # succeeds visually.  CLI subcommands (--apply, --watch,
        # --regenerate, --update-configs) shouldn't trigger a window
        # raise — they're not "open the GUI" requests — so we only
        # signal when launching the bare GUI.
        is_gui_launch = not (options.watch or options.apply
                             or options.regenerate or options.update_configs)
        if is_gui_launch:
            _signal_existing_instance()
        logging.getLogger('splitrandr').warning(
            "another splitrandr is already running in this session; exiting"
        )
        return

    if options.watch:
        _run_watch()
        return

    if options.apply:
        json_path = args[0] if args else Application.LAYOUT_JSON
        _apply_config(json_path)
        return

    if options.regenerate:
        _regenerate_config()
        return

    if options.update_configs:
        _update_configs()
        return

    app = Application(
        randr_display=options.randr_display,
        force_version=options.force_version
    )
    app.run()


if __name__ == '__main__':
    main()
