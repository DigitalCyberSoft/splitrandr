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
import sys
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
        window.props.title = _("SplitRandR")
        window.set_icon_name('video-display')
        window.connect('delete-event', self._on_delete_event)

        self._updating_controls = False
        # Which profile the Proposed pane reflects; None = live X state.
        self._shown_profile = None
        # Status-InfoBar suppression flags (see gui_app_layout).
        self._apply_in_flight = False
        self._reload_in_flight = False
        self._fxr_bad_streak = 0

        window.set_titlebar(self._build_headerbar())

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
        self.current_widget.set_fit_height(150)
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
        self.original_widget.set_fit_height(150)
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

        # Size to the primary monitor's work area rather than a fixed
        # geometry — on a 4K leaf this opens near-full-height; the old
        # 1160x1000 stays as the floor for small work areas. With the
        # fakexrandr shim loaded, Gdk monitors are the split leaves, so
        # this sizes relative to the leaf the window will occupy.
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() if display else None
        if monitor is None and display and display.get_n_monitors() > 0:
            monitor = display.get_monitor(0)
        if monitor:
            wa = monitor.get_workarea()
            window.set_default_size(
                max(1160, int(wa.width * 0.72)),
                max(1000, int(wa.height * 0.85)))
        else:
            window.set_default_size(1160, 1000)
        window.show_all()

        self._tray = None

        # Start tray if enabled (default on: the resident process is
        # what keeps wake/unlock re-apply alive).
        if profiles.get_setting('tray_enabled', 'true') == 'true':
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

        # Initial control state. Auto-select the primary output so the
        # controls area never starts as a dead zone; _shown_profile
        # stays None because the pane holds live X state, not a profile.
        self.widget.select_default_output()
        self._update_controls_for_selection()
        self._refresh_profile_ui()

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


def _strip_own_preload():
    """Re-exec without the fakexrandr shim if this process inherited it.

    splitrandr manages the REAL display state; launched from a preloaded
    lineage (Cinnamon menu items inherit the WM's LD_PRELOAD, and the
    session-wide preload reaches everything else), its own GDK/Xlib view
    becomes the synthesized one — fake EDID-less outputs, folded leaf
    names, leaf outputs listed as physical monitors in the controls, and
    split trees that fail to reconstruct. xrandr SUBPROCESSES were
    always stripped (xrandr_invoke._xrandr_env); this strips the process
    itself. exec-time interposition can't be undone in-process, hence
    the re-exec.
    """
    import sys
    preload = os.environ.get('LD_PRELOAD', '')
    if not preload:
        return
    from .fakexrandr_config import _is_fake_xrandr_lib_path
    parts = [p for p in preload.split(':') if p]
    kept = [p for p in parts if not _is_fake_xrandr_lib_path(p)]
    if len(kept) == len(parts):
        return
    if kept:
        os.environ['LD_PRELOAD'] = ':'.join(kept)
    else:
        os.environ.pop('LD_PRELOAD', None)
    if os.path.basename(sys.argv[0]) == '__main__.py':
        argv = [sys.executable, '-m', 'splitrandr'] + sys.argv[1:]
    else:
        argv = [sys.executable] + sys.argv
    os.execv(sys.executable, argv)


_logging_setup_done = False


def _log_file_path():
    state_home = os.environ.get('XDG_STATE_HOME') or os.path.join(
        os.path.expanduser('~'), '.local', 'state')
    return os.path.join(state_home, 'splitrandr', 'splitrandr.log')


def _setup_logging():
    """Log to stderr AND a timestamped rotating file.

    stderr is not a reliable channel on this rig: autostarted instances
    land in ~/.xsession-errors, GIO desktop launches (the Cinnamon menu)
    get /dev/null, and root recovery runs get a tty that is gone by the
    time anyone reads it. The file is the only post-mortem record that
    survives every launch path -- the 2026-08-02 lock-screen incident was
    diagnosable only through config file mtimes and the shim's own log
    because the running watcher's stderr was /dev/null.
    """
    global _logging_setup_done
    if _logging_setup_done:
        return
    _logging_setup_done = True

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(logging.Formatter('%(name)s: %(message)s'))
    root.addHandler(stderr_handler)

    log_path = None
    try:
        from logging.handlers import RotatingFileHandler
        log_path = _log_file_path()
        os.makedirs(os.path.dirname(log_path), mode=0o700, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path, maxBytes=4 * 1024 * 1024, backupCount=3)
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s %(name)s: %(message)s'))
        root.addHandler(file_handler)
    except OSError as e:
        print("splitrandr: file logging unavailable (%s); "
              "continuing with stderr only" % e, file=sys.stderr)
        log_path = None

    logging.getLogger('splitrandr').info(
        "splitrandr %s starting (pid=%d, argv=%r, log file: %s)",
        __version__, os.getpid(), sys.argv,
        log_path or 'unavailable, stderr only')


