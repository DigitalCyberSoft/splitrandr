# SplitRandR -- fakexrandr binary config writer
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Writes fakexrandr binary configuration and manages Cinnamon restart."""

import os
import re
import struct
import subprocess
import logging
import ctypes
import time
import xml.etree.ElementTree as ET

from . import compositor

log = logging.getLogger('splitrandr')

# Must match _fakexrandr_config_version in libXrandr.c
FAKEXRANDR_CONFIG_VERSION = 3
FAKEXRANDR_CONFIG_MAGIC = b"FXRD"
# Sentinel meaning "no leaf is explicitly primary"
FAKEXRANDR_NO_PRIMARY_LEAF = 0xFFFFFFFF
# Fixed-width 128-byte field carrying the connector that fakexrandr
# should advertise as primary, regardless of the X server's view.
# Required because Nvidia's binary driver eats `xrandr --primary` on
# tile sub-outputs; see libXrandr.c's _primary_connector_name comment.
FAKEXRANDR_PRIMARY_NAME_SIZE = 128


def _compute_primary_connector_name(splits_dict, xrandr_config):
    """Pick the connector name that fakexrandr should report as primary.

    Naming convention (post 2026-04-29):
      - leaf 0 takes the parent connector name (e.g. ``DP-5``)
      - leaves 1..N-1 are named ``PARENT~1``, ``PARENT~2``, ... (0-indexed)

    libXrandr.c emits fake-output names in the same form, so
    the .so's XRRGetOutputPrimary intercept resolves primary via a
    single ``strcmp`` against the augmented res->outputs list.

    For a non-split output marked primary, returns the raw connector
    name. Empty string means "no override".
    """
    for name, output in xrandr_config.outputs.items():
        if not getattr(output, 'active', False):
            continue
        if not getattr(output, 'primary', False):
            continue
        tree = splits_dict.get(name) if splits_dict else None
        if tree is not None and not tree.is_leaf:
            idx = tree.primary_leaf_index()
            if idx is None:
                idx = 0
            if idx == 0:
                return name
            return "%s~%d" % (name, idx)
        return name
    return ""


def _get_cinnamon_pid():
    """Get the PID of the live (non-zombie) cinnamon process."""
    from .cinnamon_compat import _get_cinnamon_pid as _impl
    return _impl()


def is_cinnamon_fakexrandr_loaded():
    """Check if the running Cinnamon process has fakexrandr loaded."""
    return _get_cinnamon_fakexrandr_path() is not None


def _get_so_config_version(lib_path):
    """Read _fakexrandr_config_version from a .so file via ctypes.

    Returns the version int, or 0 if the symbol is missing (old .so).
    """
    try:
        lib = ctypes.CDLL(lib_path)
        ver = ctypes.c_int.in_dll(lib, '_fakexrandr_config_version')
        return ver.value
    except (OSError, ValueError):
        return 0


# The real libX{randr,inerama} always live in a system library dir; our
# fakexrandr shim is deliberately installed outside the default linker path
# (historically site-packages/fakexrandr/, now /usr/local/lib64 per the COPR
# spec). Anything named like the X libs but mapped from outside these dirs is
# ours.
_SYSTEM_LIB_DIRS = ('/usr/lib64/', '/lib64/', '/usr/lib/', '/lib/')


def _is_fake_xrandr_lib_path(path):
    """True if a mapped file path is our fakexrandr shim, not the real X lib.

    Detecting by the literal substring 'fakexrandr' broke once the RPM moved
    the .so to /usr/local/lib64/libXrandr.so, whose path contains no such
    substring: Cinnamon had the shim loaded but is_cinnamon_fakexrandr_loaded()
    reported False, causing a spurious cinnamon --replace on every apply.
    """
    base = os.path.basename(path)
    if not (base.startswith('libXrandr.so')
            or base.startswith('libXinerama.so')):
        return False
    if 'fakexrandr' in path:  # legacy site-packages/fakexrandr/ layout
        return True
    return not any(path.startswith(d) for d in _SYSTEM_LIB_DIRS)


def _get_cinnamon_fakexrandr_path():
    """Find the fakexrandr .so path loaded in Cinnamon's process.

    Reads /proc/PID/maps for the cinnamon process and looks for a
    mapped fakexrandr library. Returns the on-disk path or None.
    """
    pid = _get_cinnamon_pid()
    if not pid:
        return None
    try:
        with open(f'/proc/{pid}/maps', 'r') as f:
            for line in f:
                # Format: addr perms offset dev inode path
                parts = line.split(maxsplit=5)
                if len(parts) < 6:
                    continue
                path = parts[5].rstrip('\n')
                # The path may end with " (deleted)" if the .so was replaced
                if path.endswith(' (deleted)'):
                    path = path[:-len(' (deleted)')]
                if _is_fake_xrandr_lib_path(path):
                    return path
    except (OSError, PermissionError):
        pass
    return None


def is_cinnamon_fakexrandr_current():
    """Check if Cinnamon's loaded fakexrandr .so matches the on-disk version.

    Returns True if versions match, False if mismatch or can't determine.
    """
    loaded_path = _get_cinnamon_fakexrandr_path()
    if not loaded_path:
        return False

    ondisk_path = _find_fakexrandr_lib()
    if not ondisk_path:
        return False

    # Check if maps shows (deleted) — the .so was replaced on disk
    pid = _get_cinnamon_pid()
    if pid:
        try:
            with open(f'/proc/{pid}/maps', 'r') as f:
                for line in f:
                    if 'fakexrandr' in line and '.so' in line and '(deleted)' in line:
                        log.info("fakexrandr .so is deleted from disk (stale)")
                        return False
        except (OSError, PermissionError):
            pass

    # Check if the loaded file is the same as the on-disk file
    try:
        loaded_stat = os.stat(loaded_path)
        ondisk_stat = os.stat(ondisk_path)
        if loaded_stat.st_dev == ondisk_stat.st_dev and loaded_stat.st_ino == ondisk_stat.st_ino:
            return True
    except OSError:
        pass

    # Files differ (or stat failed) — compare config versions
    loaded_ver = _get_so_config_version(loaded_path)
    ondisk_ver = _get_so_config_version(ondisk_path)
    if loaded_ver != ondisk_ver:
        log.info("fakexrandr version mismatch: loaded=%d, on-disk=%d", loaded_ver, ondisk_ver)
        return False

    return True

CONFIG_PATH = os.path.join(
    os.environ.get('XDG_CONFIG_HOME', os.path.expanduser('~/.config')),
    'fakexrandr.bin'
)


def _find_fakexrandr_lib():
    """Find the fakexrandr libXrandr.so.2, whether running from the source
    tree or installed from the RPM."""
    # Source tree: the built .so sits in <project>/fakexrandr/ next to the
    # Python package. Checked first so a dev run prefers the freshly built
    # local library over any system-installed copy.
    module_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(module_dir)
    candidates = [
        os.path.join(project_dir, 'fakexrandr', 'libXrandr.so.2'),
        os.path.join(project_dir, 'fakexrandr', 'libXrandr.so'),
        # Installed via RPM: the spec (splitrandr-copr's .copr/Makefile)
        # installs the .so to %{_prefix}/local/lib64. Without these the
        # installed package never finds its own library, so LD_PRELOAD is
        # never set and splitting silently does nothing.
        '/usr/local/lib64/libXrandr.so.2',
        '/usr/local/lib64/libXrandr.so',
        '/usr/local/lib/libXrandr.so.2',
        '/usr/local/lib/libXrandr.so',
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


# ---------------------------------------------------------------------------
# cinnamon-screensaver fakexrandr injection
# ---------------------------------------------------------------------------
#
# muffin sees the virtual split monitors because splitrandr restarts it with
# LD_PRELOAD=<fakexrandr libXrandr.so.2> (restart_cinnamon_with_fakexrandr).
# cinnamon-screensaver, though, is D-Bus activated from
# /usr/share/dbus-1/services/org.cinnamon.ScreenSaver.service with a bare
# `Exec=/usr/bin/cinnamon-screensaver` and NO preload, so the lock screen
# computes its geometry from the real, unsplit outputs. When the split and
# unsplit monitor rects disagree the screensaver logs
# "Screen rect ... and monitor rects ... DO NOT add up" and can fall back to
# cs-backup-locker — a bare black grab with no unlock UI, i.e. an
# unrecoverable lock on display hotplug.
#
# Fix: a user-level D-Bus service override (files under $XDG_DATA_HOME shadow
# the system service file for the session bus) that injects the SAME
# fakexrandr .so into the screensaver. cs-backup-locker inherits it as a
# child. Delete the file and reload dbus to restore the stock screensaver.

SCREENSAVER_DBUS_NAME = "org.cinnamon.ScreenSaver"
SCREENSAVER_EXEC = "/usr/bin/cinnamon-screensaver"
SCREENSAVER_FAKEXRANDR_LOG = "/tmp/fakexrandr-screensaver.log"


def _screensaver_override_path():
    """Path of the user D-Bus service override for cinnamon-screensaver.

    Uses $XDG_DATA_HOME (default ~/.local/share); a service file there
    shadows /usr/share/dbus-1/services/<name>.service on the session bus.
    """
    data_home = (os.environ.get('XDG_DATA_HOME')
                 or os.path.expanduser('~/.local/share'))
    return os.path.join(data_home, 'dbus-1', 'services',
                        SCREENSAVER_DBUS_NAME + '.service')


def _screensaver_override_content(lib_path):
    return (
        "[D-BUS Service]\n"
        "Name=%s\n"
        "# Managed by splitrandr (%s). Injects the fakexrandr LD_PRELOAD so the\n"
        "# lock screen sees the same virtual split monitors as muffin; without\n"
        "# it the screensaver's rects 'DO NOT add up' and it can wedge on\n"
        "# hotplug. Delete this file and reload dbus for the stock screensaver.\n"
        "Exec=/usr/bin/env LD_PRELOAD=%s FAKEXRANDR_LOG=%s %s\n"
        % (SCREENSAVER_DBUS_NAME, __name__, lib_path,
           SCREENSAVER_FAKEXRANDR_LOG, SCREENSAVER_EXEC)
    )


def write_screensaver_dbus_override(lib_path=None, activate=True):
    """Install/refresh the cinnamon-screensaver D-Bus override that preloads
    fakexrandr, so the lock screen sees splitrandr's virtual monitors.

    Idempotent: only rewrites when the content changes. When it does change
    and ``activate`` is set, the session bus is asked to reload its service
    files and any running cinnamon-screensaver is terminated so the next
    activation picks up the preload. The daemon re-activates on demand
    (idle/lock/query) — this does not lock the screen.

    Returns True if the override was written or updated, False otherwise.
    """
    # GNOME draws the lock screen inside gnome-shell (which already carries
    # the preload); there is no separate screensaver process to override.
    if not compositor.current().needs_screensaver_override:
        return False
    if lib_path is None:
        lib_path = _find_fakexrandr_lib()
    if not lib_path:
        log.warning("fakexrandr library not found; "
                    "skipping screensaver D-Bus override")
        return False

    dest = _screensaver_override_path()
    content = _screensaver_override_content(lib_path)

    try:
        with open(dest) as f:
            if f.read() == content:
                return False  # already current
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("could not read %s: %s", dest, e)

    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + '.tmp'
        with open(tmp, 'w') as f:
            f.write(content)
        os.replace(tmp, dest)
    except OSError as e:
        log.warning("failed to write screensaver override %s: %s", dest, e)
        return False

    log.info("installed screensaver fakexrandr override -> %s (LD_PRELOAD=%s)",
             dest, lib_path)

    if activate:
        _activate_screensaver_override()
    return True


def _activate_screensaver_override():
    """Reload the session bus service files and drop any running
    cinnamon-screensaver so the override takes effect on next activation."""
    try:
        subprocess.run(
            ['dbus-send', '--session', '--type=method_call',
             '--dest=org.freedesktop.DBus', '/org/freedesktop/DBus',
             'org.freedesktop.DBus.ReloadConfig'],
            capture_output=True, timeout=5)
    except Exception as e:
        log.warning("dbus ReloadConfig failed: %s", e)
    # Drop the running daemon so it re-activates with the preload. Match on
    # the exact executable path so csd-screensaver-proxy and the
    # cs-backup-locker helper (different argv) are left alone.
    try:
        subprocess.run(['pkill', '-f', '/cinnamon-screensaver$'],
                       capture_output=True, timeout=5)
    except Exception as e:
        log.warning("could not restart cinnamon-screensaver: %s", e)


# gsettings keys that gate cinnamon's automatic lock. Disabling these is the
# only fix that reliably keeps the lock screen from trapping the user on this
# split-monitor rig.
_LOCK_GSETTINGS = (
    ("org.cinnamon.desktop.screensaver", "lock-enabled", "false"),
    ("org.cinnamon.desktop.screensaver", "lock-delay", "uint32 0"),
    ("org.cinnamon.desktop.session", "idle-delay", "uint32 0"),
)


def disable_screensaver_lock():
    """Disable cinnamon's automatic screen lock while the split is active.

    On this virtual-split rig the lock screen is broken: cinnamon-screensaver
    cannot draw its real unlock UI over the split monitors, so it falls back to
    cs-backup-locker -- a bare black grab with NO password prompt, which locks
    the user out unrecoverably. This was verified 2026-07-14: even with the
    fakexrandr screensaver override active and the screensaver seeing the same
    6 monitors as muffin (rects agree), triggering a lock still spawned
    cs-backup-locker. So the shim override (write_screensaver_dbus_override) is
    necessary-but-insufficient; the lock itself must be disabled.

    Sets lock-enabled=false and idle-delay=0 so nothing auto-activates a lock.
    Persists in the user's dconf; re-applied on every split apply so it can't
    silently regress. Re-enable with `gsettings reset` on the same keys once the
    lock-screen rendering bug is fixed upstream.
    """
    for schema, key, value in _LOCK_GSETTINGS:
        try:
            subprocess.run(['gsettings', 'set', schema, key, value],
                           capture_output=True, timeout=5)
        except Exception as e:
            log.warning("could not set %s %s=%s: %s", schema, key, value, e)
    log.info("disabled cinnamon auto-lock (broken lock UI falls back to "
             "cs-backup-locker black grab on split monitors)")


def write_fakexrandr_config(splits_dict, xrandr_state, xrandr_config, borders_dict=None):
    """Write ~/.config/fakexrandr.bin from the current split configuration.

    Binary format (version 3):
        Header: b"FXRD" + <version:4B uint>
                + <primary_connector_name:128B padded>
        Per entry:
            <length:4B uint><name:128B padded><edid:768B padded>
            <width:4B uint><height:4B uint><split_count:4B uint><border:4B uint>
            <primary_leaf:4B uint, 0xFFFFFFFF if none><tree_data>

    Args:
        splits_dict: {output_name: SplitTree}
        xrandr_state: XRandR.State with output EDID data
        xrandr_config: XRandR.Configuration with output sizes
        borders_dict: {output_name: int} border pixels per output (default None)
    """
    entries = []

    for output_name, tree in splits_dict.items():
        output_cfg = xrandr_config.outputs.get(output_name)
        if not output_cfg or not output_cfg.active:
            continue

        output_state = xrandr_state.outputs.get(output_name)
        edid_hex = output_state.edid_hex if output_state else ""

        width = output_cfg.size[0]
        height = output_cfg.size[1]

        split_count = tree.count_leaves()
        tree_data = tree.to_fakexrandr_bytes(width, height)
        border = borders_dict.get(output_name, 0) if borders_dict else 0
        primary_leaf = tree.primary_leaf_index()
        if primary_leaf is None:
            primary_leaf = FAKEXRANDR_NO_PRIMARY_LEAF

        # Pack the entry (without the leading length field). v2 layout:
        # name(128) edid(768) width(4) height(4) split_count(4) border(4)
        # primary_leaf(4) tree_data(variable)
        name_bytes = output_name.encode('utf-8')[:128].ljust(128, b'\x00')
        edid_bytes = edid_hex.encode('ascii')[:768].ljust(768, b'\x00')
        entry_payload = (
            name_bytes +
            edid_bytes +
            struct.pack('I', width) +
            struct.pack('I', height) +
            struct.pack('I', split_count) +
            struct.pack('I', border) +
            struct.pack('I', primary_leaf) +
            tree_data
        )

        # Prepend the length
        entry = struct.pack('I', len(entry_payload)) + entry_payload
        entries.append(entry)

    config_dir = os.path.dirname(CONFIG_PATH)
    os.makedirs(config_dir, exist_ok=True)

    # The v3 global primary_connector_name is meaningful even when there
    # are no split entries — a non-split primary still needs to be
    # advertised through fakexrandr to defeat Mutter's leftmost-pick
    # default. So write the file whenever we have entries OR a primary.
    primary_connector = _compute_primary_connector_name(splits_dict, xrandr_config)
    primary_bytes = (
        primary_connector.encode('utf-8')[:FAKEXRANDR_PRIMARY_NAME_SIZE]
        .ljust(FAKEXRANDR_PRIMARY_NAME_SIZE, b'\x00')
    )

    if entries or primary_connector:
        header = (
            FAKEXRANDR_CONFIG_MAGIC
            + struct.pack('I', FAKEXRANDR_CONFIG_VERSION)
            + primary_bytes
        )
        # Atomic write: open(path, 'wb') truncates the file IN PLACE,
        # so any process reading concurrently (e.g. an LD_PRELOAD'd
        # Cinnamon's first XRRGetMonitors call) sees the file at 0
        # bytes and bails with "no config" passthrough — Cinnamon
        # then builds its MetaMonitor list without our primary
        # override, and the next monitors-changed callback dereferences
        # a NULL primary_logical_monitor → SEGV in
        # meta_display_logical_index_to_xinerama_index. Write to a
        # tmp file and rename so readers always see either the old
        # complete file or the new complete file, never a torn write.
        tmp_path = CONFIG_PATH + '.tmp'
        with open(tmp_path, 'wb') as f:
            f.write(header)
            for entry in entries:
                f.write(entry)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, CONFIG_PATH)
        total = len(header) + sum(len(e) for e in entries)
        log.info("wrote fakexrandr config: %s (%d entries, primary=%r, %d bytes)",
                 CONFIG_PATH, len(entries), primary_connector or None, total)
    else:
        # No splits active and no primary — remove config so fakexrandr passes through
        if os.path.exists(CONFIG_PATH):
            os.remove(CONFIG_PATH)
            log.info("removed fakexrandr config (no splits active)")


def _parse_edid_monitorspec(edid_hex):
    """Parse vendor, product name, and serial from an EDID hex string.

    Returns (vendor, product, serial) or ("unknown", "unknown", "unknown").
    """
    if not edid_hex or len(edid_hex) < 256:
        return ("unknown", "unknown", "unknown")

    try:
        # Manufacturer ID from bytes 8-9 (chars 16-19)
        val = (int(edid_hex[16:18], 16) << 8) | int(edid_hex[18:20], 16)
        vendor = (
            chr(((val >> 10) & 0x1f) + ord('A') - 1) +
            chr(((val >> 5) & 0x1f) + ord('A') - 1) +
            chr((val & 0x1f) + ord('A') - 1)
        )
    except (ValueError, IndexError):
        vendor = "unknown"

    product = "unknown"
    serial = "unknown"

    # Parse descriptor blocks (bytes 54-125, four 18-byte descriptors)
    for i in range(4):
        offset = 54 + i * 18
        char_off = offset * 2
        if char_off + 36 > len(edid_hex):
            break
        # Check if it's a text descriptor (first 3 bytes = 00 00 00)
        if edid_hex[char_off:char_off + 6] != '000000':
            continue
        tag = int(edid_hex[char_off + 6:char_off + 8], 16)
        # Text starts at byte offset+5 (char_off+10), 13 bytes
        text_hex = edid_hex[char_off + 10:char_off + 36]
        try:
            text = bytes.fromhex(text_hex).split(b'\x0a')[0].decode('ascii', errors='replace').strip()
        except (ValueError, UnicodeDecodeError):
            text = ""
        if tag == 0xFC and text:
            product = text
        elif tag == 0xFF and text:
            serial = text

    # Muffin falls back to the binary product/serial codes when the text
    # descriptors are absent (meta_output_parse_edid, muffin 6.6
    # meta-monitor-manager.c: "0x%04x" % product_code, "0x%08x" %
    # serial_number; edid-parse.c reads both little-endian). The strings
    # in cinnamon-monitors.xml must be byte-identical to muffin's or the
    # stored configuration never matches, so replicate the fallbacks.
    if product == "unknown":
        try:
            product = "0x%04x" % ((int(edid_hex[22:24], 16) << 8)
                                  | int(edid_hex[20:22], 16))
        except ValueError:
            pass
    if serial == "unknown":
        try:
            serial = "0x%08x" % ((int(edid_hex[30:32], 16) << 24)
                                 | (int(edid_hex[28:30], 16) << 16)
                                 | (int(edid_hex[26:28], 16) << 8)
                                 | int(edid_hex[24:26], 16))
        except ValueError:
            pass

    return (vendor, product, serial)


def _precise_mode_rates():
    """Parse ``xrandr --verbose`` mode timings into
    ``{output_name: [(width, height, rate), ...]}``.

    ``rate`` is dotClock / (hTotal * vTotal) — the exact value muffin
    computes for every mode (muffin 6.6 meta-gpu-xrandr.c) and compares
    against a stored monitors.xml ``<rate>`` with a 0.001 tolerance
    (MAXIMUM_REFRESH_RATE_DIFF, meta-monitor.c). The 2-decimal rates
    xrandr prints (59.97 for a true 59.9685) miss that tolerance, which
    silently discards the whole stored configuration at Cinnamon
    startup and triggers the enable-everything linear fallback.
    """
    env = os.environ.copy()
    env.pop('LD_PRELOAD', None)  # xrandr must never load our .so
    try:
        out = subprocess.run(
            ['xrandr', '--verbose'], env=env,
            capture_output=True, text=True, timeout=15
        ).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("xrandr --verbose failed, no precise rates: %s", e)
        return {}

    rates = {}
    modes = None
    mhz = width = h_total = None
    for line in out.splitlines():
        m = re.match(r'(\S+) (?:dis)?connected', line)
        if m:
            modes = rates.setdefault(m.group(1), [])
            mhz = None
            continue
        if modes is None:
            continue
        m = re.match(r'\s+\d+x\d+i?\s+\(0x[0-9a-f]+\)\s+([\d.]+)MHz', line)
        if m:
            mhz = float(m.group(1))
            continue
        m = re.match(r'\s+h:\s+width\s+(\d+).*\btotal\s+(\d+)', line)
        if m and mhz is not None:
            width, h_total = int(m.group(1)), int(m.group(2))
            continue
        m = re.match(r'\s+v:\s+height\s+(\d+).*\btotal\s+(\d+)', line)
        if m and mhz is not None and h_total:
            height, v_total = int(m.group(1)), int(m.group(2))
            if v_total:
                modes.append(
                    (width, height, mhz * 1e6 / (h_total * v_total)))
            mhz = None
    return rates


def _precise_rate_for(rates, output_name, output_cfg):
    """Best precise rate for an output's configured mode, falling back
    to the stored 2-decimal rate when the mode can't be found."""
    fallback = output_cfg.mode.refresh_rate or 60.0
    candidates = [r for (w, h, r) in rates.get(output_name, ())
                  if w == output_cfg.size[0] and h == output_cfg.size[1]]
    if not candidates:
        return fallback
    if output_cfg.mode.refresh_rate is None:
        return candidates[0]
    best = min(candidates, key=lambda r: abs(r - fallback))
    # 2-decimal rounding error is at most 0.005; anything further off
    # is a different mode with the same geometry.
    return best if abs(best - fallback) < 0.01 else fallback


