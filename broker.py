"""Calibre adapter for tailcatctl. Connection policy and profiles live in Rust."""

from functools import lru_cache
import hashlib
import json
import os
import platform
import selectors
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


MAX_REPLY = 128 * 1024


@lru_cache(maxsize=1)
def executable():
    override = os.environ.get('TAILCATCTL_BIN') or os.environ.get('KINDLE_LINK_BIN')
    if override:
        path = os.path.abspath(os.path.expanduser(override))
        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            raise RuntimeError(f'Configured CLI binary is not executable: {path}')
        return path

    which_bin = shutil.which('tailcatctl') or shutil.which('kindle-link')
    if which_bin and os.path.isfile(which_bin) and os.access(which_bin, os.X_OK):
        return which_bin

    local_bin = Path.home() / '.local/bin/tailcatctl'
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return str(local_bin)

    machine = platform.machine().lower()
    machine = {'aarch64': 'arm64', 'amd64': 'x86_64'}.get(machine, machine)
    loader = globals().get('get_resources')

    data = None
    for name in (f'bin/tailcatctl-{sys.platform}-{machine}', f'bin/kindle-link-{sys.platform}-{machine}'):
        if loader:
            data = loader(name, print_tracebacks_for_missing_resources=False)
            if data is not None:
                break

    if data is None:
        raise RuntimeError(
            f'The plugin has no tailcatctl binary for {sys.platform}/{machine}. '
            'Install tailcatctl in ~/.local/bin or set TAILCATCTL_BIN to its absolute path.'
        )

    digest = hashlib.sha256(data).hexdigest()
    cache = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'tailcatctl' / digest
    cache.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = cache.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError(f'Unsafe tailcatctl binary cache directory: {cache}')
    target = cache / 'tailcatctl'
    if target.exists():
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise RuntimeError(f'Unsafe tailcatctl executable: {target}')
        if hashlib.sha256(target.read_bytes()).hexdigest() == digest and os.access(target, os.X_OK):
            return str(target)
    with tempfile.NamedTemporaryFile(dir=cache, delete=False) as output:
        temporary = output.name
        try:
            output.write(data)
            output.flush()
            os.fchmod(output.fileno(), 0o700)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return str(target)


def connection_config(options):
    """Credentials travel only on the CLI's stdin pipe, never its arguments."""
    config = {
        'host': options['host'], 'port': options.get('port', 22),
        'username': options.get('username', 'root'),
        'identity_file': os.path.expanduser(options.get('identity_file') or ''),
        'known_hosts': os.path.expanduser(options.get('known_hosts') or '~/.ssh/known_hosts'),
        'password': options.get('password') or '', 'timeout': options.get('timeout', 15),
    }
    for name in ('ssh_executable', 'sftp_executable', 'tailcat_executable'):
        if options.get(name):
            config[name] = options[name]
    return config


def _run_json(command, value=None, timeout=15, args=()):
    argv = [executable(), command, *args]
    payload = None
    if value is not None:
        argv += ['--config', '-']
        payload = json.dumps(value).encode('utf-8')
    result = subprocess.run(argv, input=payload, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', 'replace').strip() or
                           f'tailcatctl {command} failed (exit {result.returncode})')
    return json.loads(result.stdout)


def registry_stamp():
    custom = os.environ.get('TAILCATCTL_CONFIG_DIR') or os.environ.get('TAILCAT_CONFIG_DIR')
    if custom:
        try:
            info = (Path(custom) / 'devices.json').stat()
            return info.st_ino, info.st_mtime_ns, info.st_size
        except FileNotFoundError:
            return None

    base = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    for rel in ('tailcat/devices.json', 'kindle-link/devices.json'):
        try:
            info = (base / rel).stat()
            return info.st_ino, info.st_mtime_ns, info.st_size
        except FileNotFoundError:
            continue
    return None


def frontend():
    return _run_json('frontend')


def sync_devices(devices, active_id, revision=None):
    return _run_json('sync', {'devices': devices, 'active_id': active_id, 'revision': revision})


