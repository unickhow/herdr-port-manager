#!/usr/bin/env python3
"""Lists the ports this computer is listening on, and who holds them. Can also stop a process.

  port-manager.py list
  port-manager.py kill TERM|KILL PID:PORT:START [PID:PORT:START ...]
  port-manager.py tunnel start cloudflared|devtunnel PORT [OWNER_PID OWNER_START]
  port-manager.py tunnel stop cloudflared|devtunnel PORT [ID]
  port-manager.py tunnel status
  port-manager.py version
  port-manager.py tunnel login devtunnel
  port-manager.py _run cloudflared|devtunnel PORT OWNER_PID OWNER_START   (internal: the supervisor)

list prints one JSON object; each process has `start`, its start time in epoch seconds. kill takes
the pid, port and start time that were listed and, immediately before signalling each pid,
re-checks that it still has that start time, is still yours and still holds that port.

A tunnel runs under a small supervisor (_run) that caps its log at about 30 KB, stops the tunnel
GRACE seconds after the shared service (OWNER_PID with OWNER_START) is gone, and stops the tool
it started on every way out. Start and stop are serialised by one flock on STATE/.lock. Each
supervisor first takes, for its whole life, a flock on STATE/PROVIDER-PORT.run (never removed) and
exits at once if another holds it, so two of this version never run for one share. Stop with ID (the
`id` that status lists) refuses if the share was restarted since. A state with a newer `v` is
never stopped, deleted or overwritten by this version.
Reads process names, paths and ports only: no traffic, no file contents.
"""
import fcntl
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qr  # noqa: E402

# Bumped whenever the panel (main.js, pin.js) and this script must change together. The panel
# compares it with its own copy and warns when the installed files do not match.
VERSION = 2

HOME = os.path.expanduser('~')
ME = os.getuid()
SCRIPT = os.path.abspath(__file__)
STATE = os.path.join(HOME, '.cache', 'herdr-port-manager', 'tunnels')
V = 2               # state file schema version (no `v`: written by an older version)

GRACE = 15          # seconds a shared service may be gone before its tunnel is stopped
OWNER_CHECK = 3     # seconds between checks that the shared service still holds the port
LOG_HEAD = 10000    # the start of the log is kept verbatim (the link is printed there)
LOG_TAIL = 20000    # then only the most recent output (errors are printed there)
LOCK_WAIT = 10      # seconds start/stop wait for another start/stop
START_WAIT = 15     # seconds start waits for the supervisor to say it is up

TOOLS = ('cloudflared', 'devtunnel')
EXTRA_PATH = ['/opt/homebrew/bin', '/usr/local/bin', HOME + '/.local/bin', HOME + '/bin']
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
C_ENV = dict(os.environ, LC_ALL='C')


def sh(args, timeout=8):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=C_ENV).stdout
    except Exception:
        return ''


def lsof_ports(proto_args):
    """Yields dicts {pid, name, uid, proto, host, port} from lsof's field output."""
    out = sh(['lsof', '-nP', '-FpcuPn'] + proto_args)
    pid = name = uid = proto = None
    for line in out.splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == 'p':
            pid, name, uid, proto = int(val), None, None, None
        elif tag == 'c':
            name = val
        elif tag == 'u':
            uid = int(val)
        elif tag == 'P':
            proto = val
        elif tag == 'n' and pid is not None and '->' not in val:
            m = re.match(r'^(.*):(\d+)$', val)
            if m:
                yield {'pid': pid, 'name': name, 'uid': uid, 'proto': proto,
                       'host': m.group(1).strip('[]'), 'port': int(m.group(2))}


def scope(host):
    if host in ('127.0.0.1', '::1', 'localhost'):
        return 'local'
    return 'all' if host in ('*', '0.0.0.0', '::') else 'network'


def tool_path():
    return os.pathsep.join([os.environ.get('PATH', '')] + EXTRA_PATH)


def tools():
    """Which optional sharing tools are installed. Looks only; never runs or installs them."""
    return {k: bool(shutil.which(k, path=tool_path())) for k in TOOLS}


# ---- processes ------------------------------------------------------------------------------