def _ensure_one_primary(config_elem):
    """Mutter SIGSEGVs in meta_display_logical_index_to_xinerama_index
    when a <configuration> has no <primary> child anywhere. Promote the
    first <logicalmonitor> if none is marked primary. Returns the count
    of <logicalmonitor> children (callers can refuse to write empty
    blocks)."""
    monitors = config_elem.findall('logicalmonitor')
    if not monitors:
        return 0
    if not any(lm.find('primary') is not None for lm in monitors):
        ET.SubElement(monitors[0], 'primary').text = 'yes'
        # <primary> conventionally precedes <monitor>; reorder so the
        # serialised XML matches what cinnamon-settings writes.
        first = monitors[0]
        primary_node = first.find('primary')
        first.remove(primary_node)
        # Insert after <scale> (which is at index 2: x, y, scale, ...).
        for i, child in enumerate(list(first)):
            if child.tag == 'scale':
                first.insert(i + 1, primary_node)
                break
        else:
            first.insert(0, primary_node)
    return len(monitors)


def write_cinnamon_monitors_xml(splits_dict, xrandr_state, xrandr_config, borders_dict=None):
    """Write ~/.config/cinnamon-monitors.xml describing the monitor set
    muffin will actually see at its next startup.

    Muffin only applies a stored ``<configuration>`` when its monitor
    specs are an EXACT match for the connected monitor set: same
    connector/vendor/product/serial strings for every monitor
    (including disabled ones, via ``<disabled>``), and a ``<mode>``
    whose width/height match exactly with rate within 0.001
    (meta_monitor_mode_spec_equals, muffin 6.6 meta-monitor.c). On any
    mismatch it silently generates a linear fallback config that
    ENABLES EVERY connected output — which is what kept re-enabling a
    deliberately-disabled internal panel on every Cinnamon restart.

    Two consequences drive the shape of this writer:

    - With splits active, Cinnamon runs under the LD_PRELOAD shim, so
      muffin's monitor set is the SYNTHESIZED one: leaf 0 folded into
      the parent connector name (``HDMI-A-0``), later leaves as
      ``HDMI-A-0~1``..; all leaves expose no EDID, so their specs are
      the literal string "unknown" three times (meta_output_parse_edid
      fallback). A parent-only configuration can never match that set.
    - Rates must be computed the way muffin computes them —
      dotClock / (hTotal * vTotal), see _precise_mode_rates() — not the
      2-decimal values xrandr prints.

    History: the first version emitted a "split" block with 1-INDEXED
    fake connectors (``HDMI-0~1..3``); those names matched nothing in
    Mutter's MetaMonitor list, no MetaMonitor got ``is_primary=true``,
    and Cinnamon JS died on ``layoutManager.primaryMonitor`` being
    undefined (2026-04-29, sessions 109 and 145). The replacement
    emitted only parent outputs — which never matched under the shim
    either, so every startup silently took the linear fallback. That
    fallback happened to reproduce a look-alike layout while all
    outputs were meant to be enabled, hiding the mismatch until a
    profile needed an output OFF (2026-07-07). The leaf entries
    written here are 0-indexed with leaf 0 folded to the parent name,
    matching both ``xrandr --setmonitor`` registration and the shim's
    synthesis, and their positions tile without overlap, so neither
    old failure mode applies.
    """
    rates = _precise_mode_rates()
    root = ET.Element('monitors', version='2')
    cfg = ET.SubElement(root, 'configuration')

    for output_name, output_cfg in xrandr_config.outputs.items():
        if not output_cfg.active:
            continue
        output_state = xrandr_state.outputs.get(output_name)
        rate = _precise_rate_for(rates, output_name, output_cfg)
        tree = (splits_dict or {}).get(output_name)
        if tree is not None and not tree.is_leaf:
            # Split output: muffin sees one synthesized monitor per
            # leaf, without EDID ("unknown" specs). Leaf mode timings
            # are copied from the parent's current mode (libXrandr.c
            # fake-mode synthesis), so every leaf inherits the
            # parent's precise rate.
            primary_idx = tree.primary_leaf_index()
            if primary_idx is None and output_cfg.primary:
                primary_idx = 0
            base_x, base_y = output_cfg.position
            regions = tree.leaf_regions(
                output_cfg.size[0], output_cfg.size[1])
            for i, (lx, ly, lw, lh, _w_mm, _h_mm) in enumerate(regions):
                connector = (output_name if i == 0
                             else '%s~%d' % (output_name, i))
                _add_logicalmonitor(
                    cfg, connector,
                    'unknown', 'unknown', 'unknown',
                    base_x + lx, base_y + ly, lw, lh, rate,
                    primary=(i == primary_idx), scale=1
                )
        else:
            edid = output_state.edid_hex if output_state else ""
            vendor, product, serial = _parse_edid_monitorspec(edid)
            _add_logicalmonitor(
                cfg, output_name,
                vendor, product, serial,
                output_cfg.position[0], output_cfg.position[1],
                output_cfg.size[0], output_cfg.size[1], rate,
                primary=output_cfg.primary, scale=1
            )

    count = _ensure_one_primary(cfg)

    if count == 0:
        log.warning(
            "refusing to write monitors.xml: no active outputs "
            "(would produce empty <configuration /> block and crash "
            "Cinnamon at next startup)"
        )
        return

    # Connected-but-disabled outputs must be listed in <disabled> —
    # muffin's stored-config key covers the WHOLE connected set, so a
    # connected monitor that appears nowhere in the configuration
    # makes it unmatchable and re-enables everything via the fallback.
    disabled_elem = None
    disabled_count = 0
    for output_name, output_cfg in xrandr_config.outputs.items():
        if output_cfg.active:
            continue
        output_state = xrandr_state.outputs.get(output_name)
        if not (output_state and output_state.connected):
            continue
        vendor, product, serial = _parse_edid_monitorspec(
            output_state.edid_hex)
        if disabled_elem is None:
            disabled_elem = ET.SubElement(cfg, 'disabled')
        spec = ET.SubElement(disabled_elem, 'monitorspec')
        ET.SubElement(spec, 'connector').text = output_name
        ET.SubElement(spec, 'vendor').text = vendor
        ET.SubElement(spec, 'product').text = product
        ET.SubElement(spec, 'serial').text = serial
        disabled_count += 1

    _indent_xml(root)
    tree_obj = ET.ElementTree(root)
    xml_path = compositor.current().monitors_xml_path
    config_dir = os.path.dirname(xml_path)
    os.makedirs(config_dir, exist_ok=True)
    tmp_path = xml_path + '.tmp'
    tree_obj.write(tmp_path, encoding='unicode', xml_declaration=False)
    os.replace(tmp_path, xml_path)
    log.info("wrote %s (logical monitors=%d, disabled=%d)",
             xml_path, count, disabled_count)