def select_device(row_id):
    current = frontend()
    return sync_devices(current['devices'], row_id, current['revision'])


def configure(options):
    current = frontend()
    devices = list(current.get('devices', []))
    target_id = options.get('device_id') or options.get('id', '')
    endpoint = options.get('endpoint', 'lan')

    new_row = connection_config(options)
    new_row['password'] = ''
    new_row.update({
        'device_id': target_id or f'kindle_{int(time.time())}',
        'alias': options.get('device_alias') or options.get('alias', 'Kindle'),
        'endpoint': endpoint,
        'documents': options.get('documents', '/mnt/us/documents'),
        'user_store': options.get('user_store') or os.path.dirname(options.get('documents', '/mnt/us/documents')),
    })
    if not new_row.get('id'):
        new_row['id'] = f"{new_row['device_id']}:{endpoint}"

    updated = False
    for idx, dev in enumerate(devices):
        if dev.get('id') == new_row['id'] or (dev.get('device_id') == new_row['device_id'] and dev.get('endpoint') == endpoint):
            devices[idx] = new_row
            updated = True
            break
    if not updated:
        devices.append(new_row)

    active = new_row['id'] if options.get('active', True) else current.get('active_id')
    return sync_devices(devices, active, current.get('revision'))


def _json_lines(process, timeout):
    """Read bounded NDJSON without hanging on a truncated CLI response."""
    deadline = time.monotonic() + timeout
    pending = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise RuntimeError('tailcatctl response timed out.')
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                if pending:
                    raise RuntimeError('tailcatctl returned a truncated JSON response.')
                return
            pending.extend(chunk)
            while b'\n' in pending:
                line, _, rest = pending.partition(b'\n')
                if len(line) > MAX_REPLY:
                    raise RuntimeError('tailcatctl returned an oversized response.')
                pending = bytearray(rest)
                yield json.loads(line)
            if len(pending) > MAX_REPLY:
                raise RuntimeError('tailcatctl returned an oversized response.')


class Lease:
    def __init__(self, process, diagnostics, control_path):
        self.process = process
        self.diagnostics = diagnostics
        self.control_path = control_path

    def is_alive(self):
        return self.process is not None and self.process.poll() is None

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        try:
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()
        self.diagnostics.close()


def acquire(options):
    config = connection_config(options)
    diagnostics = tempfile.TemporaryFile()
    process = subprocess.Popen(
        [executable(), 'lease', '--config', '-'], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=diagnostics,
    )
    lease = Lease(process, diagnostics, None)
    try:
        process.stdin.write(json.dumps(config).encode() + b'\n')
        process.stdin.flush()
        replies = _json_lines(process, config['timeout'] + 20)
        reply = next(replies, None)
        if not reply or reply.get('protocol') != 1 or not reply.get('ok'):
            diagnostics.seek(0)
            detail = diagnostics.read().decode('utf-8', 'replace').strip()
            raise RuntimeError((reply or {}).get('error') or detail or 'tailcatctl did not acquire a lease.')
        path = reply.get('control_path')
        if not isinstance(path, str) or not os.path.isabs(path):
            raise RuntimeError('tailcatctl did not return an SSH control socket.')
        lease.control_path = path
        return lease
    except Exception:
        lease.close()
        raise


def get_host_keys(options):
    return _run_json('host-keys', connection_config(options), options.get('timeout', 15) + 10)['keys']


def setup_tailcat(options, binary_path=None, license_path=None, arch='auto', force_new_key=False, logger=None):
    from calibre_plugins.kindle_sftp.provision import setup_tailcat as _setup
    return _setup(options, binary_path=binary_path, license_path=license_path,
                  arch=arch, force_new_key=force_new_key, logger=logger)


def uninstall_tailcat(options, logger=None):
    from calibre_plugins.kindle_sftp.provision import uninstall_tailcat as _uninstall
    return _uninstall(options, logger=logger)