def proc_status(pid):
    """('gone' | 'present' | 'unknown', lstart, args) of a pid. 'gone' only when the system says
    there is no such process of mine; 'unknown' when it could not be read."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return 'gone', None, None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return 'gone', None, None
    except PermissionError:
        return 'gone', None, None  # another user's process: not one of ours
    except OSError:
        return 'unknown', None, None
    try:
        out = subprocess.run(['ps', '-o', 'lstart=,args=', '-p', str(pid)], capture_output=True,
                             text=True, timeout=8, env=C_ENV).stdout
    except Exception:
        return 'unknown', None, None
    parts = out.strip().split(None, 5)
    if len(parts) == 6:
        return 'present', ' '.join(parts[:5]), parts[5]
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return 'gone', None, None  # it exited in between
    except OSError:
        pass
    return 'unknown', None, None


def start_time(pid):
    status, started, _ = proc_status(pid)
    return started if status == 'present' else None


def epoch(lstart):
    """ps's lstart ('Fri Nov  6 10:00:00 2026', LC_ALL=C) as integer epoch seconds, or None."""
    try:
        return int(time.mktime(time.strptime(' '.join(lstart.split()), '%a %b %d %H:%M:%S %Y')))
    except Exception:
        return None


def port_owners(pid, port, udp=True):
    """{uid} of this pid's listening TCP / bound UDP sockets on this port (empty if none)."""
    uids = set()
    protos = [['-iTCP:%d' % port, '-sTCP:LISTEN']] + ([['-iUDP:%d' % port]] if udp else [])
    for proto in protos:
        for r in lsof_ports(['-a', '-p', str(pid)] + proto):
            if r['pid'] == pid and r['port'] == port:
                uids.add(r['uid'])
    return uids


def holds(pid, port):
    return port_owners(pid, port, udp=False) == {ME}


# ---- state files ----------------------------------------------------------------------------

def state_file(provider, port):
    return os.path.join(STATE, '%s-%d.json' % (provider, port))


def read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return None


def write_atomic(path, body):
    """Writes bytes so that a reader never sees half a file; the temp file is removed on failure."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=os.path.basename(path) + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(body)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def write_json(path, data):
    write_atomic(path, json.dumps(data).encode())


def cleanup(path, st):
    """Forgets a tunnel: its state and its log (only a log inside STATE)."""
    files = [path]
    log = st.get('log')
    if isinstance(log, str) and os.path.dirname(os.path.abspath(log)) == STATE:
        files.append(log)
    for f in files:
        try:
            os.remove(f)
        except OSError:
            pass


class Busy(Exception):
    pass


BUSY_WHY = 'another start or stop of a share is in progress; try again in a moment'
NEWER_WHY = 'this share was made by a newer version of Port Manager; update the mod folder to manage it'
RESTARTED_WHY = 'this share was restarted since it was shown'


def newer(st):
    """A state written by a newer version: this version never stops, deletes or overwrites it."""
    return isinstance(st, dict) and type(st.get('v')) is int and st['v'] > V


def share_id(st):
    """A short token naming one run of a share: its supervisor's pid and start time (epoch), or the
    pid alone for a state of the first version. Stop with an ID acts only on that run."""
    pid = st.get('pid')
    if type(pid) is not int:
        return None
    if 'pid_start' not in st:
        return str(pid)
    return '%d-%d' % (pid, epoch(st['pid_start'] if isinstance(st['pid_start'], str) else '') or 0)


@contextmanager
def locked(wait=LOCK_WAIT):
    """Holds the one lock for starting and stopping shares (STATE/.lock, never removed). It is
    close-on-exec, so neither a supervisor nor a tool inherits it. Raises Busy after `wait` s."""
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(STATE, '.lock'), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.time() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise Busy()
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


# ---- tunnel identity ------------------------------------------------------------------------

def tool_args(provider, port):
    if provider == 'cloudflared':
        # Rewriting the Host header keeps dev servers that only accept localhost (Vite) from refusing it.
        return ['tunnel', '--no-autoupdate', '--url', 'http://localhost:%d' % port,
                '--http-host-header', 'localhost:%d' % port]
    return ['host', '-p', str(port)]


def contains(words, seq):
    return any(words[i:i + len(seq)] == seq for i in range(len(words) - len(seq) + 1))


def is_supervisor(words, provider, port):
    """`python … port-manager.py _run PROVIDER PORT …` (any version of the supervisor; the script's
    path may contain spaces)."""
    return os.path.basename(words[0]).lower().startswith('python') and any(
        words[i - 1].endswith('port-manager.py') and words[i:i + 3] == ['_run', provider, str(port)]
        for i in range(2, len(words)))


def is_tool(words, provider, port):
    """The tool an older version started directly for this port."""
    if provider == 'cloudflared':
        return 'tunnel' in words and contains(words, ['--url', 'http://localhost:%d' % port])
    return contains(words, ['host', '-p', str(port)])


def liveness(st):
    """('up' | 'gone' | 'unknown', why) of the process a state records. Never substring matching.

    Current states (with pid_start): the pid must have that start time and be the supervisor of
    this provider and port. Older states (no pid_start: the pid is the tool itself, or a supervisor
    of an intermediate version): the pid must positively be one of those for this port. A pid that
    is now another program counts as gone; the provider's own executable with other arguments, or
    a process that cannot be read, is 'unknown': kept and shown, never removed or signalled."""
    pid, provider, port = st.get('pid'), st.get('provider'), st.get('port')
    status, started, args = proc_status(pid)
    if status == 'gone':
        return 'gone', None
    if status == 'unknown':
        return 'unknown', 'could not tell whether pid %s is still running' % pid
    words = args.split()
    if 'pid_start' in st:
        return ('up', None) if started == st['pid_start'] and is_supervisor(words, provider, port) else ('gone', None)
    if is_supervisor(words, provider, port) or (os.path.basename(words[0]) == provider and is_tool(words[1:], provider, port)):
        return 'up', None
    if os.path.basename(words[0]) == provider:
        return 'unknown', ('pid %s is %s but not clearly the share of port %s, so it is left alone; '
                           'if it is, stop it with `kill %s`' % (pid, provider, port, pid))
    return 'gone', None


def signal_tunnel(st, sig):
    """Signals a tunnel if, checked immediately before, it is still the recorded one: a supervisor's
    whole process group (it, the tool and anything the tool started), or an older version's tool."""
    if liveness(st)[0] != 'up':
        return
    pid = st['pid']
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except ProcessLookupError:
        pass