def _add_logicalmonitor(parent, connector, vendor, product, serial,
                        x, y, width, height, rate, primary=False, scale=1):
    """Add a <logicalmonitor> element to a <configuration>."""
    lm = ET.SubElement(parent, 'logicalmonitor')
    ET.SubElement(lm, 'x').text = str(int(x))
    ET.SubElement(lm, 'y').text = str(int(y))
    ET.SubElement(lm, 'scale').text = str(scale)
    if primary:
        ET.SubElement(lm, 'primary').text = 'yes'
    monitor = ET.SubElement(lm, 'monitor')
    monitorspec = ET.SubElement(monitor, 'monitorspec')
    ET.SubElement(monitorspec, 'connector').text = connector
    ET.SubElement(monitorspec, 'vendor').text = vendor
    ET.SubElement(monitorspec, 'product').text = product
    ET.SubElement(monitorspec, 'serial').text = serial
    mode = ET.SubElement(monitor, 'mode')
    ET.SubElement(mode, 'width').text = str(int(width))
    ET.SubElement(mode, 'height').text = str(int(height))
    ET.SubElement(mode, 'rate').text = str(rate)


def _indent_xml(elem, level=0):
    """Add indentation to XML tree for readable output."""
    indent = "\n" + "  " * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = indent + "  "
        if not elem.tail or not elem.tail.strip():
            elem.tail = indent
        for child in elem:
            _indent_xml(child, level + 1)
        if not child.tail or not child.tail.strip():
            child.tail = indent
    else:
        if not elem.tail or not elem.tail.strip():
            elem.tail = indent


