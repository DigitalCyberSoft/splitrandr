# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Command-line entry points for splitrandr: ``--watch``, ``--apply``,
``--regenerate``, ``--update-configs``.

Each function corresponds to one option in :func:`splitrandr.gui.main`'s
``optparse`` setup. They live here (rather than in ``gui.py``) because
they don't need any of the GTK ``Application`` state — they construct a
fresh :class:`XRandR` and operate on disk + X server state directly.
"""

import os

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gdk, GLib

from . import profiles
from .gui_screen_watcher import ScreenWatcher


# ``Application.LAYOUT_JSON`` is the canonical autostart-config path; we
# import it lazily to avoid a circular import (``gui.py`` imports this
# module while it's still being initialised).
def _layout_json_path():
    from .gui import Application
    return Application.LAYOUT_JSON


def _run_watch():
    """Run headless screen watcher that re-applies layout on unlock/wake."""
    # Initialize GDK so Gdk.Screen monitors-changed signals work
    Gdk.init([])
    watcher = ScreenWatcher()
    active = profiles.get_active_profile()
    if active:
        print("Watching for screen events, will re-apply profile '%s'" % active)
    else:
        print("Watching for screen events (no active profile yet)")
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    watcher.destroy()


def _apply_config(json_path):
    """Load layout from JSON and apply via save_to_x()."""
    from .xrandr import XRandR

    if not os.path.exists(json_path):
        print("Error: config file not found: %s" % json_path)
        return

    xrandr = XRandR(force_version=True)
    xrandr.load_from_x()
    xrandr.load_from_json(json_path)
    xrandr.save_to_x()
    print("Applied config from %s" % json_path)


def _regenerate_config():
    """Regenerate autostart config and active profile from current X state."""
    from .xrandr import XRandR

    xrandr = XRandR(force_version=True)
    xrandr.load_from_x()

    # Preserve pre-commands from existing JSON config if present
    json_path = _layout_json_path()
    if os.path.exists(json_path):
        try:
            import json
            with open(json_path) as f:
                existing = json.load(f)
            pre_cmds = existing.get('pre_commands', [])
            if pre_cmds:
                xrandr.configuration._pre_commands = pre_cmds
        except Exception:
            pass

    # Save layout as JSON
    xrandr.save_to_json(json_path)
    print("Updated autostart config: %s" % json_path)

    # Regenerate active profile
    active = profiles.get_active_profile()
    if active:
        profiles.save_profile(active, xrandr.configuration.to_dict())
        print("Updated profile: %s" % active)
    else:
        print("No active profile to update")

    # Regenerate fakexrandr config and cinnamon-monitors.xml
    try:
        from .fakexrandr_config import (
            write_fakexrandr_config, write_cinnamon_monitors_xml,
        )
        borders = xrandr.configuration.borders
        write_fakexrandr_config(
            xrandr.configuration.splits, xrandr.state, xrandr.configuration, borders
        )
        write_cinnamon_monitors_xml(
            xrandr.configuration.splits, xrandr.state, xrandr.configuration, borders
        )
        print("Updated cinnamon-monitors.xml")
    except Exception as e:
        print("Warning: failed to update configs: %s" % e)


def _update_configs():
    """Write fakexrandr.bin and cinnamon-monitors.xml from current X state."""
    from .xrandr import XRandR

    xrandr = XRandR(force_version=True)
    xrandr.load_from_x()

    # Try to load borders from the JSON config if it exists
    borders = xrandr.configuration.borders
    json_path = _layout_json_path()
    if not borders and os.path.exists(json_path):
        try:
            import json
            with open(json_path) as f:
                data = json.load(f)
            for bname, bval in data.get('borders', {}).items():
                if isinstance(bval, int) and bval > 0:
                    borders[bname] = bval
        except Exception:
            pass

    from .fakexrandr_config import (
        write_fakexrandr_config, write_cinnamon_monitors_xml,
    )
    write_fakexrandr_config(
        xrandr.configuration.splits, xrandr.state, xrandr.configuration, borders
    )
    write_cinnamon_monitors_xml(
        xrandr.configuration.splits, xrandr.state, xrandr.configuration, borders
    )
