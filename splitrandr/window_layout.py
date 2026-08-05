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


def _num_desktops():
    """Number of virtual desktops, from ``wmctrl -d``. 0 on failure."""
    try:
        proc = subprocess.run(['wmctrl', '-d'], capture_output=True, text=True,
                              timeout=5, env=_wmctrl_env())
    except Exception:
        return 0
    return len([l for l in proc.stdout.splitlines() if l.strip()])


def _set_desktop(wid, desktop):
    """Move a window to a virtual desktop. Changes no geometry whatsoever."""
    try:
        result = subprocess.run(
            ['wmctrl', '-ir', wid, '-t', str(desktop)],
            capture_output=True, text=True, timeout=3, env=_wmctrl_env(),
        )
        return result.returncode == 0
    except Exception:
        return False


def _xrefresh():
    """Repaint the root window, generating Expose on every mapped window.

    Cheapest possible nudge: touches no geometry, no state, no stacking. Only
    helps if the client simply hasn't redrawn; it cannot fix a stale pixmap
    held by the compositor.
    """
    try:
        result = subprocess.run(['xrefresh'], capture_output=True, text=True,
                                timeout=10, env=_wmctrl_env())
        return result.returncode == 0
    except Exception:
        return False


def _resize_only(wid, w, h):
    """Set a window's size without touching its position.

    ``wmctrl -e`` takes ``gravity,x,y,w,h`` and treats -1 as "leave alone", so
    passing -1 for x and y writes no coordinates. That matters because
    ``wmctrl -lG`` positions are unreliable here (a window xwininfo places at
    3840,648 is reported at 7680,1296), while its *sizes* agree with xwininfo
    and are safe to use.
    """
    try:
        result = subprocess.run(
            ['wmctrl', '-ir', wid, '-e', '0,-1,-1,%d,%d' % (w, h)],
            capture_output=True, text=True, timeout=3, env=_wmctrl_env(),
        )
        return result.returncode == 0
    except Exception:
        return False


def _size_increments(wid):
    """(width_inc, height_inc) from WM_NORMAL_HINTS, defaulting to (1, 1).

    Terminals declare character-cell increments. A sub-cell resize gets
    snapped down by the WM and the original size is never restored (measured
    on xterm: 259x134 -> 253x121), so the nudge delta must be a whole number
    of increments.
    """
    try:
        proc = subprocess.run(
            ['xprop', '-id', wid, 'WM_NORMAL_HINTS'],
            capture_output=True, text=True, timeout=3, env=_wmctrl_env(),
        )
    except Exception:
        return (1, 1)
    m = re.search(r'resize increment:\s*(\d+)\s*by\s*(\d+)', proc.stdout)
    if not m:
        return (1, 1)
    return (max(1, int(m.group(1))), max(1, int(m.group(2))))


def _wm_states(wid):
    """Set of _NET_WM_STATE_* suffixes on a window, lowercased."""
    try:
        proc = subprocess.run(
            ['xprop', '-id', wid, '_NET_WM_STATE'],
            capture_output=True, text=True, timeout=3, env=_wmctrl_env(),
        )
    except Exception:
        return set()
    if proc.returncode != 0:
        return set()
    return {m.lower() for m in re.findall(r'_NET_WM_STATE_(\w+)', proc.stdout)}


def _set_state(wid, action, states):
    """wmctrl -b <add|remove>,<state>[,<state>]."""
    if not states:
        return False
    try:
        result = subprocess.run(
            ['wmctrl', '-ir', wid, '-b', '%s,%s' % (action, ','.join(states))],
            capture_output=True, text=True, timeout=3, env=_wmctrl_env(),
        )
        return result.returncode == 0
    except Exception:
        return False


# A maximized or fullscreen window ignores an explicit resize, so for those the
# state itself is toggled off and on -- which IS a real resize both ways and
# leaves the window in its original state.
_TOGGLE_STATES = ('maximized_vert', 'maximized_horz', 'fullscreen')


