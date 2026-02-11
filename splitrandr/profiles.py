# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Profile and settings management for SplitRandR (no GTK dependency)."""

import configparser
import os
import stat
import subprocess

CONFIG_DIR = os.path.expanduser('~/.config/splitrandr')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config')
ACTIVE_FILE = os.path.join(CONFIG_DIR, 'active')
PROFILES_DIR = os.path.join(CONFIG_DIR, 'profiles')


# ── Settings ──────────────────────────────────────────────────────────

def _read_config():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)
    return cfg


def _write_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        cfg.write(f)


def get_setting(key, default=None):
    cfg = _read_config()
    return cfg.get('splitrandr', key, fallback=default)


def set_setting(key, value):
    cfg = _read_config()
    if not cfg.has_section('splitrandr'):
        cfg.add_section('splitrandr')
    cfg.set('splitrandr', key, value)
    _write_config(cfg)


def is_first_run():
    return not os.path.exists(CONFIG_FILE)


# ── Profiles ──────────────────────────────────────────────────────────

def list_profiles():
    if not os.path.isdir(PROFILES_DIR):
        return []
    names = []
    for f in os.listdir(PROFILES_DIR):
        if f.endswith('.sh'):
            names.append(f[:-3])
    return sorted(names)


def profile_path(name):
    return os.path.join(PROFILES_DIR, name + '.sh')


def save_profile(name, script_content):
    os.makedirs(PROFILES_DIR, exist_ok=True)
    path = profile_path(name)
    with open(path, 'w') as f:
        f.write(script_content)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


def delete_profile(name):
    path = profile_path(name)
    if os.path.exists(path):
        os.remove(path)
    if get_active_profile() == name:
        set_active_profile('')


def get_active_profile():
    try:
        with open(ACTIVE_FILE, 'r') as f:
            return f.read().strip()
    except FileNotFoundError:
        return ''


def set_active_profile(name):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(ACTIVE_FILE, 'w') as f:
        f.write(name + '\n')


def apply_profile(name):
    path = profile_path(name)
    if os.path.exists(path):
        # Clear fakexrandr config so the script's xrandr commands
        # see real physical outputs (e.g. DP-5 instead of DP-5~1/~2/~3)
        try:
            from .fakexrandr_config import CONFIG_PATH
            if os.path.exists(CONFIG_PATH):
                os.remove(CONFIG_PATH)
        except Exception:
            pass
        subprocess.run(['sh', path])
        set_active_profile(name)

        # Restart xapp-sn-watcher so AppIndicator3 menus use new layout
        # (the profile script runs setmonitor commands that change geometry)
        try:
            subprocess.run(
                ['pkill', '-x', 'xapp-sn-watcher'],
                capture_output=True, timeout=5
            )
        except Exception:
            pass

        # Update fakexrandr config and monitors.xml from current X state
        try:
            from .xrandr import XRandR
            xrandr = XRandR(force_version=True)
            xrandr.load_from_x()

            from .fakexrandr_config import (
                write_fakexrandr_config, write_cinnamon_monitors_xml,
            )
            write_fakexrandr_config(
                xrandr.configuration.splits, xrandr.state, xrandr.configuration
            )
            write_cinnamon_monitors_xml(
                xrandr.configuration.splits, xrandr.state, xrandr.configuration
            )
        except Exception:
            pass