def wait_gone(st, seconds):
    deadline = time.time() + seconds
    while True:
        if liveness(st)[0] == 'gone':
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.25)


# ---- status ---------------------------------------------------------------------------------

def read_log(path):
    """The start and the end of a log; reads at most LOG_HEAD + LOG_TAIL bytes."""
    try:
        with open(path, 'rb') as fh:
            size = os.fstat(fh.fileno()).st_size
            if size <= LOG_HEAD + LOG_TAIL:
                data = fh.read()
            else:
                data = fh.read(LOG_HEAD) + b'\n'
                fh.seek(size - LOG_TAIL)
                data += fh.read(LOG_TAIL)
        return ANSI.sub('', data.decode('utf-8', 'replace'))
    except Exception:
        return ''


def find_url(provider, port, text):
    if provider == 'cloudflared':
        m = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', text)
        return m.group(0) if m else None
    urls = [u.rstrip('.,;)') for u in re.findall(r'https://[^\s,]*devtunnels\.ms[^\s,]*', text)]
    urls = [u for u in urls if 'inspect' not in u]
    for u in urls:
        if '-%d.' % port in u:
            return u
    return urls[0] if urls else None


def valid_state(path, st):
    return (isinstance(st, dict) and st.get('provider') in TOOLS and isinstance(st.get('port'), int)
            and not isinstance(st.get('port'), bool) and path == state_file(st['provider'], st['port']))


def log_of(st):
    return read_log(st['log']) if isinstance(st.get('log'), str) else ''


def removable(st):
    """A gone tunnel that has nothing left to show: it had a link and ended without a reason, or it
    is an attempt older than an hour nobody retried. States of a newer version are left alone."""
    if newer(st):
        return False
    started = st.get('started') if isinstance(st.get('started'), (int, float)) else 0
    url = st.get('url') or find_url(st['provider'], st['port'], log_of(st))
    return time.time() - started > 3600 or bool(url and not st.get('ended'))


def forget_if_gone(path):
    """Removes a finished tunnel's state, only if the lock is free at once (listing never waits)
    and, re-read under it, it is still gone and removable."""
    try:
        with locked(wait=0):
            cur = read_json(path)
            if valid_state(path, cur) and removable(cur) and liveness(cur)[0] == 'gone':
                cleanup(path, cur)
    except (Busy, OSError):
        pass


