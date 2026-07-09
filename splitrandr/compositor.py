# SplitRandR -- desktop-environment (compositor) abstraction
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Cinnamon (Muffin) vs GNOME (Mutter) abstraction.

splitrandr was written against Cinnamon; every Muffin/cinnamon-specific
name, path, and crash workaround routes through this module so the same
code can drive GNOME as well. One hard constraint: fakexrandr is an
**X11-only** trick (it LD_PRELOADs libXrandr/libxcb-randr), so a GNOME
*Wayland* session can never work — only "GNOME on Xorg". Callers should
check :func:`session_is_wayland` and refuse with a clear message there.

Muffin is a fork of Mutter, so the DisplayConfig D-Bus API, the
monitors.xml schema, and the ``<shell> --replace`` restart verb are all
structurally identical between the two — only the names/paths differ.
The genuinely Cinnamon-only pieces (the muffin#532 SIGSTOP guard, the
csd-xrandr plugin fight, panel pinning, and the separate
cinnamon-screensaver process) are exposed as boolean capability flags
that are False on GNOME.
"""

import os
import functools
import subprocess

CINNAMON = 'cinnamon'
GNOME = 'gnome'


class Compositor:
    """DE-specific names, paths, and capability flags. Immutable per session."""

    def __init__(self, kind):
        self.kind = kind

    def __repr__(self):
        return "Compositor(%r)" % self.kind

    @property
    def is_cinnamon(self):
        return self.kind == CINNAMON

    @property
    def is_gnome(self):
        return self.kind == GNOME

    # --- process / restart -------------------------------------------------
    @property
    def shell_process(self):
        """Process name (``pgrep -x`` / ``/proc/PID/comm``) of the WM+shell."""
        return 'cinnamon' if self.is_cinnamon else 'gnome-shell'

    @property
    def restart_argv(self):
        """Argv that replaces the running shell in place (X11)."""
        return [self.shell_process, '--replace']

    # --- Mutter/Muffin DisplayConfig D-Bus ---------------------------------
    @property
    def displayconfig_name(self):
        return ('org.cinnamon.Muffin.DisplayConfig' if self.is_cinnamon
                else 'org.gnome.Mutter.DisplayConfig')

    @property
    def displayconfig_path(self):
        return ('/org/cinnamon/Muffin/DisplayConfig' if self.is_cinnamon
                else '/org/gnome/Mutter/DisplayConfig')

    @property
    def displayconfig_iface(self):
        # Interface name == well-known bus name for both.
        return self.displayconfig_name

    # --- shell control D-Bus (readiness probe) -----------------------------
    @property
    def shell_bus_name(self):
        return 'org.Cinnamon' if self.is_cinnamon else 'org.gnome.Shell'

    @property
    def shell_bus_path(self):
        return '/org/Cinnamon' if self.is_cinnamon else '/org/gnome/Shell'

    @property
    def supports_eval(self):
        """org.Cinnamon.Eval exists; gnome-shell disables unrestricted Eval
        by default (security), so GNOME readiness must fall back to a Ping."""
        return self.is_cinnamon

    # --- persistence file --------------------------------------------------
    @property
    def monitors_xml_path(self):
        """The shell's persisted display layout. Same Mutter schema; Cinnamon
        just names the file cinnamon-monitors.xml, GNOME uses monitors.xml."""
        cfg = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
        return os.path.join(
            cfg, 'cinnamon-monitors.xml' if self.is_cinnamon else 'monitors.xml')

    # --- settings-daemon xrandr plugin (Cinnamon only) ---------------------
    @property
    def csd_xrandr_schema(self):
        """gsettings schema of the settings-daemon xrandr plugin to disable
        during setmonitor, or None. GNOME folded display handling into Mutter
        itself, so there is no separate plugin to fight."""
        return ('org.cinnamon.settings-daemon.plugins.xrandr'
                if self.is_cinnamon else None)

    # --- capability flags for Cinnamon-only workarounds --------------------
    @property
    def needs_setmonitor_sigstop_guard(self):
        """muffin#532: Muffin >= 5.4.0 SIGSEGVs on ``--setmonitor`` events, so
        splitrandr SIGSTOPs Cinnamon across those calls. Mutter is not known
        to share the crash; keep the guard Cinnamon-only until proven needed."""
        return self.is_cinnamon

    @property
    def needs_screensaver_override(self):
        """Cinnamon's lock screen is a separate D-Bus-activated
        cinnamon-screensaver process with no preload, so it needs the
        fakexrandr injection override. GNOME draws the lock inside gnome-shell
        itself, which already carries the preload — nothing to override."""
        return self.is_cinnamon

    @property
    def has_panels(self):
        """Cinnamon has gsettings-controlled panels to pin to the primary
        monitor; GNOME's top bar has no equivalent knob."""
        return self.is_cinnamon


@functools.lru_cache(maxsize=1)
def detect():
    """Best-effort detection of the running desktop, cached for the session.

    Prefers ``XDG_CURRENT_DESKTOP``; that variable is frequently absent in a
    ``systemd --user`` service (e.g. the splitrandr-watch unit), so it falls
    back to sniffing which shell is actually running, and finally defaults to
    Cinnamon to preserve historical behavior.
    """
    xdg = (os.environ.get('XDG_CURRENT_DESKTOP') or '').lower()
    if 'cinnamon' in xdg:
        return Compositor(CINNAMON)
    if 'gnome' in xdg:
        return Compositor(GNOME)
    for kind, proc in ((CINNAMON, 'cinnamon'), (GNOME, 'gnome-shell')):
        try:
            if subprocess.run(['pgrep', '-x', proc],
                              capture_output=True, timeout=5).returncode == 0:
                return Compositor(kind)
        except Exception:
            pass
    return Compositor(CINNAMON)


def current():
    """The Compositor for this session."""
    return detect()


def session_is_wayland():
    """True if the session is Wayland, where fakexrandr cannot work at all."""
    if (os.environ.get('XDG_SESSION_TYPE') or '').lower() == 'wayland':
        return True
    return bool(os.environ.get('WAYLAND_DISPLAY'))
