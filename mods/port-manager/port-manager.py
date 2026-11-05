#!/usr/bin/env python3
"""Lists the ports this computer is listening on, and who holds them. Can also stop a process.

  port-manager.py list
  port-manager.py kill TERM|KILL PID:PORT [PID:PORT ...]
  port-manager.py tunnel start cloudflared|devtunnel PORT
  port-manager.py tunnel stop cloudflared|devtunnel PORT
  port-manager.py tunnel login devtunnel

list prints one JSON object. kill re-reads the port table first and only signals a pid that
still holds the port it was shown with, so a recycled pid is never hit. It refuses processes
owned by other users. Reads process names, paths and ports only: no traffic, no file contents.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qr  # noqa: E402

HOME = os.path.expanduser('~')
ME = os.getuid()


def sh(args, timeout=8):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
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


TOOLS = {'cloudflared': 'cloudflared', 'devtunnel': 'devtunnel'}
EXTRA_PATH = ['/opt/homebrew/bin', '/usr/local/bin', HOME + '/.local/bin', HOME + '/bin']


def tools():
    """Which optional sharing tools are installed. Looks only; never runs or installs them."""
    path = os.pathsep.join([os.environ.get('PATH', '')] + EXTRA_PATH)
    return {k: bool(shutil.which(b, path=path)) for k, b in TOOLS.items()}


STATE = os.path.join(HOME, '.cache', 'herdr-port-manager', 'tunnels')
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')


def tool_path():
    return os.pathsep.join([os.environ.get('PATH', '')] + EXTRA_PATH)


def state_file(provider, port):
    return os.path.join(STATE, '%s-%d.json' % (provider, port))


def alive(pid, provider=None):
    """Is this pid running? With a provider, also checks the process is still that tool, so a pid
    the system has since handed to something else is never treated as ours (or signalled)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except Exception:
        pass
    if provider:
        return provider in sh(['ps', '-o', 'command=', '-p', str(pid)])
    return True


LOG_LIMIT = 256 * 1024


def read_log(path):
    """The start and the end of the log (the link is near the start, errors near the end)."""
    try:
        with open(path, 'rb') as fh:
            data = fh.read()
        text = data.decode('utf-8', 'replace')
        if len(text) > 30000:
            text = text[:10000] + '\n' + text[-20000:]
        return ANSI.sub('', text)
    except Exception:
        return ''


def trim_log(path):
    """A long-lived tunnel keeps logging. The child appends, so emptying the file is safe."""
    try:
        if os.path.getsize(path) > LOG_LIMIT:
            os.truncate(path, 0)
    except Exception:
        pass


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


def tunnels():
    """Tunnels this tool started. A tunnel whose process ended without ever getting a link is
    kept (with the end of its log) until dismissed, so the error can be shown."""
    out = []
    try:
        names = os.listdir(STATE)
    except Exception:
        return out
    for n in sorted(names):
        if not n.endswith('.json'):
            continue
        path = os.path.join(STATE, n)
        try:
            with open(path) as fh:
                st = json.load(fh)
        except Exception:
            continue
        up = alive(st['pid'], st['provider'])
        if not up and time.time() - st['started'] > 3600:
            cleanup(path, st)  # an old failed attempt nobody retried
            continue
        text = read_log(st['log'])
        # The link is printed once at the start, so remember it in case the log gets trimmed.
        url = st.get('url') or find_url(st['provider'], st['port'], text)
        if url and not st.get('url'):
            st['url'] = url
            try:
                with open(path, 'w') as fh:
                    json.dump(st, fh)
            except Exception:
                pass
        if up:
            trim_log(st['log'])
        if not up and url:
            cleanup(path, st)  # it ran and has ended: nothing left to show
            continue
        item = {'provider': st['provider'], 'port': st['port'], 'pid': st['pid'], 'url': url,
                'running': up, 'started': st['started']}
        if up and url:
            try:
                item['qr'] = [''.join(map(str, r)) for r in qr.make(url)]
            except Exception:
                pass
        if not up:
            tail = [l.strip() for l in text.splitlines() if l.strip()][-4:]
            item['error'] = ' '.join(tail)[:300] or 'Exited without output'
            item['needs_login'] = bool(re.search(r'log ?in|sign ?in|unauthori[sz]ed|not authenticated', text, re.I))
        out.append(item)
    return out


def cleanup(path, st):
    for f in (path, st.get('log')):
        try:
            os.remove(f)
        except Exception:
            pass


def stop_tunnel(provider, port):
    path = state_file(provider, port)
    try:
        with open(path) as fh:
            st = json.load(fh)
    except Exception:
        return {'ok': True}
    if alive(st['pid'], provider):
        try:
            os.killpg(st['pid'], signal.SIGTERM)  # the tool and anything it started
        except Exception:
            try:
                os.kill(st['pid'], signal.SIGTERM)
            except Exception:
                pass
    cleanup(path, st)
    return {'ok': True}