def restart_cinnamon_with_fakexrandr(lib_path=None):
    """Restart the shell (Cinnamon/GNOME) with fakexrandr LD_PRELOAD.

    Args:
        lib_path: Path to libXrandr.so.2. If None, auto-detect.
    """
    comp = compositor.current()
    if lib_path is None:
        lib_path = _find_fakexrandr_lib()
    if not lib_path:
        log.warning("fakexrandr library not found, skipping Cinnamon restart")
        return False

    # Keep the lock screen in lockstep: give cinnamon-screensaver the same
    # fakexrandr preload path muffin is about to get, so it sees the split
    # monitors instead of wedging when they change under a lock. (Necessary
    # but NOT sufficient -- see disable_screensaver_lock below.)
    write_screensaver_dbus_override(lib_path)
    # The lock UI is broken on split monitors and falls back to the
    # cs-backup-locker black grab (verified even when monitor rects agree), so
    # the automatic lock must be disabled outright or it traps the user.
    disable_screensaver_lock()

    env = os.environ.copy()
    existing = env.get('LD_PRELOAD', '')
    if lib_path not in existing:
        env['LD_PRELOAD'] = lib_path + (':' + existing if existing else '')

    # Enable fakexrandr's own diagnostic logging so we can post-mortem
    # crashes that happen inside the .so or during Cinnamon's first
    # XRRGetMonitors callback. The path is fixed so post-mortems are
    # easy to locate; existing handlers in libXrandr.c open in append
    # mode, so multiple restarts share the file with timestamps.
    env.setdefault('FAKEXRANDR_LOG', '/tmp/fakexrandr.log')

    # Reap any zombie cinnamon children from previous restarts
    _reap_children()

    # Capture Cinnamon's own stderr — Mutter warnings, JS exceptions,
    # GLib criticals — to a sibling log so a SEGV in JS code is
    # readable post-mortem. Append so multiple restarts in one
    # session don't lose history.
    try:
        cinnamon_log = open('/tmp/cinnamon.log', 'a')
        cinnamon_log.write('\n=== restart_cinnamon_with_fakexrandr at %s ===\n'
                           % time.strftime('%Y-%m-%d %H:%M:%S'))
        cinnamon_log.flush()
    except Exception as e:
        log.warning("could not open /tmp/cinnamon.log: %s", e)
        cinnamon_log = subprocess.DEVNULL

    log.info("restarting %s with LD_PRELOAD=%s FAKEXRANDR_LOG=%s",
             comp.shell_process, env['LD_PRELOAD'], env['FAKEXRANDR_LOG'])
    subprocess.Popen(
        comp.restart_argv, env=env,
        start_new_session=True,
        stdout=cinnamon_log, stderr=cinnamon_log,
    )
    return True