def _install_excepthook():
    """Log unhandled exceptions before the default hook runs.

    abrt on this machine runs with ProcessUnpackaged=no and DESTROYS the
    traceback for unpackaged scripts -- it ate two `splitrandr --apply`
    crashes (2026-08-01 18:42, 2026-08-02 10:51) and the
    cinnamon-screensaver death (2026-08-02 12:40), leaving only "Error in
    sys.excepthook:" lines. This hook keeps our own copy in the log file,
    then chains to the previous hook so abrt/stderr behavior is unchanged.
    """
    def _hook(exc_type, exc, tb):
        if not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            logging.getLogger('splitrandr').critical(
                "unhandled exception; process is exiting",
                exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _hook

    import threading
    def _thread_hook(args):
        logging.getLogger('splitrandr').critical(
            "unhandled exception in thread %r; process is exiting",
            args.thread.name if args.thread else '?',
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        threading.__excepthook__(args)
    threading.excepthook = _thread_hook


def main():
    _strip_own_preload()
    _setup_logging()
    _install_excepthook()

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
        help="Write fakexrandr.bin and the compositor's monitors.xml from current X state, then exit",
        action='store_true'
    )
    parser.add_option(
        '--watch',
        help='Run headless, re-applying active profile on screen unlock or wake from suspend',
        action='store_true'
    )
    parser.add_option(
        '--nudge-repaint',
        help='Force windows to redraw (fixes stale window contents after the '
             'displays drop out and come back), then exit',
        action='store_true'
    )
    parser.add_option(
        '--nudge-mode',
        help="How --nudge-repaint works: 'resize' (default) grows each window "
             "and sets it back, forcing a new drawing surface; 'remap' bounces "
             "each window off a spare desktop and back, geometry-neutral but "
             "measured ineffective for stale surfaces; 'refresh' just sends "
             "an xrefresh, which touches nothing but only helps if the client "
             "merely needs an Expose",
        choices=('resize', 'remap', 'refresh'), default='resize'
    )
    parser.add_option(
        '--recover-shell',
        help='Recover a wedged compositor after a display power loss: '
             'deactivate a trapped lock screen, restart the shell with the '
             'fakexrandr shim (shielding terminals from the WM swap), verify '
             'it reports monitors again, rebuild the split tiles, and force '
             'a repaint. Safe to run any time: exits immediately when the '
             'shell is healthy',
        action='store_true'
    )
    parser.add_option(
        '--sentinel',
        help='One health check: recover the shell if it is wedged and '
             'relaunch the watcher if it is missing. Meant to be run by the '
             'systemd user timer (see --install-sentinel)',
        action='store_true'
    )
    parser.add_option(
        '--install-sentinel',
        help='Install and enable a systemd --user timer running --sentinel '
             'every minute',
        action='store_true'
    )

    (options, args) = parser.parse_args()

    # Handled before the singleton lock: this only moves windows between
    # desktops and never touches ~/.config/fakexrandr.bin, so it is safe to run
    # while the GUI instance is up -- which is exactly when it is needed.
    if options.nudge_repaint:
        from . import window_layout
        n = window_layout.nudge_repaint(mode=options.nudge_mode)
        print("nudged %d windows (mode=%s)" % (n, options.nudge_mode))
        return

    # Also pre-lock: recovery and the sentinel must run while the GUI
    # instance holds the singleton lock -- the GUI may be SIGSTOPped or
    # wedged along with the shell, which is exactly when these are
    # needed. They serialize against each other on their own recovery
    # lock instead, and never write ~/.config/fakexrandr.bin outside a
    # recovery (where the only other writer is frozen by the shield).
    if options.recover_shell:
        from .shell_recovery import recover
        auto = bool(os.environ.get('SPLITRANDR_AUTO_RECOVERY'))
        verdict = recover(auto=auto, reason='cli --recover-shell')
        print("shell recovery: %s" % verdict)
        return

    if options.sentinel:
        from .shell_recovery import sentinel
        sentinel()
        return

    if options.install_sentinel:
        from .shell_recovery import install_sentinel
        install_sentinel()
        return

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
