# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Automated recovery from the post-power-loss muffin wedge.

After a wall-power outage takes both monitors down and back, muffin can
come up with an EMPTY logical monitor list even though every X-side
check passes (outputs connected, setmonitor VMs registered): setmonitor
writes are protocol-silent, so the output drop is the last event muffin
processed. Cinnamon then composites nothing -- black screen, no panel,
sometimes an unlockable lock screen -- until the shell is restarted.

The sequence implemented here is the manually validated one that
recovered five consecutive incidents (2026-08-05/-06/-09/-12/-14),
including its hard-won variants:

  (a) lock-trap: screensaver active while the shell is wedged means the
      unlock UI can never render; deactivate CLEANLY over D-Bus first.
      Never kill cs-backup-locker -- that orphans pam-helpers and causes
      the 2026-07-25 style lockout.
  (b) orphaned XI2 input grab: after recovery the shell can hold
      pointer+keyboard grabs with no modal to justify them; nothing
      in-process releases them, only another shell restart does.
  (c) dropped output-bound tiles: the outage can delete the virtual
      monitors bound to physical outputs; a post-restart --apply
      rebuilds them (mandatory, and harmless when already correct).
  (d) --replace standoff: a wedged old cinnamon can refuse to yield the
      WM selection; both PIDs run and org.Cinnamon vanishes from D-Bus.
      TERM (then KILL) the old PID.

Plus the SHIELD: gnome-terminal-server (and any other splitrandr GTK
process) is SIGSTOPped across the WM swap. On 2026-08-01 an unshielded
restart SEGV'd gnome-terminal-server -- GTK dereferenced a GDK input
device the swap invalidated -- killing 29 tabs at once. Frozen, the
process buffers the X event churn and wakes to a coherent world; the
shield preserved every tab across five validated runs. The splitrandr
watcher itself died the same way on 2026-08-06, which is why this
module must run in a dedicated NON-GTK process (spawned detached by
save_to_x's wedge check, by the sentinel timer, or by hand via
``--recover-shell``).

