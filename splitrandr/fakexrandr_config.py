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
import xml.etree.ElementTree as ET

log = logging.getLogger('splitrandr')


def _get_cinnamon_pid():
    """Get the PID of the cinnamon process."""
    try:
        result = subprocess.run(
            ['pgrep', '-x', 'cinnamon'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            pids = result.stdout.strip().split('\n')
            if pids and pids[0]:
                return int(pids[0])
    except Exception:
        pass
    return None


def is_cinnamon_fakexrandr_loaded():
    """Check if the running Cinnamon process has fakexrandr loaded."""
    pid = _get_cinnamon_pid()
    if not pid:
        return False
    try:
        with open(f'/proc/{pid}/maps', 'r') as f:
            for line in f:
                if 'fakexrandr' in line.lower() or 'libXrandr.so' in line:
                    # Check it's our fakexrandr, not the real libXrandr
                    if 'fakexrandr' in line:
                        return True
    except (OSError, PermissionError):
        pass
    return False

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

    Binary format per entry:
        <length:4B uint><name:128B padded><edid:768B padded>
        <width:4B uint><height:4B uint><split_count:4B uint><border:4B uint><tree_data>

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

        # Pack the entry (without the leading length field)
        name_bytes = output_name.encode('utf-8')[:128].ljust(128, b'\x00')
        edid_bytes = edid_hex.encode('ascii')[:768].ljust(768, b'\x00')
        entry_payload = (
            name_bytes +
            edid_bytes +
            struct.pack('I', width) +
            struct.pack('I', height) +
            struct.pack('I', split_count) +
            struct.pack('I', border) +
            tree_data
        )

        # Prepend the length
        entry = struct.pack('I', len(entry_payload)) + entry_payload
        entries.append(entry)

    config_dir = os.path.dirname(CONFIG_PATH)
    os.makedirs(config_dir, exist_ok=True)

    if entries:
        with open(CONFIG_PATH, 'wb') as f:
            for entry in entries:
                f.write(entry)
        log.info("wrote fakexrandr config: %s (%d entries, %d bytes)",
                 CONFIG_PATH, len(entries), sum(len(e) for e in entries))
    else:
        # No splits active — remove config so fakexrandr passes through
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


def write_cinnamon_monitors_xml(splits_dict, xrandr_state, xrandr_config, borders_dict=None):
    """Write ~/.config/cinnamon-monitors.xml with correct display settings.

    Generates two configuration blocks:
    1. Split layout (fakexrandr active): fake outputs (1-indexed) + real non-split outputs
    2. Unsplit layout: all real outputs only

    This ensures Muffin applies correct positions, modes, and primary
    settings regardless of whether fakexrandr is loaded.
    """
    root = ET.Element('monitors', version='2')

    # --- Configuration 1: Split layout (fakexrandr active) ---
    split_config = ET.SubElement(root, 'configuration')

    for output_name, output_cfg in xrandr_config.outputs.items():
        if not output_cfg.active:
            continue

        output_state = xrandr_state.outputs.get(output_name)
        tree = splits_dict.get(output_name)

        if tree and not tree.is_leaf:
            # This output has splits — generate fake output entries (1-indexed)
            ox, oy = output_cfg.position
            w = output_cfg.size[0]
            h = output_cfg.size[1]
            w_mm = output_state.physical_w_mm if output_state else 0
            h_mm = output_state.physical_h_mm if output_state else 0
            rate = output_cfg.mode.refresh_rate if output_cfg.mode.refresh_rate else 60.0

            regions = list(tree.leaf_regions(w, h, 0, 0, w_mm, h_mm))
            for i, (rx, ry, rw, rh, rmm_w, rmm_h) in enumerate(regions):
                connector = "%s~%d" % (output_name, i)  # 0-indexed, matches setmonitor
                _add_logicalmonitor(
                    split_config, connector,
                    "unknown", "unknown", "unknown",
                    ox + rx, oy + ry, rw, rh, rate,
                    primary=False, scale=1
                )
        else:
            # No splits — add as real output
            edid = output_state.edid_hex if output_state else ""
            vendor, product, serial = _parse_edid_monitorspec(edid)
            rate = output_cfg.mode.refresh_rate if output_cfg.mode.refresh_rate else 60.0
            _add_logicalmonitor(
                split_config, output_name,
                vendor, product, serial,
                output_cfg.position[0], output_cfg.position[1],
                output_cfg.size[0], output_cfg.size[1], rate,
                primary=output_cfg.primary, scale=1
            )

    # --- Configuration 2: Unsplit layout (no fakexrandr) ---
    unsplit_config = ET.SubElement(root, 'configuration')

    for output_name, output_cfg in xrandr_config.outputs.items():
        if not output_cfg.active:
            continue
        output_state = xrandr_state.outputs.get(output_name)
        edid = output_state.edid_hex if output_state else ""
        vendor, product, serial = _parse_edid_monitorspec(edid)
        rate = output_cfg.mode.refresh_rate if output_cfg.mode.refresh_rate else 60.0
        _add_logicalmonitor(
            unsplit_config, output_name,
            vendor, product, serial,
            output_cfg.position[0], output_cfg.position[1],
            output_cfg.size[0], output_cfg.size[1], rate,
            primary=output_cfg.primary, scale=1
        )

    # Write XML
    _indent_xml(root)
    tree_obj = ET.ElementTree(root)
    config_dir = os.path.dirname(MONITORS_XML_PATH)
    os.makedirs(config_dir, exist_ok=True)
    tree_obj.write(MONITORS_XML_PATH, encoding='unicode', xml_declaration=False)
    log.info("wrote cinnamon-monitors.xml: %s", MONITORS_XML_PATH)


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

    log.info("restarting Cinnamon with LD_PRELOAD=%s", env['LD_PRELOAD'])
    subprocess.Popen(
        ['cinnamon', '--replace'], env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
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

    log.info("restarting Cinnamon without fakexrandr")
    subprocess.Popen(
        ['cinnamon', '--replace'], env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return True


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
        else:
            log.info("Cinnamon already has fakexrandr loaded, config updated")
    else:
        if is_cinnamon_fakexrandr_loaded():
            log.info("no splits active, restarting Cinnamon without fakexrandr")
            restart_cinnamon_without_fakexrandr()