def nudge_repaint(settle_delay=0.25, snapshot=None, mode='resize'):
    """Force windows to redraw after the displays drop out and come back.

    Why this exists: when the monitors lose power (or a DP link drops), apps
    keep showing a stale image and never repaint. Dragging a window to another
    monitor and back fixes it by hand; this does the equivalent without the
    manual work.

    This is NOT what :func:`fakexrandr_config.nudge_gtk_monitor_refresh` does.
    That fires an RROutputPropertyNotify so GTK re-enumerates its GdkMonitor
    list, fixing popups placed on the wrong screen -- it does not make an app
    redraw its contents. :func:`restore` cannot do it either: it deliberately
    skips windows whose geometry already matches, which after a display return
    is all of them.

    ``mode='resize'`` (default)
        Grow each window, then set it back to its exact original size. This is
        the mechanism the user established by hand: resizing a stale window is
        what makes it paint again, because Chrome allocates a new output
        surface on resize and Swing does a full revalidate. The affected
        clients are still live and hit-testable throughout -- the mouse reacts
        to widgets that aren't painted -- so this is a stale drawing surface,
        not a dead window, and only a resize reliably replaces it.

        Grows rather than shrinks: shrinking below a size increment gets
        snapped down by the WM and the original size is never recovered.
        Maximized and fullscreen windows ignore an explicit resize, so their
        state is toggled off and on instead.

    ``mode='remap'``
        Bounce each window off a spare virtual desktop. Geometry-neutral and
        idempotent (verified byte-identical across repeated passes), but
        MEASURED INEFFECTIVE for this symptom: unmap/map cycles X visibility
        without making the client rebuild its drawing surface. Kept only
        because it is the least invasive thing that still forces a map cycle.

    ``mode='refresh'``
        One ``xrefresh`` call. Zero risk -- no geometry, no state, no stacking
        -- but it only helps when the client merely needs an Expose, which is
        not this bug.

    Mechanisms measured and rejected, so they don't get retried:

    - Shrink by 1px and back -- size-hinted windows snap to cell multiples and
      never regain the original size (measured on xterm: 259x134 -> 253x121).
    - ``xdotool windowmove --relative`` by +1/-1 -- reads the client origin but
      writes the frame origin, leaking the titlebar height every pass
      (measured y 337 -> 375 -> 413 -> 451).
    - ``wmctrl -e`` round-trip of captured coordinates -- ``wmctrl -lG``
      positions disagree with xwininfo (a window at 3840,648 is reported at
      7680,1296), so the first application displaces the window (300,451 ->
      310,514); stable afterwards, but the initial jump is unacceptable.
    - Detecting which windows need it by sampling their rendered content --
      unreliable. A stale window holds an old painted frame, which measures as
      normal content, so broken windows look healthy. Nudge everything; it is
      harmless on windows that are already painting.

    Returns the number of windows nudged (or 1 for a successful 'refresh').
    """
    if mode == 'refresh':
        ok = _xrefresh()
        log.info("xrefresh nudge %s", "sent" if ok else "FAILED")
        return 1 if ok else 0

    if mode not in ('resize', 'remap'):
        raise ValueError("unknown nudge mode: %r" % (mode,))

    if mode == 'resize':
        return _nudge_by_resize(settle_delay, snapshot)

    wins = snapshot if snapshot is not None else capture()
    if not wins:
        return 0

    ndesk = _num_desktops()
    if ndesk < 2:
        log.warning("only %d desktop(s); cannot remap-nudge, "
                    "falling back to xrefresh", ndesk)
        return nudge_repaint(mode='refresh')

    # Phase 1: park every window on a desktop that isn't its own.
    parked = []
    for entry in wins:
        home = entry['desktop']
        spare = (home + 1) % ndesk
        if _set_desktop(entry['id'], spare):
            parked.append(entry)

    if not parked:
        return 0

    # One settle for the whole batch, so clients process the unmap before we
    # map them again -- otherwise the pair can collapse into no visible change.
    if settle_delay > 0:
        time.sleep(settle_delay)

    # Phase 2: send each window home. Geometry is untouched throughout.
    for entry in parked:
        _set_desktop(entry['id'], entry['desktop'])

    log.info("remap-nudged %d windows for repaint", len(parked))
    return len(parked)


def _nudge_by_resize(settle_delay=0.25, snapshot=None):
    """Grow every window then restore its exact size, forcing a repaint.

    Batched: all windows are grown, then one settle, then all restored. The
    clients need to process the first ConfigureNotify before the second, or the
    pair can be coalesced into no resize at all.
    """
    wins = snapshot if snapshot is not None else capture()
    if not wins:
        return 0

    grown = []
    for entry in wins:
        wid = entry['id']
        toggled = [s for s in _TOGGLE_STATES if s in _wm_states(wid)]
        if toggled:
            # Maximized/fullscreen: an explicit resize is ignored, but dropping
            # and re-adding the state resizes it twice and ends where it began.
            if _set_state(wid, 'remove', toggled):
                grown.append((entry, toggled))
            continue
        winc, hinc = _size_increments(wid)
        # Grow by whole increments so the restore lands exactly. 20px is enough
        # to be a real resize rather than something the toolkit coalesces away.
        dw = winc * max(1, -(-20 // winc))
        dh = hinc * max(1, -(-20 // hinc))
        if _resize_only(wid, entry['w'] + dw, entry['h'] + dh):
            grown.append((entry, None))

    if not grown:
        return 0

    if settle_delay > 0:
        time.sleep(settle_delay)

    for entry, toggled in grown:
        if toggled:
            _set_state(entry['id'], 'add', toggled)
        else:
            _resize_only(entry['id'], entry['w'], entry['h'])

    log.info("resize-nudged %d windows for repaint", len(grown))
    return len(grown)


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