def restart_cinnamon_without_fakexrandr():
    """Restart Cinnamon without LD_PRELOAD to restore normal behavior."""
    env = os.environ.copy()
    # Remove any fakexrandr from LD_PRELOAD
    existing = env.get('LD_PRELOAD', '')
    if existing:
        lib_path = _find_fakexrandr_lib()
        if lib_path:
            parts = [p for p in existing.split(':') if p != lib_path]
            if parts:
                env['LD_PRELOAD'] = ':'.join(parts)
            else:
                env.pop('LD_PRELOAD', None)

    # Reap any zombie cinnamon children from previous restarts
    _reap_children()

    log.info("restarting %s without fakexrandr", compositor.current().shell_process)
    subprocess.Popen(
        compositor.current().restart_argv, env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return True


def _reap_children():
    """Reap any zombie child processes (from previous cinnamon --replace calls)."""
    try:
        while True:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
    except ChildProcessError:
        pass


def ensure_fakexrandr_active(splits_dict, xrandr_state, xrandr_config, borders_dict=None):
    """Write fakexrandr config and restart Cinnamon if needed.

    Call this after applying xrandr settings. If splits are active
    and Cinnamon doesn't have fakexrandr loaded, restarts Cinnamon
    with LD_PRELOAD.
    """
    has_splits = any(
        not tree.is_leaf
        for tree in splits_dict.values()
    )

    lib_path = _find_fakexrandr_lib()
    if not lib_path:
        log.info("fakexrandr library not found, skipping")
        return

    # Always write/update the config
    write_fakexrandr_config(splits_dict, xrandr_state, xrandr_config, borders_dict)

    if has_splits:
        if not is_cinnamon_fakexrandr_loaded():
            log.info("Cinnamon doesn't have fakexrandr loaded, restarting")
            restart_cinnamon_with_fakexrandr(lib_path)
        elif not is_cinnamon_fakexrandr_current():
            log.info("Cinnamon has outdated fakexrandr loaded, restarting")
            restart_cinnamon_with_fakexrandr(lib_path)
        else:
            log.info("Cinnamon already has current fakexrandr loaded, config updated")
    else:
        if is_cinnamon_fakexrandr_loaded():
            log.info("no splits active, restarting Cinnamon without fakexrandr")
            restart_cinnamon_without_fakexrandr()