Why the previous in-tree recovery was wrong: it spawned ``cinnamon
--replace`` and then polled Eval on the org.Cinnamon well-known name --
which the OLD wedged shell still owned and happily answered. It logged
"recovered" 5 milliseconds after spawning while the shell still
reported zero monitors. The only honest readiness signal is
cinnamon_shell_health() reporting monitors > 0, combined with standoff
resolution. And even then: the final verdict belongs to the user's
eyes, not any probe -- log claims accordingly.
"""

import fcntl
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time

log = logging.getLogger('splitrandr.recovery')

# One automatic recovery per cooldown window. Prevents a restart storm
# when the shell cannot come up at all (e.g. GPU wedged): each restart
# of Cinnamon is disruptive, so a broken recovery must not loop.
AUTO_COOLDOWN_SECS = 600

# Detached failsafe that SIGCONTs every shielded pid even if this
# process dies mid-recovery. CONT on an already-running process is a
# no-op, so firing late is harmless.
FAILSAFE_CONT_SECS = 300

XORG_LOG = '/var/log/Xorg.0.log'


def _runtime_dir():
    return os.environ.get('XDG_RUNTIME_DIR') or '/tmp'


def _recovery_lock_path():
    return os.path.join(_runtime_dir(), 'splitrandr', 'recovery.lock')


def _state_path():
    base = os.environ.get('XDG_STATE_HOME') or os.path.expanduser(
        '~/.local/state')
    return os.path.join(base, 'splitrandr', 'recovery.json')


def _load_state():
    try:
        with open(_state_path()) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save_state(state):
    path = _state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("could not save recovery state: %s", e)


def _pidof(name):
    """Exact-program-name pid lookup. pidof matches the running program's
    name, unlike pgrep whose comm matching truncates at 15 chars and whose
    -f patterns match the calling shell itself."""
    try:
        out = subprocess.run(['pidof', name], capture_output=True, text=True,
                             timeout=5)
        return [int(p) for p in out.stdout.split()]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def _comm(pid):
    try:
        with open('/proc/%d/comm' % pid) as f:
            return f.read().strip()
    except OSError:
        return None


def adopt_session_env():
    """Fill DISPLAY/XAUTHORITY/DBUS_SESSION_BUS_ADDRESS/XDG_RUNTIME_DIR
    from a live session process when missing (systemd --user timers don't
    carry them). cinnamon-session is tried first because it survives shell
    crashes."""
    wanted = ('DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS',
              'XDG_RUNTIME_DIR')
    if all(os.environ.get(k) for k in ('DISPLAY',
                                       'DBUS_SESSION_BUS_ADDRESS')):
        return True
    # cinnamon-session-binary is the Fedora/Mint process name; plain
    # cinnamon-session kept for other packagings.
    for name in ('cinnamon-session-binary', 'cinnamon-session', 'cinnamon',
                 'csd-xsettings', 'gnome-terminal-server'):
        for pid in _pidof(name):
            try:
                with open('/proc/%d/environ' % pid, 'rb') as f:
                    raw = f.read()
            except OSError:
                continue
            for kv in raw.split(b'\0'):
                if b'=' not in kv:
                    continue
                k, v = kv.split(b'=', 1)
                k = k.decode('utf-8', 'replace')
                if k in wanted and not os.environ.get(k):
                    os.environ[k] = v.decode('utf-8', 'replace')
            if all(os.environ.get(k) for k in ('DISPLAY',
                                               'DBUS_SESSION_BUS_ADDRESS')):
                log.info("adopted session environment from %s (pid %d)",
                         name, pid)
                return True
    return all(os.environ.get(k) for k in ('DISPLAY',
                                           'DBUS_SESSION_BUS_ADDRESS'))


def _connected_outputs():
    """Count physically connected outputs, bypassing the shim. Zero means
    the outage is still in progress -- restarting the shell then would be
    churn for nothing; the wedge only becomes fixable once outputs are
    back."""
    env = os.environ.copy()
    env.pop('LD_PRELOAD', None)
    try:
        out = subprocess.run(['xrandr', '--query'], capture_output=True,
                             text=True, timeout=10, env=env)
        return len(re.findall(r'^\S+ connected', out.stdout, re.M))
    except (OSError, subprocess.TimeoutExpired):
        return 0


def _screensaver_call(method, *args):
    cmd = ['gdbus', 'call', '--session',
           '--dest', 'org.cinnamon.ScreenSaver',
           '--object-path', '/org/cinnamon/ScreenSaver',
           '--method', 'org.cinnamon.ScreenSaver.' + method]
    cmd += list(args)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return None
        return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None


def _screensaver_active():
    return _screensaver_call('GetActive') == '(true,)'


def _deactivate_screensaver():
    """Variant (a) lock-trap: with the shell wedged the unlock UI can never
    render, so a locked screen is a lockout. Deactivate cleanly over D-Bus
    -- the locker exits on its own. NEVER kill cs-backup-locker or the
    pam-helper directly."""
    log.warning("screensaver is active while the shell is wedged (lock-trap)"
                " -- deactivating cleanly via SetActive(false)")
    _screensaver_call('SetActive', 'false')
    deadline = time.time() + 15
    while time.time() < deadline:
        if not _screensaver_active():
            log.info("screensaver deactivated")
            return True
        time.sleep(0.5)
    log.warning("screensaver still active after SetActive(false); "
                "continuing anyway (it may release once the shell is back)")
    return False


def _gui_pid():
    """PID of the running splitrandr GUI/watcher from the singleton lock
    file, or None. Verified against /proc so a recycled pid doesn't count."""
    from .gui_lock import _lock_path
    try:
        with open(_lock_path()) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    if pid <= 0 or pid == os.getpid():
        return None
    try:
        with open('/proc/%d/cmdline' % pid, 'rb') as f:
            cmdline = f.read().decode('utf-8', 'replace')
    except OSError:
        return None
    return pid if 'splitrandr' in cmdline else None