def tunnels():
    """Tunnels this tool started, from the state files only (no port scan)."""
    out = []
    try:
        names = sorted(os.listdir(STATE))
    except OSError:
        return out
    for n in names:
        path = os.path.join(STATE, n)
        if not n.endswith('.json'):
            continue
        st = read_json(path)
        if not valid_state(path, st):
            continue
        provider, port = st['provider'], st['port']
        if newer(st):  # its fields may mean something else: shown, never judged or acted on
            out.append({'provider': provider, 'port': port, 'pid': st.get('pid'), 'id': share_id(st),
                        'url': st.get('url') if isinstance(st.get('url'), str) else None,
                        'running': False, 'newer': True, 'error': NEWER_WHY[0].upper() + NEWER_WHY[1:] + '.'})
            continue
        state, why = liveness(st)
        if state == 'gone' and removable(st):
            forget_if_gone(path)
            continue
        text = log_of(st)
        url = st.get('url') or find_url(provider, port, text)
        legacy = 'pid_start' not in st
        item = {'provider': provider, 'port': port, 'pid': st.get('pid'), 'id': share_id(st), 'url': url,
                'running': state == 'up', 'started': st.get('started'), 'legacy': legacy,
                'watch': not legacy and st.get('owner_pid') is not None}  # stops on its own
        if state == 'up' and url:
            try:
                item['qr'] = [''.join(map(str, r)) for r in qr.make(url)]
            except Exception:
                pass
        if state == 'unknown':
            item['uncertain'] = True
            item['error'] = 'Cannot tell whether this share is still running: %s.' % why
        elif state == 'gone':
            ended = st.get('ended') if isinstance(st.get('ended'), str) else None
            if ended:
                item['ended'] = item['error'] = ended
            else:
                tail = [l.strip() for l in text.splitlines() if l.strip()][-4:]
                item['error'] = ' '.join(tail)[:300] or 'Exited without output'
                item['needs_login'] = bool(re.search(r'log ?in|sign ?in|unauthori[sz]ed|not authenticated', text, re.I))
        out.append(item)
    return out


# ---- start and stop -------------------------------------------------------------------------

def stop_tunnel(provider, port, sid=None):
    try:
        with locked():
            return _stop_locked(provider, port, sid)
    except Busy:
        return {'ok': False, 'why': BUSY_WHY}
    except OSError as e:
        return {'ok': False, 'why': 'could not use ~/.cache/herdr-port-manager: %s' % e.strerror}


def _stop_locked(provider, port, sid=None):
    """Stops the recorded tunnel (TERM, then KILL) and forgets it only once it is positively gone.
    Otherwise keeps the state, so the panel keeps showing it, and says why. With `sid`, only if the
    recorded share is still that run. Never touches a newer version's state."""
    path = state_file(provider, port)
    st = read_json(path)
    if not valid_state(path, st):
        return {'ok': True}
    if newer(st):
        return {'ok': False, 'newer': True, 'why': NEWER_WHY}
    if sid is not None and share_id(st) != sid:
        return {'ok': False, 'restarted': True, 'why': RESTARTED_WHY}
    try:
        state, why = liveness(st)
        if state == 'unknown':
            return {'ok': False, 'why': why}
        if state == 'up':
            signal_tunnel(st, signal.SIGTERM)
            if not wait_gone(st, 8):
                signal_tunnel(st, signal.SIGKILL)
                if not wait_gone(st, 2):
                    return {'ok': False, 'why': liveness(st)[1] or 'the tunnel (pid %s) did not stop' % st.get('pid')}
    except Exception as e:
        return {'ok': False, 'why': 'could not stop pid %s: %s' % (st.get('pid'), e)}
    cleanup(path, st)
    return {'ok': True}


def listener(port):
    """The pid of one of my processes listening on this TCP port, or None."""
    for r in lsof_ports(['-iTCP:%d' % port, '-sTCP:LISTEN']):
        if r['port'] == port and r['uid'] == ME:
            return r['pid']
    return None


