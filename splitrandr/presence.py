# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Output-presence tracking: which physical outputs are connected right
now, and what the active profile looks like with the missing ones removed.

The screen watcher consumes this on every display event, so the query
side is deliberately one subprocess (``xrandr --query``, LD_PRELOAD
stripped) parsed into a small dict plus a fingerprint hash. The
fingerprint lets the watcher classify an event as "nothing
layout-relevant changed" without touching the setmonitor VMs or the
Cinnamon SIGSTOP guard -- RandR delivers plenty of events (output
property spam, other clients' writes) whose headline state is identical
to the last one, and reacting to those used to cost a full VM teardown
plus a multi-second re-apply.

``degrade_profile_data`` is the policy half: given the profile dict and
the set of profile outputs that are currently missing, produce the
layout that should be on screen *until they return* -- the missing
outputs turned off, their splits and borders dropped, the survivors
shifted back to the origin, and primary moved onto a survivor. The
result is never persisted anywhere; the saved profile keeps the full
layout so the return of the hardware restores it.
"""

import copy
import hashlib
import logging
import os
import re
import subprocess

log = logging.getLogger('splitrandr.presence')

# Headline geometry token, e.g. "3840x2160+3840+0". Mode names lack the
# +x+y offsets and don't match.
_GEOM_RE = re.compile(r'(\d+)x(\d+)\+(\d+)\+(\d+)')


def parse_query_output(raw):
    """Parse ``xrandr --query`` text into (fingerprint, outputs).

    ``outputs`` maps output name to::

        {'connected': bool,           # 'connected' or 'unknown-connection'
         'geometry': (w, h, x, y) or None,   # None when no active CRTC
         'primary': bool}

    ``fingerprint`` is a sha1 over the headline lines only -- connection
    state, geometry, primary and physical size. Mode lists are excluded
    on purpose: modes appearing during a DP link train don't change the
    layout, and including them would defeat the no-change fast path.
    Returns ``(None, {})`` when no output headline parsed (garbage or
    empty input is a failed query, not an empty machine).
    """
    outputs = {}
    fp_lines = []
    for line in raw.splitlines():
        if line.startswith(('Screen', ' ', '\t')):
            continue
        line = line.replace('unknown connection', 'unknown-connection')
        parts = line.split()
        if len(parts) < 2 or parts[1] not in (
                'connected', 'disconnected', 'unknown-connection'):
            continue
        geometry = None
        for token in parts[2:]:
            m = _GEOM_RE.match(token)
            if m:
                geometry = tuple(int(g) for g in m.groups())
                break
        outputs[parts[0]] = {
            'connected': parts[1] in ('connected', 'unknown-connection'),
            'geometry': geometry,
            'primary': 'primary' in parts,
        }
        fp_lines.append(line)
    if not outputs:
        return None, {}
    fingerprint = hashlib.sha1('\n'.join(fp_lines).encode()).hexdigest()
    return fingerprint, outputs


def query_output_state(timeout=5):
    """One real-server ``xrandr --query``, parsed via parse_query_output.

    LD_PRELOAD is always stripped (see feedback in xrandr_invoke: xrandr
    must never load the fakexrandr .so). Returns ``(None, {})`` on any
    failure so callers treat an unreadable server the same as an
    unparseable one: don't act on it.
    """
    env = dict(os.environ)
    env.pop('LD_PRELOAD', None)
    try:
        proc = subprocess.run(
            ['xrandr', '--query'],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("xrandr --query failed: %s", e)
        return None, {}
    if proc.returncode != 0:
        log.warning("xrandr --query exit %d: %s",
                    proc.returncode, proc.stderr.strip())
        return None, {}
    return parse_query_output(proc.stdout)


def missing_profile_outputs(profile_data, outputs):
    """Profile-active output names that are absent or disconnected now.

    'unknown-connection' counts as present: it is what some virtual and
    TV-style outputs report while perfectly usable, and treating it as
    missing would degrade layouts that still work.
    """
    missing = []
    for name, out in (profile_data.get('outputs') or {}).items():
        if not out.get('active'):
            continue
        current = outputs.get(name)
        if current is None or not current['connected']:
            missing.append(name)
    return sorted(missing)


def degrade_profile_data(profile_data, missing):
    """Return the profile dict reduced to the outputs that still exist.

    The input dicts are never mutated (the watcher caches the parsed
    profile between events). For each name in ``missing``: the output is
    turned off and its splits and borders dropped. The surviving active
    outputs are translated as a block so the top-left of the arrangement
    sits at the origin -- leaving them where the full profile put them
    would keep a framebuffer-sized dead margin that the cursor and new
    windows can wander into. If the profile's primary was on a missing
    output (or the profile marked none), the largest surviving output by
    mode area becomes primary, name as tie-break: Muffin with no primary
    at all is the documented start of the
    meta_display_logical_index_to_xinerama_index crash chain.
    """
    effective = copy.deepcopy(profile_data)
    gone = set(missing)
    outs = effective.get('outputs') or {}
    for name in gone:
        if name in outs:
            outs[name] = {'active': False, 'primary': False}
    effective['splits'] = {
        name: tree for name, tree in (effective.get('splits') or {}).items()
        if name not in gone
    }
    effective['borders'] = {
        name: border for name, border in
        (effective.get('borders') or {}).items()
        if name not in gone
    }

    actives = {n: o for n, o in outs.items() if o.get('active')}
    if not actives:
        return effective

    min_x = min(o.get('position', [0, 0])[0] for o in actives.values())
    min_y = min(o.get('position', [0, 0])[1] for o in actives.values())
    if min_x or min_y:
        for out in actives.values():
            pos = out.get('position', [0, 0])
            out['position'] = [pos[0] - min_x, pos[1] - min_y]

    if not any(o.get('primary') for o in actives.values()):
        def mode_area(item):
            try:
                w, h = item[1].get('mode', '').split('x')
                return int(w) * int(h)
            except ValueError:
                return 0
        best = sorted(actives.items(),
                      key=lambda item: (-mode_area(item), item[0]))[0][0]
        outs[best]['primary'] = True

    return effective