class _Shield:
    """SIGSTOP the GTK processes a WM swap can SEGV; SIGCONT them after.

    Scope is deliberately exact: gnome-terminal-server (29 tabs lost on
    2026-08-01 without it) and the splitrandr GUI/watcher (died the same
    way on 2026-08-06). A detached failsafe CONTs them even if this
    process dies mid-recovery.
    """

    def __init__(self):
        self._stopped = []

    def stop(self):
        pids = list(_pidof('gnome-terminal-server'))
        gui = _gui_pid()
        if gui:
            pids.append(gui)
        if not pids:
            return
        try:
            subprocess.Popen(
                ['sh', '-c', 'sleep %d; kill -CONT %s 2>/dev/null' %
                 (FAILSAFE_CONT_SECS, ' '.join(str(p) for p in pids))],
                start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            log.warning("could not arm SIGCONT failsafe: %s", e)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGSTOP)
                self._stopped.append(pid)
            except (ProcessLookupError, PermissionError) as e:
                log.warning("could not shield pid %d: %s", pid, e)
        log.info("shielded %d GTK process(es) with SIGSTOP: %s",
                 len(self._stopped), self._stopped)

    def cont(self):
        for pid in self._stopped:
            try:
                os.kill(pid, signal.SIGCONT)
            except (ProcessLookupError, PermissionError):
                pass
        if self._stopped:
            log.info("unshielded: SIGCONT sent to %s", self._stopped)
        self._stopped = []


def _wait_new_shell_pid(old_pids, timeout=25.0):
    old = set(old_pids)
    deadline = time.time() + timeout
    while time.time() < deadline:
        for pid in _pidof('cinnamon'):
            if pid not in old:
                return pid
        time.sleep(0.5)
    return None


def _resolve_standoff(old_pids, new_pid, grace=8.0):
    """Variant (d): a wedged old cinnamon can refuse to yield the WM
    selection to the --replace; both run in parallel and org.Cinnamon
    vanishes from D-Bus. Give it a grace period to yield on its own
    (it did on 2026-08-14), then TERM, then KILL."""
    deadline = time.time() + grace
    while time.time() < deadline:
        alive = [p for p in old_pids if _comm(p) == 'cinnamon']
        if not alive:
            return
        time.sleep(0.5)
    for pid in old_pids:
        if _comm(pid) != 'cinnamon':
            continue
        log.warning("old cinnamon (pid %d) did not yield to the new one "
                    "(pid %d) -- SIGTERM (--replace standoff)", pid, new_pid)
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
    deadline = time.time() + 5
    while time.time() < deadline:
        if not any(_comm(p) == 'cinnamon' for p in old_pids):
            return
        time.sleep(0.5)
    for pid in old_pids:
        if _comm(pid) == 'cinnamon':
            log.warning("old cinnamon (pid %d) survived SIGTERM -- SIGKILL",
                        pid)
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def _wait_shell_healthy(timeout=60.0):
    """The honest readiness signal: the shell itself reporting a non-empty
    logical monitor list. D-Bus presence is NOT it -- the old wedged shell
    answers Eval right up until it yields (that false signal is how the
    previous recovery logged success in 5ms while doing nothing)."""
    from . import compositor
    if not compositor.current().supports_eval:
        # No Eval (GNOME): D-Bus readiness is the best signal available.
        from .cinnamon_compat import _wait_cinnamon_on_dbus
        return _wait_cinnamon_on_dbus(timeout=timeout)
    from .cinnamon_compat import cinnamon_shell_health
    deadline = time.time() + timeout
    health = None
    while time.time() < deadline:
        health = cinnamon_shell_health()
        if health and health.get('monitors', 0) > 0:
            log.info("shell reports %s monitor(s), %s panel(s)",
                     health.get('monitors'), health.get('panels'))
            if health.get('panels', 0) < 1:
                panel_deadline = time.time() + 10
                while time.time() < panel_deadline:
                    health = cinnamon_shell_health() or health
                    if health.get('panels', 0) > 0:
                        break
                    time.sleep(1)
                if health.get('panels', 0) < 1:
                    log.warning("shell has monitors but still zero panels; "
                                "the post-restart apply's panel pin should "
                                "correct it")
            return True
        time.sleep(1)
    log.error("shell never reported a non-empty monitor list within %.0fs "
              "(last health: %s)", timeout, health)
    return False


