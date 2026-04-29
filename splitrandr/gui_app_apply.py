# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Mixin: layout-apply / revert / Cinnamon-reload paths for ``Application``.

The two top-level entry points are :meth:`do_apply` and
:meth:`do_apply_autostart` (the latter additionally writes the layout
JSON and the autostart .desktop). Both pre-update the active profile on
disk before calling ``self.widget.save_to_x()`` — a deliberate ordering
that the screen watcher's mid-apply tick depends on; see
``feedback_apply_race.md``.

:meth:`_capture_revert_script` snapshots the live X state into a shell
script that :meth:`_confirm_or_revert` will run if the user rejects the
new layout (or the 30s confirm-countdown expires). The reload path
(:meth:`_on_reload_cinnamon`) restarts Cinnamon with the fakexrandr
LD_PRELOAD wrapper and verifies the freshly loaded version.
"""

import os
import subprocess
import logging

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib

from . import profiles
from .i18n import _


def _safe_to_save_profile(xrandr):
    """Return False if cfg.splits is empty but Cinnamon's live MetaMonitor
    list shows more monitors than active outputs — i.e. the user has
    splits on screen right now and the in-memory configuration is stale
    (typically because a load_from_json just replaced cfg.splits with
    {} from a corrupted profile). Saving in that state would propagate
    the corruption.

    Returns True if it's safe to save (cfg.splits has data, OR cinnamon
    isn't reachable, OR cinnamon agrees there are no splits). The
    permissive default avoids blocking legitimate "user explicitly
    wants no splits" applies — any caller that genuinely wants empty
    splits should still proceed."""
    cfg = xrandr.configuration
    if cfg.splits:
        return True  # have data → safe
    # No splits in cfg. Check if cinnamon disagrees.
    try:
        from .cinnamon_compat import query_cinnamon_monitors
        live = query_cinnamon_monitors()
    except Exception:
        return True  # can't query → don't block
    if not live:
        return True  # cinnamon offline / wayland → don't block
    active_outputs = sum(
        1 for o in cfg.outputs.values() if getattr(o, 'active', False)
    )
    # Cinnamon shows more monitors than we have active outputs ⇒ it
    # currently sees splits. Refuse to overwrite the saved profile.
    return len(live) <= active_outputs


log = logging.getLogger('splitrandr')


class ApplicationApplyMixin:

    def _on_reload_cinnamon(self):
        """Force-restart Cinnamon with fakexrandr LD_PRELOAD and verify."""
        from .fakexrandr_config import (
            _find_fakexrandr_lib, restart_cinnamon_with_fakexrandr,
            is_cinnamon_fakexrandr_loaded, is_cinnamon_fakexrandr_current,
            write_fakexrandr_config, write_cinnamon_monitors_xml,
        )
        from .cinnamon_compat import _wait_cinnamon_on_dbus

        lib_path = _find_fakexrandr_lib()
        if not lib_path:
            self.widget.error_message(
                _("fakexrandr library not found.\n\n"
                  "Build it with 'make' in the fakexrandr/ directory."))
            return

        # Write configs before restarting so the new Cinnamon picks them up
        xrandr = self.widget._xrandr
        try:
            write_fakexrandr_config(
                xrandr.configuration.splits, xrandr.state,
                xrandr.configuration, xrandr.configuration.borders)
            write_cinnamon_monitors_xml(
                xrandr.configuration.splits, xrandr.state,
                xrandr.configuration, xrandr.configuration.borders)
        except Exception as e:
            self.widget.error_message(
                _("Failed to write configs: %s") % e)
            return

        restart_cinnamon_with_fakexrandr(lib_path)

        # Show a progress dialog while waiting for Cinnamon to come back
        dialog = Gtk.MessageDialog(
            transient_for=self.window, modal=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.NONE,
            text=_("Restarting Cinnamon..."),
        )
        dialog.format_secondary_text(
            _("Waiting for Cinnamon to restart with fakexrandr."))
        dialog.show_all()

        # Process GTK events so the dialog renders
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)

        ready = _wait_cinnamon_on_dbus(timeout=15.0)
        dialog.destroy()

        if not ready:
            self.widget.error_message(
                _("Cinnamon did not respond on D-Bus within 15 seconds."))
            return

        # Verify
        loaded = is_cinnamon_fakexrandr_loaded()
        current = is_cinnamon_fakexrandr_current()

        if loaded and current:
            dialog = Gtk.MessageDialog(
                transient_for=self.window, modal=True,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text=_("Cinnamon restarted successfully."),
            )
            dialog.format_secondary_text(
                _("fakexrandr is loaded and current."))
            dialog.run()
            dialog.destroy()
        elif loaded:
            self.widget.error_message(
                _("fakexrandr is loaded but the version is stale.\n\n"
                  "Rebuild the library and try again."))
        else:
            self.widget.error_message(
                _("fakexrandr is NOT loaded in the new Cinnamon process.\n\n"
                  "LD_PRELOAD may have been stripped."))

        # Refresh both panes
        self.current_widget.load_from_cinnamon()
        self.widget.load_from_x()

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
            _("Reverting in %d seconds…") % state['remaining']
        )
        dialog.add_button(_("Revert Settings"), Gtk.ResponseType.REJECT)
        dialog.add_button(_("Keep Changes"), Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)
        dialog.set_keep_above(True)

        secondary_label = dialog.get_message_area().get_children()[1]

        # Raise the main window and dialog above other windows.
        # After save_to_x() resets the display, the WM restacks
        # everything and the dialog ends up behind other windows.
        # Done via idle_add so it runs inside dialog.run()'s main loop
        # after the dialog has been properly mapped.
        def _raise():
            self.window.present_with_time(Gdk.CURRENT_TIME)
            dialog.present_with_time(Gdk.CURRENT_TIME)
            return False
        GLib.idle_add(_raise)

        def tick():
            state['remaining'] -= 1
            if state['remaining'] <= 0:
                dialog.response(Gtk.ResponseType.REJECT)
                return False
            secondary_label.set_text(
                _("Reverting in %d seconds…") % state['remaining']
            )
            return True

        state['timer_id'] = GLib.timeout_add_seconds(1, tick)

        response = dialog.run()
        GLib.source_remove(state['timer_id'])
        dialog.destroy()

        if response != Gtk.ResponseType.ACCEPT:
            log.info("REVERTING: running revert script")
            log.info("revert script:\n%s", revert_script)
            # Clear fakexrandr config so xrandr sees real physical outputs
            try:
                from .fakexrandr_config import CONFIG_PATH
                os.remove(CONFIG_PATH)
            except FileNotFoundError:
                pass
            subprocess.run(['sh', '-c', revert_script], timeout=30)
            self.widget.load_from_x()
            # Restore fakexrandr config if splits are active
            try:
                xrandr = self.widget._xrandr
                splits = xrandr.configuration.splits
                if any(not t.is_leaf for t in splits.values()):
                    from .fakexrandr_config import (
                        write_fakexrandr_config, write_cinnamon_monitors_xml,
                    )
                    borders = xrandr.configuration.borders
                    write_fakexrandr_config(
                        splits, xrandr.state, xrandr.configuration, borders
                    )
                    write_cinnamon_monitors_xml(
                        splits, xrandr.state, xrandr.configuration, borders
                    )
            except Exception:
                pass
            return False
        log.info("KEEPING changes")
        return True

    def do_apply(self):
        if self.widget.abort_if_unsafe():
            return

        revert_script = self._capture_revert_script()

        # Snapshot window positions BEFORE we touch the layout. If our
        # apply ends up restarting Cinnamon (size-change path), Mutter
        # will rearrange windows whose monitor changed dimensions; we
        # restore them after so the user doesn't have to.
        from . import window_layout
        window_snapshot = window_layout.capture()

        # Snapshot and pre-update the active profile.
        # save_to_x can take 10+ seconds; the screen watcher's debounce
        # timer fires ~3s after the first RandR event from inside that
        # call. If the profile still holds the OLD layout when the timer
        # fires, the watcher's _layout_matches sees old-profile vs.
        # in-progress X state, declares a mismatch, and re-applies the
        # OLD profile — racing the user's apply. So write the new
        # profile FIRST and roll back if the user reverts.
        active = profiles.get_active_profile()
        old_profile = None
        if active:
            try:
                with open(profiles.profile_path(active), 'r') as f:
                    old_profile = f.read()
            except Exception:
                old_profile = None
            if _safe_to_save_profile(self.widget._xrandr):
                try:
                    profiles.save_profile(
                        active,
                        self.widget._xrandr.configuration.to_dict(),
                    )
                except Exception as e:
                    log.warning("failed to pre-update active profile: %s", e)
            else:
                log.warning(
                    "skipping pre-save of profile %r: cfg.splits is empty "
                    "but Cinnamon still shows splits — refusing to "
                    "propagate stale state to disk",
                    active,
                )

        try:
            self.widget.save_to_x()
        except Exception as exc:
            dialog = Gtk.MessageDialog(
                None, Gtk.DialogFlags.MODAL, Gtk.MessageType.ERROR,
                Gtk.ButtonsType.OK, _("XRandR failed:\n%s") % exc
            )
            dialog.run()
            dialog.destroy()
            # Roll back the pre-saved profile since the apply failed.
            if active and old_profile is not None:
                try:
                    with open(profiles.profile_path(active), 'w') as f:
                        f.write(old_profile)
                except Exception:
                    pass
            return

        # Restore window positions in case Mutter shuffled them during a
        # Cinnamon restart. Small delay so Cinnamon's WM is fully ready.
        try:
            window_layout.restore(window_snapshot, settle_delay=0.5)
        except Exception as e:
            log.warning("window-layout restore failed: %s", e)

        # save_to_x can rebuild the configuration via load_from_x; refresh
        # the profile JSON with the post-load state for accuracy.
        if active:
            try:
                profiles.save_profile(
                    active,
                    self.widget._xrandr.configuration.to_dict(),
                )
            except Exception as e:
                log.warning("failed to refresh active profile after apply: %s", e)

        # Keep layout.json (the autostart/--apply entry point) in sync with
        # the active profile on every Apply, not just Apply & Autostart.
        try:
            self.widget._xrandr.save_to_json(self.LAYOUT_JSON)
        except Exception as e:
            log.warning("failed to update layout.json after apply: %s", e)

        if not self._confirm_or_revert(revert_script):
            # User reverted — restore the prior profile contents.
            if active and old_profile is not None:
                try:
                    with open(profiles.profile_path(active), 'w') as f:
                        f.write(old_profile)
                except Exception as e:
                    log.warning("failed to restore profile after revert: %s", e)

    def do_apply_autostart(self):
        if self.widget.abort_if_unsafe():
            return

        revert_script = self._capture_revert_script()

        from . import window_layout
        window_snapshot = window_layout.capture()

        # Pre-save profile so the screen watcher's mid-apply tick sees
        # the new layout, not the old one. See do_apply for details.
        active = profiles.get_active_profile()
        old_profile = None
        if active:
            try:
                with open(profiles.profile_path(active), 'r') as f:
                    old_profile = f.read()
            except Exception:
                old_profile = None
            if _safe_to_save_profile(self.widget._xrandr):
                try:
                    profiles.save_profile(
                        active,
                        self.widget._xrandr.configuration.to_dict(),
                    )
                except Exception as e:
                    log.warning("failed to pre-update active profile: %s", e)
            else:
                log.warning(
                    "skipping pre-save of profile %r: cfg.splits is empty "
                    "but Cinnamon still shows splits — refusing to "
                    "propagate stale state to disk",
                    active,
                )

        try:
            self.widget.save_to_x()
        except Exception as exc:
            dialog = Gtk.MessageDialog(
                None, Gtk.DialogFlags.MODAL, Gtk.MessageType.ERROR,
                Gtk.ButtonsType.OK, _("XRandR failed:\n%s") % exc
            )
            dialog.run()
            dialog.destroy()
            if active and old_profile is not None:
                try:
                    with open(profiles.profile_path(active), 'w') as f:
                        f.write(old_profile)
                except Exception:
                    pass
            return

        # Refresh profile after save_to_x's reload may have rebuilt cfg.
        if active:
            try:
                profiles.save_profile(
                    active,
                    self.widget._xrandr.configuration.to_dict(),
                )
            except Exception as e:
                log.warning("failed to refresh active profile after apply: %s", e)

        if not self._confirm_or_revert(revert_script):
            if active and old_profile is not None:
                try:
                    with open(profiles.profile_path(active), 'w') as f:
                        f.write(old_profile)
                except Exception as e:
                    log.warning("failed to restore profile after revert: %s", e)
            return

        # Save layout as JSON (autostart entry point reads this)
        self.widget._xrandr.save_to_json(self.LAYOUT_JSON)

        # Restore window positions in case the apply restarted Cinnamon.
        try:
            window_layout.restore(window_snapshot, settle_delay=0.5)
        except Exception as e:
            log.warning("window-layout restore failed: %s", e)

        # Write autostart .desktop pointing at --apply
        autostart_dir = os.path.dirname(self.AUTOSTART_DESKTOP)
        os.makedirs(autostart_dir, exist_ok=True)

        import sys
        python = sys.executable or 'python3'
        # The autostart's CWD is $HOME by default, which is the WRONG
        # place to invoke `python -m splitrandr` from when the project
        # isn't installed system-wide: from /home/user, Python finds
        # the project root /home/user/splitrandr as the package dir
        # and fails with "splitrandr is a package and cannot be
        # directly executed" because the project root has no
        # __main__.py — that lives in the inner splitrandr/ subdir.
        # Set Path= to the project root (one level above this file's
        # package dir) so `python -m splitrandr` resolves correctly.
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        desktop_entry = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=SplitRandR Layout\n"
            "Comment=Restore monitor layout and virtual splits\n"
            "Path=%s\n"
            "Exec=%s -m splitrandr --apply\n"
            "X-GNOME-Autostart-enabled=true\n"
        ) % (project_root, python)

        with open(self.AUTOSTART_DESKTOP, 'w') as f:
            f.write(desktop_entry)

        dialog = Gtk.MessageDialog(
            None, Gtk.DialogFlags.MODAL, Gtk.MessageType.INFO,
            Gtk.ButtonsType.OK,
            _("Layout applied and saved for autostart.\n\n"
              "Config: %s\n"
              "Autostart: %s") % (self.LAYOUT_JSON, self.AUTOSTART_DESKTOP)
        )
        dialog.run()
        dialog.destroy()
