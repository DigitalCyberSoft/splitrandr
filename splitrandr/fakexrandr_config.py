# SplitRandR -- fakexrandr binary config writer
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Writes fakexrandr binary configuration and manages Cinnamon restart."""

import os
import struct
import subprocess
import logging
import ctypes
import time
import xml.etree.ElementTree as ET

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
                if 'fakexrandr' not in line:
                    continue
                # Format: addr perms offset dev inode path
                parts = line.split()
                if len(parts) >= 6:
                    path = parts[5]
                    # Only match shared library files, not config files
                    if not (path.endswith('.so') or '.so.' in path
                            or path.endswith('.so (deleted)')):
                        continue
                    # The path may end with " (deleted)" if .so was replaced
                    if path.endswith('(deleted)'):
                        path = path.rsplit(' ', 1)[0].strip()
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
    """Find the fakexrandr libXrandr.so.2 in the splitrandr project tree."""
    # Look relative to this module's location
    module_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(module_dir)
    candidates = [
        os.path.join(project_dir, 'fakexrandr', 'libXrandr.so.2'),
        os.path.join(project_dir, 'fakexrandr', 'libXrandr.so'),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def write_fakexrandr_config(splits_dict, xrandr_state, xrandr_config, borders_dict=None):
    """Write ~/.config/fakexrandr.bin from the current split configuration.

    Binary format (version 2):
        Header: b"FXRD" + <version:4B uint>
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

    return (vendor, product, serial)


MONITORS_XML_PATH = os.path.join(
    os.environ.get('XDG_CONFIG_HOME', os.path.expanduser('~/.config')),
    'cinnamon-monitors.xml'
)


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
    """Write ~/.config/cinnamon-monitors.xml with one configuration block
    that lists ONLY the parent (real) outputs.

    History: earlier versions emitted two ``<configuration>`` blocks —
    a "split" block listing 1-indexed fake-output connectors
    (``HDMI-0~1..3``, ``DP-5~1..3``), and an "unsplit" block listing
    parent outputs (``HDMI-0``, ``DP-5``). On Nvidia tile hardware
    where ``xrandr --setmonitor`` registers RandR 1.5 monitors with
    0-indexed names (``HDMI-0~0..2``, ``DP-5~0..2``), the 1-indexed
    fake names did NOT match Mutter's MetaMonitor list, so Mutter
    applied the split block but produced no MetaMonitor with
    ``is_primary=true``. Cinnamon JS startup then dereferenced
    ``layoutManager.primaryMonitor`` (which becomes ``undefined`` when
    ``global.display.get_primary_monitor()`` returns -1) and
    ``main.js`` threw a TypeError, killing the WM before paint.
    Observed 2026-04-29 sessions 109 and 145.

    The split-block names couldn't simply be switched to 0-indexed
    either, because the split layout's logical-monitor positions
    overlap each other inside a single parent (the splits are
    sub-regions, not independent panels), and Mutter rejects
    overlapping positioned outputs as an invalid configuration.

    Resolution: emit only the parent-output configuration. Window
    snapping and applet placement on the actual splits are still
    handled by:
      - ``xrandr --setmonitor`` (X-server-level RandR 1.5 monitors,
        visible to apps that query ``XRRGetMonitors``);
      - ``libXrandr.so.2`` synthesised fake outputs (visible to apps
        that query ``XRRGetScreenResources``);
      - the ``primary_connector_name`` field in fakexrandr.bin which
        flips ``XRRMonitorInfo.primary`` for the chosen tile.

    cinnamon-monitors.xml only needs to keep Mutter's MetaMonitor
    layout sane at startup so the WM survives long enough for the
    other layers to take effect.
    """
    # Always write monitors.xml with parent-output entries.  The
    # cinnamon LD_PRELOAD restart path was removed 2026-04-29 (see
    # xrandr_save.py for the rationale); without that restart,
    # libXrandr.so.2's fake outputs never reach Cinnamon, so
    # monitors.xml referring to parent connectors is exactly what
    # Mutter expects.
    root = ET.Element('monitors', version='2')
    cfg = ET.SubElement(root, 'configuration')

    for output_name, output_cfg in xrandr_config.outputs.items():
        if not output_cfg.active:
            continue
        output_state = xrandr_state.outputs.get(output_name)
        edid = output_state.edid_hex if output_state else ""
        vendor, product, serial = _parse_edid_monitorspec(edid)
        rate = output_cfg.mode.refresh_rate if output_cfg.mode.refresh_rate else 60.0
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

    _indent_xml(root)
    tree_obj = ET.ElementTree(root)
    config_dir = os.path.dirname(MONITORS_XML_PATH)
    os.makedirs(config_dir, exist_ok=True)
    tmp_path = MONITORS_XML_PATH + '.tmp'
    tree_obj.write(tmp_path, encoding='unicode', xml_declaration=False)
    os.replace(tmp_path, MONITORS_XML_PATH)
    log.info("wrote cinnamon-monitors.xml: %s (parent outputs=%d)",
             MONITORS_XML_PATH, count)


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
    """Restart Cinnamon with fakexrandr LD_PRELOAD.

    Args:
        lib_path: Path to libXrandr.so.2. If None, auto-detect.
    """
    if lib_path is None:
        lib_path = _find_fakexrandr_lib()
    if not lib_path:
        log.warning("fakexrandr library not found, skipping Cinnamon restart")
        return False

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

    log.info("restarting Cinnamon with LD_PRELOAD=%s FAKEXRANDR_LOG=%s",
             env['LD_PRELOAD'], env['FAKEXRANDR_LOG'])
    subprocess.Popen(
        ['cinnamon', '--replace'], env=env,
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

    log.info("restarting Cinnamon without fakexrandr")
    subprocess.Popen(
        ['cinnamon', '--replace'], env=env,
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