def _default_restart():
    from .fakexrandr_config import (
        _find_fakexrandr_lib, restart_cinnamon_with_fakexrandr,
    )
    lib_path = _find_fakexrandr_lib()
    if not lib_path:
        log.error("fakexrandr library not found; cannot restart the shell "
                  "with the shim")
        return False
    return restart_cinnamon_with_fakexrandr(lib_path)


def _restart_shell_cycle(restart_fn=None):
    """One spawn + standoff resolution + health wait. Returns True when the
    shell reports monitors > 0. ``restart_fn`` performs the spawn (default:
    with the fakexrandr shim)."""
    old_pids = _pidof('cinnamon')
    if not (restart_fn or _default_restart)():
        return False
    new_pid = _wait_new_shell_pid(old_pids)
    if new_pid is None:
        log.error("no new cinnamon process appeared after the restart spawn")
        return False
    log.info("new cinnamon spawned (pid %d), old: %s", new_pid, old_pids)
    if old_pids:
        _resolve_standoff(old_pids, new_pid)
    return _wait_shell_healthy()


def restart_shell_shielded(restart_fn):
    """A PLANNED shell restart (split topology or monitors.xml changed) with
    the same protections as recovery: SIGSTOP shield on gnome-terminal-server
    (and the GUI, unless we are it), standoff resolution, and a real health
    wait instead of polling a bus name the old shell still answers. Returns
    True when the new shell reports monitors > 0."""
    shield = _Shield()
    shield.stop()
    try:
        return _restart_shell_cycle(restart_fn)
    finally:
        shield.cont()


def _apply_layout():
    """Variant (c): the outage can delete the output-bound virtual monitors
    (raw output rows in --listmonitors). Re-applying the saved layout
    rebuilds them, re-pins the panel against the FRESH shell's index, and
    nudges GTK clients. Harmless when the tiles are already correct."""
    try:
        from .gui import Application
        from .gui_cli import _apply_config
        json_path = Application.LAYOUT_JSON
        if not os.path.exists(json_path):
            log.warning("no layout config at %s; skipping tile rebuild",
                        json_path)
            return
        _apply_config(json_path)
    except SystemExit:
        log.error("layout apply refused to run (see above)")
    except Exception:
        log.exception("post-restart layout apply failed")


