# SplitRandR -- Window position snapshot/restore
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Snapshot and restore window geometry around disruptive events.

Used by:
- the apply pipeline (Cinnamon restart with fakexrandr moves windows when
  monitor sizes change)
- the screen watcher (sleep/wake and screensaver lock can re-arrange
  windows on some systems)

Backed by `wmctrl -lG` (read) and `wmctrl -ir <id> -e g,x,y,w,h` (write).
Window IDs are stable across Cinnamon restart because the X server keeps
them; clients aren't destroyed when only the WM respawns.
"""

import logging
import os
import re
import subprocess
import time

log = logging.getLogger('splitrandr.window_layout')


def _wmctrl_env():
    """Build env for wmctrl that points at the user's X session."""
    env = os.environ.copy()
    env.setdefault('DISPLAY', ':0')
    if 'XAUTHORITY' not in env:
        # Best-effort: lightdm path, else don't override.
        for p in ('/run/lightdm/user/xauthority',
                  os.path.expanduser('~/.Xauthority')):
            if os.path.exists(p):
                env['XAUTHORITY'] = p
                break
    return env


def capture():
    """Return a list of dicts {id, x, y, w, h, desktop, title} for every
    managed window currently visible. Empty list on failure."""
    try:
        proc = subprocess.run(
            ['wmctrl', '-lG'],
            capture_output=True, text=True, timeout=5, env=_wmctrl_env(),
        )
    except Exception as e:
        log.warning("wmctrl -lG failed: %s", e)
        return []

    snapshot = []
    for line in proc.stdout.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 7:
            continue
        wid, desktop, x, y, w, h = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
        try:
            entry = {
                'id': wid,
                'desktop': int(desktop),
                'x': int(x), 'y': int(y),
                'w': int(w), 'h': int(h),
                'title': parts[7] if len(parts) >= 8 else '',
            }
        except ValueError:
            continue
        # Skip pseudo-windows (desktop -1) and root/sticky decorations.
        if entry['desktop'] < 0:
            continue
        snapshot.append(entry)

    log.info("captured %d windows", len(snapshot))
    return snapshot


def _move(wid, x, y, w, h):
    """Move + resize a window via wmctrl. Returns True on success."""
    geom = "0,%d,%d,%d,%d" % (x, y, w, h)
    try:
        result = subprocess.run(
            ['wmctrl', '-ir', wid, '-e', geom],
            capture_output=True, text=True, timeout=3, env=_wmctrl_env(),
        )
        return result.returncode == 0
    except Exception:
        return False


def restore(snapshot, settle_delay=0.0):
    """Move each window back to the saved geometry. Skips windows that no
    longer exist (closed) and ones whose geometry already matches."""
    if not snapshot:
        return

    if settle_delay > 0:
        time.sleep(settle_delay)

    # Re-query current state to skip no-op moves.
    current = {w['id']: w for w in capture()}

    moved = 0
    skipped = 0
    missing = 0
    for entry in snapshot:
        wid = entry['id']
        cur = current.get(wid)
        if cur is None:
            missing += 1
            continue
        if (cur['x'], cur['y'], cur['w'], cur['h']) == (
                entry['x'], entry['y'], entry['w'], entry['h']):
            skipped += 1
            continue
        if _move(wid, entry['x'], entry['y'], entry['w'], entry['h']):
            moved += 1

    log.info("restored windows: moved=%d skipped=%d missing=%d",
             moved, skipped, missing)