def start_tunnel(provider, port, owner=None, owner_start=None):
    """Shares a port, tied to its listener: `owner` with `owner_start` (epoch seconds, the one the
    user chose) or, without them, the current listener. All of it runs under the lock."""
    if not shutil.which(provider, path=tool_path()):
        return {'ok': False, 'why': '%s is not installed' % provider}
    try:
        with locked():
            remove_stale_temp_files()
            return _start_locked(provider, port, owner, owner_start)
    except Busy:
        return {'ok': False, 'why': BUSY_WHY}
    except OSError as e:
        return {'ok': False, 'why': 'could not use ~/.cache/herdr-port-manager: %s' % e.strerror}


def remove_stale_temp_files():
    """Temp files of a write that was killed midway (older than 10 minutes)."""
    for n in os.listdir(STATE):
        f = os.path.join(STATE, n)
        try:
            if n.endswith('.tmp') and time.time() - os.lstat(f).st_mtime > 600:
                os.remove(f)
        except OSError:
            pass


def _start_locked(provider, port, owner, owner_start):
    path = state_file(provider, port)
    st = read_json(path)
    if valid_state(path, st) and newer(st):
        return {'ok': False, 'newer': True, 'why': NEWER_WHY}
    if valid_state(path, st) and liveness(st)[0] == 'up':
        if owner is not None and st.get('owner_pid') is not None and 'pid_start' in st \
                and (st.get('owner_pid'), epoch(st.get('owner_start') or '')) != (owner, owner_start):
            return {'ok': False, 'why': 'port %d is still shared, tied to the process that held it before; '
                    'that share stops on its own within about %d s, then try again' % (port, GRACE)}
        return {'ok': True, 'already': True}
    # A tunnel is tied to the process holding the port: with none, there is nothing to share and
    # nothing that would ever stop the tunnel on its own.
    if owner is None:
        owner = listener(port)
        if not owner:
            return {'ok': False, 'why': 'nothing is listening on port %d' % port}
        owner_start = epoch(start_time(owner) or '')
    uids = port_owners(owner, port, udp=False)
    if not uids:
        return {'ok': False, 'why': 'the service on port %d changed since you chose to share it '
                '(pid %d is not listening there)' % (port, owner)}
    if uids != {ME}:
        return {'ok': False, 'why': 'the service on port %d is owned by another user' % port}
    if owner_start is None or epoch(start_time(owner) or '') != owner_start:
        return {'ok': False, 'why': 'the service on port %d changed since you chose to share it '
                '(pid %d is another process now)' % (port, owner)}
    r = _stop_locked(provider, port)  # clears a finished attempt; refuses if one is uncertain
    if not r['ok']:
        return r
    try:
        proc = subprocess.Popen([sys.executable, SCRIPT, '_run', provider, str(port), str(owner), str(owner_start)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                start_new_session=True, env=dict(os.environ, PATH=tool_path(), NO_COLOR='1'))
    except OSError as e:
        return {'ok': False, 'why': 'could not start the tunnel supervisor: %s' % e.strerror}
    # The supervisor answers one line: 'ok' once its state is saved and the tool started, 'busy'
    # when another supervisor of this share is alive (it then started nothing), or why not.
    line = None
    try:
        if select.select([proc.stdout], [], [], START_WAIT)[0]:
            line = proc.stdout.readline().decode('utf-8', 'replace').strip()
        if line == 'ok':
            proc.stdout.close()
            return {'ok': True, 'pid': proc.pid}
    finally:
        if line != 'ok':
            abandon(proc, path)  # nothing may be left running that is not recorded
    if line is None:
        return {'ok': False, 'why': 'the tunnel supervisor did not start in time'}
    if line == 'busy':
        return {'ok': False, 'why': 'another start of this share is still in progress; try again in a moment'}
    return {'ok': False, 'why': line or 'the tunnel supervisor exited (code %s)' % proc.returncode}


def abandon(proc, path):
    """Stops the supervisor this start spawned (never another one) when it did not report success,
    its tool and anything in its session, and forgets the state only if that supervisor wrote it."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            pass
        try:
            proc.wait(5)
            break
        except subprocess.TimeoutExpired:
            pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # anything the tool left in that group
    except OSError:
        pass
    st = read_json(path)
    if isinstance(st, dict) and not newer(st) and st.get('pid') == proc.pid:
        cleanup(path, st)


# ---- the supervisor -------------------------------------------------------------------------

class Log:
    """The tool's output, capped: the first LOG_HEAD bytes verbatim, then a ring of the last
    LOG_TAIL bytes. Rewritten to disk (atomically) when it changes, so the file never grows."""

    def __init__(self, path):
        self.path, self.head, self.tail = path, bytearray(), bytearray()
        self.dropped, self.changed, self.lock = False, True, threading.Lock()

    def add(self, data):
        with self.lock:
            room = LOG_HEAD - len(self.head)
            if room > 0:
                self.head += data[:room]
                data = data[room:]
            self.tail += data
            if len(self.tail) > LOG_TAIL:
                del self.tail[:len(self.tail) - LOG_TAIL]
                self.dropped = True
            self.changed = True

    def text(self):
        with self.lock:
            return (bytes(self.head) + bytes(self.tail)).decode('utf-8', 'replace')

    def flush(self):
        with self.lock:
            if not self.changed:
                return
            body = bytes(self.head) + (b'\n[...]\n' if self.dropped else b'') + bytes(self.tail)
            self.changed = False
        try:
            write_atomic(self.path, body)
        except Exception:
            with self.lock:
                self.changed = True  # retried on the next flush


def still_mine(path, st):
    """The state file still records this supervisor (it is only ever rewritten or removed then)."""
    cur = read_json(path)
    return isinstance(cur, dict) and not newer(cur) and (cur.get('pid'), cur.get('pid_start')) == (st['pid'], st['pid_start'])


def supervise(provider, port, owner, owner_epoch):
    """Runs the tunnel tool as a child, in this process's group, and stays with it. Reports one line
    on stdout to the starting process ('ok', 'busy' or why not). Once the tool is started it is stopped and
    reaped on every way out of here, including an exception."""
    path = state_file(provider, port)
    log = Log(os.path.join(STATE, '%s-%d.log' % (provider, port)))
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append('stopped'))
    signal.signal(signal.SIGINT, lambda *_: stopping.append('stopped'))
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    def report(msg):
        try:
            os.write(1, (msg + '\n').encode())
            null = os.open(os.devnull, os.O_WRONLY)
            os.dup2(null, 1)
            os.close(null)
        except OSError:
            pass  # the starting process is gone; the state (if saved) still finds this tunnel
        return 1

    # First, before anything is read or written: this share's run lock, held for this process's whole
    # life (the kernel drops it however it ends). Close-on-exec, and the tool gets no inherited fds.
    try:
        os.makedirs(STATE, mode=0o700, exist_ok=True)
        run = os.open(os.path.join(STATE, '%s-%d.run' % (provider, port)),
                      os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        fcntl.flock(run, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return report('busy')  # another supervisor of this share is alive; nothing was started
    except OSError as e:
        return report('could not use ~/.cache/herdr-port-manager: %s' % e.strerror)
    cur = read_json(path)
    if newer(cur):
        return report(NEWER_WHY)
    if isinstance(cur, dict) and cur.get('pid') != os.getpid() and liveness(cur)[0] != 'gone':  # an older version's
        return report('port %d is already shared by another tunnel' % port)
    owner_start = start_time(owner)
    if not owner_start or epoch(owner_start) != owner_epoch or not holds(owner, port):
        return report('the service on port %d exited or changed while sharing was starting' % port)
    pid_start = start_time(os.getpid())
    if not pid_start:
        return report('the tunnel supervisor could not read its own start time')
    st = {'v': V, 'provider': provider, 'port': port, 'pid': os.getpid(), 'pid_start': pid_start,
          'started': time.time(), 'log': log.path, 'owner_pid': owner, 'owner_start': owner_start}
    try:
        write_json(path, st)
    except OSError as e:
        return report('could not save the tunnel state in ~/.cache/herdr-port-manager: %s' % e.strerror)
    if stopping:
        if still_mine(path, st):
            cleanup(path, st)
        return 0
    own_group = os.getpgrp() == os.getpid()
    try:
        child = subprocess.Popen([shutil.which(provider, path=tool_path()) or provider] + tool_args(provider, port),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as e:
        if still_mine(path, st):
            cleanup(path, st)
        return report('could not start %s: %s' % (provider, e.strerror))
    reader, forced = None, False
    try:
        report('ok')

        def pump():
            while True:
                try:
                    chunk = os.read(child.stdout.fileno(), 65536)
                except OSError:
                    break
                if not chunk:
                    break
                log.add(chunk)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        next_check, lost_since = time.time() + OWNER_CHECK, None
        while child.poll() is None and not stopping:
            time.sleep(1)
            log.flush()  # a failed rewrite is retried on the next change
            if not st.get('url'):
                url = find_url(provider, port, ANSI.sub('', log.text()))
                if url and still_mine(path, st):
                    try:
                        write_json(path, dict(st, url=url))
                        st['url'] = url
                    except OSError:
                        pass  # keep the tunnel up; retried next second
            if time.time() >= next_check:
                next_check = time.time() + OWNER_CHECK
                if start_time(owner) == owner_start and holds(owner, port):
                    lost_since = None
                elif lost_since is None:
                    lost_since = time.time()
                elif time.time() - lost_since >= GRACE:
                    stopping.append('service exited')
        if stopping[:1] == ['service exited']:
            st['ended'] = 'service exited'
        return 0
    finally:
        if child.poll() is None:
            # Stop the tool and anything it started (the whole group; this process ignores it).
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            try:
                if own_group:
                    os.killpg(os.getpgrp(), signal.SIGTERM)
                else:
                    child.terminate()
            except OSError:
                child.terminate()
            try:
                child.wait(5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
                forced = True
        else:
            child.wait()
        if reader:
            reader.join(1)
        log.flush()
        if st.get('ended') and still_mine(path, st):
            try:
                write_json(path, st)
            except OSError:
                pass
        if forced and own_group:
            os.killpg(os.getpgrp(), signal.SIGKILL)  # anything the tool left behind, and this process


def login_devtunnel():
    exe = shutil.which('devtunnel', path=tool_path())
    if not exe:
        return {'ok': False, 'why': 'devtunnel is not installed'}
    try:
        subprocess.Popen([exe, 'user', 'login'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True,
                         env=dict(os.environ, PATH=tool_path(), NO_COLOR='1'))
    except OSError as e:
        return {'ok': False, 'why': 'could not run devtunnel: %s' % e.strerror}
    return {'ok': True}


# ---- list and kill --------------------------------------------------------------------------

def collect():
    rows = list(lsof_ports(['-iTCP', '-sTCP:LISTEN'])) + list(lsof_ports(['-iUDP']))
    seen, ports = set(), []
    for r in rows:
        key = (r['pid'], r['proto'], r['port'], scope(r['host']))
        if key in seen:
            continue  # the same socket is often listed once per address family
        seen.add(key)
        r['scope'] = scope(r['host'])
        ports.append(r)
    pids = sorted({r['pid'] for r in ports})
    info = {}
    if pids:
        # lstart is five words ('Fri Nov  6 10:00:00 2026'); the command comes last.
        for line in sh(['ps', '-o', 'pid=,etime=,user=,lstart=,command=', '-p', ','.join(map(str, pids))]).splitlines():
            parts = line.split(None, 8)
            if len(parts) == 9:
                info[int(parts[0])] = {'etime': parts[1], 'user': parts[2], 'command': parts[8],
                                       'start': epoch(' '.join(parts[3:8]))}
        cur = None
        for line in sh(['lsof', '-nP', '-a', '-d', 'cwd', '-Fpn', '-p', ','.join(map(str, pids))]).splitlines():
            if line.startswith('p'):
                cur = int(line[1:])
            elif line.startswith('n') and cur:
                d = line[1:]
                info.setdefault(cur, {})['cwd'] = d.replace(HOME, '~', 1) if d.startswith(HOME) else d
    procs = {}
    for r in ports:
        i = info.get(r['pid']) or {}
        p = procs.setdefault(r['pid'], {
            'pid': r['pid'], 'name': r['name'], 'mine': r['uid'] == ME, 'ports': [],
            'command': i.get('command', ''), 'cwd': i.get('cwd'), 'user': i.get('user'),
            'running': i.get('etime'), 'start': i.get('start')})
        p['ports'].append({'port': r['port'], 'proto': r['proto'], 'scope': r['scope']})
    for p in procs.values():
        p['ports'].sort(key=lambda x: (x['port'], x['proto']))
    ordered = sorted(procs.values(), key=lambda p: p['ports'][0]['port'])
    return {'version': VERSION, 'generated_at': time.time(), 'processes': ordered, 'tools': tools(), 'tunnels': tunnels()}


def num(s, lo, hi):
    """A decimal argument within [lo, hi], or None (no signs, spaces or other forms)."""
    if not isinstance(s, str) or not re.fullmatch(r'[0-9]{1,12}', s):
        return None
    n = int(s)
    return n if lo <= n <= hi else None


def kill(sig_name, targets):
    """Signals each PID:PORT:START only if, checked immediately before its signal, the pid still has
    that start time, is still mine and still holds that port."""
    sig = {'TERM': signal.SIGTERM, 'KILL': signal.SIGKILL}[sig_name]
    results = []
    for t in targets:
        parts = t.split(':')
        if len(parts) != 3 or None in (num(parts[0], 1, 2 ** 31), num(parts[1], 1, 65535), num(parts[2], 0, 2 ** 40)):
            results.append({'pid': t, 'ok': False, 'why': 'invalid target (expected PID:PORT:START)'})
            continue
        pid, port, start = map(int, parts)
        if pid in (os.getpid(), os.getppid()):
            results.append({'pid': pid, 'ok': False, 'why': 'refusing to stop this tool'})
            continue
        uids = port_owners(pid, port)
        if not uids:
            results.append({'pid': pid, 'ok': False, 'why': 'no longer holds this port'})
        elif uids != {ME}:
            results.append({'pid': pid, 'ok': False, 'why': 'owned by another user'})
        elif epoch(start_time(pid) or '') != start:
            results.append({'pid': pid, 'ok': False, 'why': 'process changed since it was listed'})
        else:
            try:
                os.kill(pid, sig)
                results.append({'pid': pid, 'ok': True})
            except OSError as e:
                results.append({'pid': pid, 'ok': False, 'why': str(e)})
    return {'results': results}


def main(a):
    """The CLI; returns (result, exit code). Optional arguments stay optional."""
    invalid = {'ok': False, 'why': 'invalid arguments'}
    if a[:1] == ['_run']:
        ok = len(a) == 5 and a[1] in TOOLS and num(a[2], 1, 65535) and num(a[3], 1, 2 ** 31) and num(a[4], 1, 2 ** 40)
        return None, (supervise(a[1], int(a[2]), int(a[3]), int(a[4])) if ok else 64)
    if a[:1] == ['tunnel'] and a[1:2] in (['start'], ['stop']):
        if len(a) < 4 or a[2] not in TOOLS or not num(a[3], 1, 65535):
            return {'ok': False, 'why': 'invalid provider or port'}, 0
        provider, port, rest = a[2], int(a[3]), a[4:]
        if a[1] == 'stop':
            # ID: the share's `id` as listed (PID or PID-EPOCH). An earlier panel passed a 16-hex token
            # instead; it is accepted and treated as no ID, so that panel keeps working as before.
            if not rest or (len(rest) == 1 and re.fullmatch(r'[0-9a-f]{16}', rest[0])):
                return stop_tunnel(provider, port), 0
            if len(rest) == 1 and re.fullmatch(r'[0-9]{1,10}(-[0-9]{1,12})?', rest[0]):
                return stop_tunnel(provider, port, rest[0]), 0
            return invalid, 0
        if not rest:
            return start_tunnel(provider, port), 0
        if len(rest) == 2 and num(rest[0], 1, 2 ** 31) and num(rest[1], 1, 2 ** 40):
            return start_tunnel(provider, port, int(rest[0]), int(rest[1])), 0
        return invalid, 0
    if a == ['version']:
        return {'version': VERSION}, 0
    if a == ['tunnel', 'status']:
        return {'tunnels': tunnels()}, 0
    if a == ['tunnel', 'login', 'devtunnel']:
        return login_devtunnel(), 0
    if a[:1] == ['kill'] and len(a) >= 3 and a[1] in ('TERM', 'KILL'):
        return kill(a[1], a[2:]), 0
    if a in ([], ['list']):
        return collect(), 0
    return invalid, 64


if __name__ == '__main__':
    out, code = main(sys.argv[1:])
    if out is not None:
        json.dump(out, sys.stdout)
    sys.exit(code)