def _eval_modal_state():
    from .cinnamon_compat import _cinnamon_eval
    raw = _cinnamon_eval(
        '(function(){var g=0;'
        ' try{g=global.display.get_grab_op();}catch(e){}'
        ' return {modals: Main.modalCount||0, grab_op: g};})()')
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def _dump_active_grabs():
    """Trigger the server's grab dump and return [(device, pid), ...] for
    each ACTIVE grab, or None when the probe isn't possible.

    XF86LogGrabInfo appends the dump to Xorg.0.log; only the freshly
    appended tail is read, and only the section between "Printing all
    currently active device grabs:" and "End list of active device
    grabs" counts. The much longer "registered grabs" section below it
    lists every client's passive hotkey grabs and must be ignored --
    parsing it makes an idle healthy desktop look like a grab storm.
    """
    if not shutil.which('xdotool'):
        return None
    try:
        offset = os.stat(XORG_LOG).st_size
    except OSError:
        return None
    try:
        subprocess.run(['xdotool', 'key', 'XF86LogGrabInfo'],
                       capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    time.sleep(0.8)
    try:
        with open(XORG_LOG, 'rb') as f:
            f.seek(offset)
            tail = f.read(512 * 1024).decode('utf-8', 'replace')
    except OSError:
        return None
    start = tail.find('Printing all currently active device grabs')
    end = tail.find('End list of active device grabs')
    if start == -1 or end == -1 or end < start:
        return None
    active = tail[start:end]
    return [(dev, int(pid)) for dev, pid in
            re.findall(r"on device '([^']+)'.*?client pid (\d+)",
                       active, re.S)]


def _stuck_grab_check():
    """Variant (b): after recovery the shell can hold pointer+keyboard
    grabs with modals=0 and no grab op -- screen fine, nothing clickable,
    and nothing in-process will release them (confirmed: end_modal,
    end_grab_op, Escape, XF86Ungrab are all no-ops; AllowDeactivateGrabs
    is off). Only another restart frees them. Returns True only when the
    pathological state is seen twice, 5s apart -- a transient startup
    grab that clears within seconds is normal."""
    for probe in (1, 2):
        grabs = _dump_active_grabs()
        if grabs is None:
            log.info("grab check not possible (xdotool or %s unavailable); "
                     "skipping", XORG_LOG)
            return False
        cinnamon = set(_pidof('cinnamon'))
        held = [dev for dev, pid in grabs if pid in cinnamon]
        has_pointer = any('pointer' in dev.lower() for dev in held)
        has_keyboard = any('keyboard' in dev.lower() for dev in held)
        if not (has_pointer and has_keyboard):
            return False
        modal = _eval_modal_state()
        if modal is None or modal.get('modals', 0) or modal.get('grab_op', 0):
            return False
        if probe == 1:
            log.info("cinnamon holds active grabs on %s with no modal; "
                     "re-checking in 5s before treating it as stuck", held)
            time.sleep(5)
    log.warning("cinnamon holds pointer+keyboard grabs with modals=0 and no "
                "grab op across two probes -- the orphaned-grab variant; "
                "only a shell restart releases them")
    return True


def _nudge_repaint_twice():
    """Windows keep stale surfaces after the swap. Two passes with a
    settle between: one pass right after a restart can race the fresh
    compositor (learned 2026-08-09)."""
    from . import window_layout
    for attempt in (1, 2):
        try:
            n = window_layout.nudge_repaint(mode='resize')
            log.info("repaint nudge pass %d/2: %d windows", attempt, n)
        except Exception:
            log.exception("repaint nudge pass %d failed", attempt)
        if attempt == 1:
            time.sleep(5)


def _watcher_launch_command():
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cmd = '%s -m splitrandr' % sys.executable
    if 'site-packages' not in pkg_parent and 'dist-packages' not in pkg_parent:
        cmd = 'env PYTHONPATH=%s %s' % (pkg_parent, cmd)
    return cmd


def relaunch_watcher_gui():
    """Resurrect the splitrandr GUI/watcher as Cinnamon's child so it keeps
    logind session membership (a setsid launch gets NoSessionForPID and
    loses the PrepareForSleep + session Lock/Unlock subscriptions)."""
    from .cinnamon_compat import _cinnamon_eval
    cmd = _watcher_launch_command()
    result = _cinnamon_eval('Util.spawnCommandLine(%s)' % json.dumps(cmd))
    if result is None:
        log.warning("could not relaunch the watcher via Cinnamon Eval")
        return False
    log.info("relaunched splitrandr watcher session-attached: %s", cmd)
    return True


def spawn_recovery(reason=''):
    """Fire-and-forget a dedicated recovery process. Called from
    save_to_x's wedge check: recovery must NOT run inside the GTK
    watcher, because the WM swap it performs can SEGV its own host
    process mid-sequence (that is how the watcher died on 2026-08-06)."""
    env = os.environ.copy()
    env['SPLITRANDR_AUTO_RECOVERY'] = '1'
    env.pop('SPLITRANDR_IN_RECOVERY', None)
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if ('site-packages' not in pkg_parent
            and 'dist-packages' not in pkg_parent):
        existing = env.get('PYTHONPATH', '')
        if pkg_parent not in existing.split(':'):
            env['PYTHONPATH'] = pkg_parent + (
                ':' + existing if existing else '')
    try:
        proc = subprocess.Popen(
            [sys.executable, '-m', 'splitrandr', '--recover-shell'],
            env=env, start_new_session=True, cwd='/',
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        log.error("could not spawn shell-recovery helper: %s", e)
        return None
    log.warning("spawned detached shell-recovery helper (pid %d, reason: %s)"
                " -- it will restart the shell, verify health, rebuild "
                "tiles, and repaint", proc.pid, reason)
    return proc.pid


def recover(auto=False, reason=''):
    """Run the full validated recovery sequence. Returns a verdict string.

    auto=True applies the cooldown (one automatic recovery per 10
    minutes); manual runs skip it because a human asked.
    """
    # Any apply we run below must not spawn yet another recovery from its
    # own wedge check.
    os.environ['SPLITRANDR_IN_RECOVERY'] = '1'
    adopt_session_env()

    # Python's default SIGTERM disposition exits without running finally
    # blocks -- which would leave the shield's SIGSTOPped processes frozen
    # until the detached failsafe fires. Convert to SystemExit so cleanup
    # runs if something (systemd, a user) terminates a recovery mid-flight.
    try:
        signal.signal(signal.SIGTERM,
                      lambda signum, frame: sys.exit(143))
    except ValueError:
        pass  # not the main thread; keep the default

    lock_path = _recovery_lock_path()
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        log.warning("cannot open recovery lock (%s); continuing unlocked", e)
        lock_fd = None
    if lock_fd is not None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(lock_fd)
            log.info("another recovery is already running; not starting a "
                     "second one")
            return 'already-running'

    try:
        return _recover_locked(auto=auto, reason=reason)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


def _recover_locked(auto, reason):
    from .cinnamon_compat import cinnamon_shell_health

    health = cinnamon_shell_health()
    cinnamon_pids = _pidof('cinnamon')
    log.info("=== shell recovery starting (auto=%s, reason: %s) === "
             "health=%s cinnamon=%s", auto, reason, health, cinnamon_pids)

    if health and health.get('monitors', 0) > 0:
        log.info("shell reports %d monitor(s) -- healthy, nothing to "
                 "recover", health.get('monitors'))
        return 'healthy'
    if (not cinnamon_pids and not _pidof('cinnamon-session-binary')
            and not _pidof('cinnamon-session')):
        log.info("no graphical session is running; nothing to recover")
        return 'no-session'

    connected = _connected_outputs()
    if connected == 0:
        log.info("no outputs are physically connected -- the outage is "
                 "still in progress; recovery waits for them to return")
        return 'outputs-absent'

    state = _load_state()
    if auto:
        last = state.get('last_recovery_start', 0)
        if time.time() - last < AUTO_COOLDOWN_SECS:
            log.warning("automatic recovery ran %.0fs ago (cooldown %ds); "
                        "refusing to restart the shell again so soon",
                        time.time() - last, AUTO_COOLDOWN_SECS)
            return 'cooldown'
    state['last_recovery_start'] = time.time()
    _save_state(state)

    log.warning("shell is wedged (health=%s, %d output(s) connected) -- "
                "running the validated recovery sequence", health, connected)

    # Variant (a): free a trapped lock before touching the WM -- but only
    # on a CONFIRMED zero-monitor wedge. That is the state where the
    # compositor composites black over everything and the unlock UI can
    # never render. When health is merely None (shell hung or gone), the
    # lock stays: cinnamon-screensaver is its own process, and once the
    # shell is restarted its unlock UI renders again. Deactivating on an
    # unconfirmed wedge would unlock an unattended machine on a false
    # positive (e.g. Eval timing out under memory pressure).
    if (health and health.get('monitors', -1) == 0
            and _screensaver_active()):
        _deactivate_screensaver()
    elif _screensaver_active():
        log.info("screensaver is active and the wedge is unconfirmed "
                 "(health=%s); leaving the lock in place", health)

    shield = _Shield()
    shield.stop()
    restarted = False
    try:
        # Up to two cycles: the second only when the orphaned-grab variant
        # is positively identified after the first.
        for attempt in (1, 2):
            restarted = _restart_shell_cycle()
            if not restarted:
                break
            _apply_layout()
            if not _stuck_grab_check():
                break
            if attempt == 1:
                log.warning("restarting the shell once more to release the "
                            "stuck grabs")
    finally:
        shield.cont()

    if not restarted:
        log.error("=== shell recovery FAILED: the shell never became "
                  "healthy; manual intervention is required ===")
        return 'restart-failed'

    _nudge_repaint_twice()

    final = cinnamon_shell_health()
    healthy = bool(final and final.get('monitors', 0) > 0)
    # Honest claim only: probes passed is not "it worked". Say exactly
    # what was measured.
    log.warning("=== shell recovery finished: restart + tile rebuild + "
                "repaint ran; shell now reports %s. Final verdict belongs "
                "to the user's eyes. ===", final)

    if healthy and not _gui_pid():
        relaunch_watcher_gui()

    return 'recovered' if healthy else 'restart-ran-shell-still-unhealthy'


def sentinel():
    """Periodic health check, run by a systemd --user timer. Catches the
    case the 2026-08-09/-12 incidents proved: the watcher itself can be
    dead (it is a GTK process; WM swaps can kill it), leaving nobody to
    detect the wedge. Also resurrects the watcher when it is missing."""
    adopt_session_env()
    if not (os.environ.get('DISPLAY')
            and os.environ.get('DBUS_SESSION_BUS_ADDRESS')):
        # No session (greeter, boot): nothing to watch.
        return

    from .cinnamon_compat import cinnamon_shell_health
    state = _load_state()
    cinnamon_pids = _pidof('cinnamon')
    session_alive = bool(_pidof('cinnamon-session-binary')
                         or _pidof('cinnamon-session'))
    health = cinnamon_shell_health() if cinnamon_pids else None

    wedged = False
    why = ''
    if cinnamon_pids and health is not None:
        state['consecutive_health_none'] = 0
        if health.get('monitors', 0) == 0:
            wedged, why = True, 'shell reports zero logical monitors'
    elif cinnamon_pids:
        # One None can be transient bus load; two sentinel ticks in a row
        # (>= a minute) with the shell unresponsive is a real problem.
        misses = state.get('consecutive_health_none', 0) + 1
        state['consecutive_health_none'] = misses
        if misses >= 2:
            wedged = True
            why = ('shell unresponsive on D-Bus for %d consecutive checks'
                   % misses)
    elif session_alive:
        wedged, why = True, 'cinnamon process is gone but the session is up'

    if wedged:
        log.warning("sentinel: %s -- invoking recovery", why)
        verdict = recover(auto=True, reason='sentinel: ' + why)
        log.warning("sentinel: recovery verdict: %s", verdict)
        if verdict in ('recovered', 'healthy'):
            state['consecutive_health_none'] = 0
    elif health and health.get('monitors', 0) > 0 and not _gui_pid():
        last = state.get('last_gui_relaunch', 0)
        if time.time() - last >= 120:
            log.warning("sentinel: shell is healthy but no splitrandr "
                        "watcher is running -- relaunching it")
            if relaunch_watcher_gui():
                state['last_gui_relaunch'] = time.time()
        else:
            log.info("sentinel: watcher still missing but it was relaunched "
                     "%.0fs ago; waiting", time.time() - last)

    _save_state(state)


_SERVICE_UNIT = """\
[Unit]
Description=SplitRandR shell-health sentinel (wedge recovery + watcher resurrection)

[Service]
Type=oneshot
# A wedge recovery inside a sentinel run takes minutes (health wait,
# tile rebuild, two repaint passes); the default oneshot start timeout
# (~90s) would SIGTERM it mid-sequence.
TimeoutStartSec=600
{env}ExecStart={exec_start}
"""

_TIMER_UNIT = """\
[Unit]
Description=Run the SplitRandR sentinel every minute

[Timer]
OnBootSec=90
OnUnitActiveSec=60
AccuracySec=15

[Install]
WantedBy=timers.target
"""


def _unit_dir():
    return os.path.join(
        os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'),
        'systemd', 'user')


def _render_units():
    """The unit texts install_sentinel would write RIGHT NOW. Shared with
    sentinel_status so an on-disk unit written by an older version (or a
    different python/package path) reads as outdated, exactly like the
    shim's disk-newer-than-process staleness check."""
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_line = ''
    if ('site-packages' not in pkg_parent
            and 'dist-packages' not in pkg_parent):
        env_line = 'Environment=PYTHONPATH=%s\n' % pkg_parent
    service = _SERVICE_UNIT.format(
        env=env_line,
        exec_start='%s -m splitrandr --sentinel' % sys.executable)
    return service, _TIMER_UNIT


def _systemctl_show(unit, props):
    """Parse ``systemctl --user show`` key=value output, or None when
    systemd --user isn't reachable from this process."""
    try:
        out = subprocess.run(
            ['systemctl', '--user', 'show', unit,
             '--property', ','.join(props)],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    parsed = {}
    for line in out.stdout.splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            parsed[k] = v
    return parsed


def sentinel_status():
    """Everything the GUI needs to show sentinel state.

    Returns a dict:
      supported   -- systemd --user answered at all
      installed   -- both unit files exist
      current     -- on-disk units match what install would write now
      enabled     -- timer unit-file state is enabled
      active      -- timer is actively scheduling
      last_run    -- human timestamp of the last trigger, or None
      next_run    -- human timestamp of the next trigger, or None
      last_result -- the service's systemd Result (e.g. 'success'), or None
    """
    st = {'supported': True, 'installed': False, 'current': False,
          'enabled': False, 'active': False,
          'last_run': None, 'next_run': None, 'last_result': None}
    unit_dir = _unit_dir()
    try:
        with open(os.path.join(unit_dir,
                               'splitrandr-sentinel.service')) as f:
            on_disk_service = f.read()
        with open(os.path.join(unit_dir, 'splitrandr-sentinel.timer')) as f:
            on_disk_timer = f.read()
        st['installed'] = True
        want_service, want_timer = _render_units()
        st['current'] = (on_disk_service == want_service
                         and on_disk_timer == want_timer)
    except OSError:
        pass

    props = _systemctl_show('splitrandr-sentinel.timer',
                            ['LoadState', 'ActiveState', 'UnitFileState',
                             'NextElapseUSecRealtime', 'LastTriggerUSec'])
    if props is None:
        st['supported'] = False
        return st
    if props.get('LoadState') == 'loaded':
        st['enabled'] = props.get('UnitFileState') in (
            'enabled', 'enabled-runtime', 'static')
        st['active'] = props.get('ActiveState') == 'active'
        for key, prop in (('next_run', 'NextElapseUSecRealtime'),
                          ('last_run', 'LastTriggerUSec')):
            val = props.get(prop, '')
            st[key] = val if val and val != 'n/a' else None
    sprops = _systemctl_show('splitrandr-sentinel.service', ['Result'])
    if sprops and sprops.get('Result'):
        st['last_result'] = sprops['Result']
    return st


def install_sentinel():
    """Write and enable the systemd --user sentinel timer. Returns
    ``(ok, message)``; also the remedy for an outdated or inactive
    sentinel (rewrite + daemon-reload + enable --now covers all three)."""
    unit_dir = _unit_dir()
    try:
        os.makedirs(unit_dir, exist_ok=True)
        service, timer = _render_units()
        with open(os.path.join(unit_dir,
                               'splitrandr-sentinel.service'), 'w') as f:
            f.write(service)
        with open(os.path.join(unit_dir,
                               'splitrandr-sentinel.timer'), 'w') as f:
            f.write(timer)
    except OSError as e:
        return False, "could not write sentinel units: %s" % e
    for cmd in (['systemctl', '--user', 'daemon-reload'],
                ['systemctl', '--user', 'enable', '--now',
                 'splitrandr-sentinel.timer']):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, "%s failed: %s" % (' '.join(cmd), e)
        if result.returncode != 0:
            return False, "%s failed: %s" % (' '.join(cmd),
                                             result.stderr.strip())
    msg = ("sentinel installed: %s/splitrandr-sentinel.{service,timer} "
           "(runs every 60s)" % unit_dir)
    log.info(msg)
    return True, msg