def spawn(provider, args, port, name):
    exe = shutil.which(provider, path=tool_path())
    if not exe:
        return {'ok': False, 'why': '%s is not installed' % provider}
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    env = dict(os.environ, PATH=tool_path(), NO_COLOR='1')
    if name is None:  # fire and forget (sign-in): nothing to track
        subprocess.Popen([exe] + args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, env=env)
        return {'ok': True}
    log = os.path.join(STATE, '%s.log' % name)
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_APPEND, 0o600)
    try:
        proc = subprocess.Popen([exe] + args, stdin=subprocess.DEVNULL, stdout=fd, stderr=fd,
                                start_new_session=True, env=env)
    finally:
        os.close(fd)
    with open(os.path.join(STATE, name + '.json'), 'w') as out:
        json.dump({'provider': provider, 'port': port, 'pid': proc.pid, 'log': log, 'started': time.time()}, out)
    return {'ok': True, 'pid': proc.pid}


def start_tunnel(provider, port):
    if provider not in TOOLS:
        return {'ok': False, 'why': 'unknown provider'}
    for t in tunnels():
        if t['provider'] == provider and t['port'] == port and t['running']:
            return {'ok': True, 'already': True}
    stop_tunnel(provider, port)  # clears a failed attempt's leftovers
    if provider == 'cloudflared':
        # Rewriting the Host header keeps dev servers that only accept localhost (Vite) from refusing it.
        args = ['tunnel', '--no-autoupdate', '--url', 'http://localhost:%d' % port,
                '--http-host-header', 'localhost:%d' % port]
    else:
        args = ['host', '-p', str(port)]
    return spawn(provider, args, port, '%s-%d' % (provider, port))


def login_devtunnel():
    return spawn('devtunnel', ['user', 'login'], 0, None)


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
        for line in sh(['ps', '-o', 'pid=,etime=,user=,command=', '-p', ','.join(map(str, pids))]).splitlines():
            parts = line.split(None, 3)
            if len(parts) == 4:
                info[int(parts[0])] = {'etime': parts[1], 'user': parts[2], 'command': parts[3]}
        cwd = {}
        cur = None
        for line in sh(['lsof', '-nP', '-a', '-d', 'cwd', '-Fpn', '-p', ','.join(map(str, pids))]).splitlines():
            if line.startswith('p'):
                cur = int(line[1:])
            elif line.startswith('n') and cur:
                cwd[cur] = line[1:]
        for p, d in cwd.items():
            info.setdefault(p, {})['cwd'] = d.replace(HOME, '~', 1) if d.startswith(HOME) else d
    procs = {}
    for r in ports:
        p = procs.setdefault(r['pid'], {
            'pid': r['pid'], 'name': r['name'], 'mine': r['uid'] == ME, 'ports': [],
            'command': (info.get(r['pid']) or {}).get('command', ''),
            'cwd': (info.get(r['pid']) or {}).get('cwd'),
            'user': (info.get(r['pid']) or {}).get('user'),
            'running': (info.get(r['pid']) or {}).get('etime')})
        p['ports'].append({'port': r['port'], 'proto': r['proto'], 'scope': r['scope']})
    for p in procs.values():
        p['ports'].sort(key=lambda x: (x['port'], x['proto']))
    ordered = sorted(procs.values(), key=lambda p: p['ports'][0]['port'])
    return {'generated_at': time.time(), 'processes': ordered, 'tools': tools(), 'tunnels': tunnels()}


def kill(sig_name, targets):
    sig = {'TERM': signal.SIGTERM, 'KILL': signal.SIGKILL}[sig_name]
    live = {p['pid']: p for p in collect()['processes']}
    results = []
    for t in targets:
        pid_s, _, port_s = t.partition(':')
        pid, port = int(pid_s), int(port_s)
        p = live.get(pid)
        if not p or port not in {x['port'] for x in p['ports']}:
            results.append({'pid': pid, 'ok': False, 'why': 'no longer holds this port'})
        elif not p['mine']:
            results.append({'pid': pid, 'ok': False, 'why': 'owned by another user'})
        elif pid == os.getpid() or pid == os.getppid():
            results.append({'pid': pid, 'ok': False, 'why': 'refusing to stop this tool'})
        else:
            try:
                os.kill(pid, sig)
                results.append({'pid': pid, 'ok': True})
            except Exception as e:
                results.append({'pid': pid, 'ok': False, 'why': str(e)})
    return {'results': results}


if __name__ == '__main__':
    a = sys.argv[1:]
    if a[:3] and a[0] == 'tunnel' and a[1] in ('start', 'stop') and len(a) == 4:
        fn = start_tunnel if a[1] == 'start' else stop_tunnel
        port = int(a[3])
        if not 0 < port < 65536 or a[2] not in TOOLS:
            json.dump({'ok': False, 'why': 'invalid provider or port'}, sys.stdout)
        else:
            json.dump(fn(a[2], port), sys.stdout)
    elif a[:3] == ['tunnel', 'login', 'devtunnel']:
        json.dump(login_devtunnel(), sys.stdout)
    elif a[:1] == ['kill'] and len(a) >= 3 and a[1] in ('TERM', 'KILL'):
        json.dump(kill(a[1], a[2:]), sys.stdout)
    else:
        json.dump(collect(), sys.stdout)
