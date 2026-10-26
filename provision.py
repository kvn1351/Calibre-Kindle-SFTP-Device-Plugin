"""Kindle-side Tailcat provisioning over SSH/SFTP.

Manages uploading the Tailcat ARM binary to /mnt/us/tailcat, generating
persistent keys, creating start/stop scripts, KUAL menu, and shortcut launchers.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from calibre_plugins.kindle_sftp import broker

TAILCAT_VERSION = "v0.7.0"
TAILCAT_RELEASE_BASE = "https://github.com/tailscale/tailcat/releases/download"
MAX_RELEASE_BYTES = 128 * 1024 * 1024


def _shell_quote(value: str) -> str:
    return shlex.quote(value)


def _sftp_quote(value: str) -> str:
    escaped = value.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _validate_remote_path(name: str, path: str) -> str:
    normalized = posixpath.normpath(path.strip())
    if not normalized.startswith('/') or any(p in ('..', '.') for p in normalized.split('/')):
        raise ValueError(f"{name} must be a valid absolute path: {path}")
    return normalized


def _ensure_within(child: str, parent: str, name: str) -> None:
    if child != parent and not child.startswith(parent.rstrip('/') + '/'):
        raise ValueError(f"{name} ({child}) escapes parent ({parent})")


class RemoteSession:
    def __init__(self, options: Dict[str, Any]):
        self.options = options
        self.lease = broker.acquire(options)
        self.control_path = self.lease.control_path
        self.ssh_exe = options.get('ssh_executable') or 'ssh'
        self.sftp_exe = options.get('sftp_executable') or 'sftp'
        self.host = options['host']
        self.port = str(options.get('port', 22))
        self.username = options.get('username', 'root')
        self.identity_file = options.get('identity_file')

    def close(self):
        if self.lease:
            self.lease.close()
            self.lease = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def run_remote(self, cmd: str) -> str:
        cmd_args = [
            self.ssh_exe, "-F", "/dev/null",
            "-o", f"ControlPath={self.control_path}",
            "-o", "ControlMaster=no",
            "-o", "ProxyCommand=false",
            "-o", "BatchMode=yes",
            "-p", self.port,
        ]
        if self.identity_file and os.path.isfile(self.identity_file):
            cmd_args += ["-i", self.identity_file]
        destination = f"{self.username}@{self.host}"
        cmd_args += [destination, cmd]

        res = subprocess.run(cmd_args, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            err = res.stderr.strip() or res.stdout.strip()
            raise RuntimeError(f"remote command failed ({res.returncode}): {err}")
        return res.stdout

    def run_remote_allow_failure(self, cmd: str) -> Tuple[bool, str, str]:
        cmd_args = [
            self.ssh_exe, "-F", "/dev/null",
            "-o", f"ControlPath={self.control_path}",
            "-o", "ControlMaster=no",
            "-o", "ProxyCommand=false",
            "-o", "BatchMode=yes",
            "-p", self.port,
        ]
        if self.identity_file and os.path.isfile(self.identity_file):
            cmd_args += ["-i", self.identity_file]
        destination = f"{self.username}@{self.host}"
        cmd_args += [destination, cmd]

        res = subprocess.run(cmd_args, capture_output=True, text=True, check=False)
        return (res.returncode == 0, res.stdout, res.stderr)

    def upload_atomic(self, data: bytes, remote_path: str, mode: int) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(data)
            tmp.flush()
            local_tmp = tmp.name

        try:
            nonce = int(time.time() * 1000)
            remote_tmp = f"{remote_path}.tmp-{os.getpid()}-{nonce}"
            batch = (
                f"put {_sftp_quote(local_tmp)} {_sftp_quote(remote_tmp)}\n"
                f"chmod {oct(mode)[2:]} {_sftp_quote(remote_tmp)}\n"
                f"rename {_sftp_quote(remote_tmp)} {_sftp_quote(remote_path)}\n"
            )
            sftp_args = [
                self.sftp_exe, "-b", "-", "-F", "/dev/null",
                "-o", f"ControlPath={self.control_path}",
                "-o", "ControlMaster=no",
                "-o", "ProxyCommand=false",
                "-o", "BatchMode=yes",
                "-P", self.port,
            ]
            if self.identity_file and os.path.isfile(self.identity_file):
                sftp_args += ["-i", self.identity_file]
            destination = f"{self.username}@{self.host}"
            sftp_args.append(destination)

            res = subprocess.run(sftp_args, input=batch.encode('utf-8'), capture_output=True, check=False)
            if res.returncode != 0:
                err = res.stderr.decode('utf-8', 'replace').strip()
                raise RuntimeError(f"SFTP upload failed ({res.returncode}): {err}")
        finally:
            if os.path.exists(local_tmp):
                os.unlink(local_tmp)


def _remote_storage_guard(session: RemoteSession, user_store: str, documents: str, tailcat: str, exact: bool) -> None:
    if exact:
        cmd = (
            f"root=$(readlink -f {_shell_quote(user_store)}) && "
            f"for target in {_shell_quote(documents)} {_shell_quote(tailcat)}; do "
            f"real=$(readlink -f \"$target\") || exit 1; "
            f"case \"$real\" in \"$root\"|\"$root\"/*) ;; *) exit 2 ;; esac; done"
        )
    else:
        cmd = (
            f"root=$(readlink -f {_shell_quote(user_store)}) && "
            f"for target in {_shell_quote(documents)} {_shell_quote(tailcat)}; do "
            f"nearest=\"$target\"; while [ ! -e \"$nearest\" ]; do next=${{nearest%/*}}; "
            f"[ \"$next\" != \"$nearest\" ] || exit 1; nearest=\"$next\"; done; "
            f"real=$(readlink -f \"$nearest\") || exit 1; "
            f"case \"$real\" in \"$root\"|\"$root\"/*) ;; *) exit 2 ;; esac; done"
        )
    session.run_remote(cmd)


def _parse_tailcat_address(text: str) -> Optional[str]:
    for token in text.split():
        clean = token.strip().strip("'\"()[]{}<>,;:")
        if clean.startswith("tc") and len(clean) >= 20 and all(c.isalnum() or c in '-_' for c in clean):
            return clean
    return None


def _start_script(directory: str, binary: str, key: str, addr: str) -> str:
    return (
        f"#!/bin/sh\n"
        f"# Name: Calibre - Start Tailcat\n"
        f"# Author: @kvn1351\n"
        f"DIR={_shell_quote(directory)}\n"
        f"BIN={_shell_quote(binary)}\n"
        f"KEY={_shell_quote(key)}\n"
        f"PIDFILE=\"$DIR/tailcat.pid\"\n"
        f"LOGFILE=\"$DIR/tailcat.log\"\n"
        f"ADDRFILE={_shell_quote(addr)}\n\n"
        f"if [ -f \"$PIDFILE\" ]; then\n"
        f"    PID=$(cat \"$PIDFILE\")\n"
        f"    if [ -n \"$PID\" ] && kill -0 \"$PID\" 2>/dev/null; then\n"
        f"        echo \"Tailcat is already running (PID $PID).\"\n"
        f"        exit 0\n"
        f"    fi\n"
        f"    rm -f \"$PIDFILE\"\n"
        f"fi\n\n"
        f"if [ ! -x \"$BIN\" ]; then chmod +x \"$BIN\" 2>/dev/null; fi\n"
        f"if [ ! -f \"$KEY\" ]; then echo \"Tailcat key not found at $KEY. Run setup wizard first.\"; exit 1; fi\n"
        f"export TAILCAT_ADDR_FILE=\"$ADDRFILE\"\n"
        f"nohup \"$BIN\" serve --key=\"$KEY\" 22 > \"$LOGFILE\" 2>&1 &\n"
        f"PID=$!\n"
        f"echo \"$PID\" > \"$PIDFILE\"\n"
        f"echo \"Tailcat started (PID $PID).\"\n"
    )


def _stop_script(directory: str) -> str:
    return (
        f"#!/bin/sh\n"
        f"# Name: Calibre - Stop Tailcat\n"
        f"# Author: @kvn1351\n"
        f"DIR={_shell_quote(directory)}\n"
        f"PIDFILE=\"$DIR/tailcat.pid\"\n"
        f"if [ -f \"$PIDFILE\" ]; then\n"
        f"    PID=$(cat \"$PIDFILE\")\n"
        f"    if [ -n \"$PID\" ]; then kill \"$PID\" 2>/dev/null; sleep 1; kill -9 \"$PID\" 2>/dev/null; fi\n"
        f"    rm -f \"$PIDFILE\"\n"
        f"    echo \"Tailcat stopped.\"\n"
        f"else\n"
        f"    echo \"Tailcat is not running (no PID file).\"\n"
        f"    exit 1\n"
        f"fi\n"
    )


def _install_kual_if_present(session: RemoteSession, user_store: str, start: str, stop: str, log: Callable[[str], None]) -> None:
    extensions = posixpath.join(user_store, "extensions")
    ok, _, _ = session.run_remote_allow_failure(f"test -d {_shell_quote(extensions)}")
    if not ok:
        return
    kual = posixpath.join(extensions, "tailcat")
    session.run_remote(f"mkdir -p {_shell_quote(kual)}")
    config = """<?xml version="1.0" encoding="UTF-8"?>
<extension>
	<information>
		<name>Calibre Tailcat</name>
		<version>1.0</version>
		<author>@kvn1351</author>
		<id>tailcat</id>
	</information>
	<menus>
		<menu type="json" dynamic="true">menu.json</menu>
	</menus>
</extension>
"""
    menu = json.dumps({
        "items": [{
            "name": "Calibre Tailcat",
            "priority": 1,
            "items": [
                {"name": "Start Calibre Tailcat", "priority": 1, "action": start},
                {"name": "Stop Calibre Tailcat", "priority": 2, "action": stop},
            ]
        }]
    }, indent=2)
    session.upload_atomic(config.encode('utf-8'), posixpath.join(kual, "config.xml"), 0o644)
    session.upload_atomic(menu.encode('utf-8'), posixpath.join(kual, "menu.json"), 0o644)
    log("Installed the optional KUAL Tailcat menu.")


def _refresh_shortcuts(session: RemoteSession, documents: str, log: Callable[[str], None]) -> None:
    ok_app, app_out, _ = session.run_remote_allow_failure("lipc-get-prop com.lab126.appmgrd activeApp 2>/dev/null")
    ok_ctx, ctx_out, _ = session.run_remote_allow_failure("lipc-get-prop com.lab126.appmgrd activeContext 2>/dev/null")
    if ok_app and ok_ctx and ctx_out.strip().endswith("/KOReader.sh"):
        log("Skipped Library shortcut scanner refresh while KOReader is active.")
        return
    for script_name in ("Calibre Start Tailcat.sh", "Calibre Stop Tailcat.sh"):
        path = posixpath.join(documents, script_name)
        session.run_remote_allow_failure(f"lipc-set-prop -i com.lab126.scanner reScanFile {_shell_quote(path)}")


def enroll_host_key(host: str, port: int, address: str, known_hosts_path: str) -> bool:
    if not known_hosts_path or not os.path.isfile(known_hosts_path):
        return False
    normalized = host.strip().strip("[]")
    query = normalized if port == 22 else f"[{normalized}]:{port}"

    res = subprocess.run(["ssh-keygen", "-F", query, "-f", known_hosts_path], capture_output=True, text=True, check=False)
    if res.returncode != 0 or not res.stdout.strip():
        return False

    trusted_keys = []
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and not parts[0].startswith('#') and not parts[0].startswith('@'):
            trusted_keys.append((parts[1], parts[2]))
    if not trusted_keys:
        return False

    host_token = f"{address.lower()},{address}"
    content = Path(known_hosts_path).read_text(encoding='utf-8', errors='replace')
    new_lines = []
    for key_type, key_data in trusted_keys:
        full_entry = f"{host_token} {key_type} {key_data}"
        if full_entry not in content:
            new_lines.append(full_entry + "\n")

    if new_lines:
        with open(known_hosts_path, "a", encoding="utf-8") as f:
            if content and not content.endswith('\n'):
                f.write('\n')
            for line in new_lines:
                f.write(line)
        return True
    return False


def setup_tailcat(
    options: Dict[str, Any],
    binary_path: Optional[str] = None,
    license_path: Optional[str] = None,
    arch: str = "auto",
    force_new_key: bool = False,
    logger: Optional[Callable[[str], None]] = None,
) -> str:
    log = logger or (lambda msg: None)
    user_store = _validate_remote_path("user_store", options.get("user_store") or "/mnt/us")
    documents = _validate_remote_path("documents", options.get("documents") or "/mnt/us/documents")
    _ensure_within(documents, user_store, "documents")

    with RemoteSession(options) as session:
        remote_tailcat = posixpath.join(user_store, "tailcat")
        remote_bin = posixpath.join(remote_tailcat, "bin/tailcat")
        remote_key = posixpath.join(remote_tailcat, "kindle.private.json")
        remote_addr = posixpath.join(remote_tailcat, "tailcat.addr")
        remote_start = posixpath.join(remote_tailcat, "start-tailcat.sh")
        remote_stop = posixpath.join(remote_tailcat, "stop-tailcat.sh")
        remote_extensions = posixpath.join(user_store, "extensions")

        _remote_storage_guard(session, user_store, user_store, remote_extensions, False)
        session.run_remote(f"mkdir -p {_shell_quote(documents)} {_shell_quote(remote_tailcat)} {_shell_quote(posixpath.join(remote_tailcat, 'bin'))}")
        _remote_storage_guard(session, user_store, documents, remote_tailcat, True)

        # Upload binary
        if binary_path and os.path.isfile(binary_path):
            bin_data = Path(binary_path).read_bytes()
        else:
            raise RuntimeError("Tailcat binary path not found or not provided.")

        log("Uploading Tailcat executable atomically...")
        session.upload_atomic(bin_data, remote_bin, 0o755)

        if license_path and os.path.isfile(license_path):
            log("Uploading Tailcat license atomically...")
            session.upload_atomic(Path(license_path).read_bytes(), posixpath.join(remote_tailcat, "LICENSE"), 0o644)

        # Check existing key
        key_exists, _, _ = session.run_remote_allow_failure(f"test -f {_shell_quote(remote_key)}")
        address = None
        if not force_new_key and key_exists:
            ok, addr_out, _ = session.run_remote_allow_failure(f"cat {_shell_quote(remote_addr)}")
            if ok:
                address = _parse_tailcat_address(addr_out)

        if force_new_key or not key_exists:
            log("Generating permanent Tailcat key...")
            force_flag = " --force" if force_new_key else ""
            res = session.run_remote(f"{_shell_quote(remote_bin)} genkey --fixed-region{force_flag} --key={_shell_quote(remote_key)}")
            address = _parse_tailcat_address(res)
            if not address:
                raise RuntimeError("Tailcat genkey completed without returning a valid server address")
        elif not address:
            log("Recovering the permanent address from the saved Tailcat key...")
            probe_cmd = (
                f"tmp={_shell_quote(remote_tailcat)}/.address-probe.$$; "
                f"TAILCAT_ADDR_FILE=\"$tmp\" {_shell_quote(remote_bin)} serve --key={_shell_quote(remote_key)} --json 22 >/dev/null 2>&1 & "
                f"p=$!; i=0; while [ $i -lt 3 ]; do [ -s \"$tmp\" ] && break; kill -0 $p 2>/dev/null || break; sleep 1; i=$((i+1)); done; "
                f"kill $p 2>/dev/null; wait $p 2>/dev/null; cat \"$tmp\" 2>/dev/null; rm -f \"$tmp\""
            )
            _, probe_out, _ = session.run_remote_allow_failure(probe_cmd)
            address = _parse_tailcat_address(probe_out)

        if not address:
            raise RuntimeError("Could not obtain or recover a valid Tailcat address")

        session.upload_atomic(f"{address}\n".encode('utf-8'), remote_addr, 0o644)

        log("Installing Calibre Tailcat start/stop launchers...")
        start_sh = _start_script(remote_tailcat, remote_bin, remote_key, remote_addr)
        stop_sh = _stop_script(remote_tailcat)
        session.upload_atomic(start_sh.encode('utf-8'), remote_start, 0o755)
        session.upload_atomic(stop_sh.encode('utf-8'), remote_stop, 0o755)

        doc_start = f"#!/bin/sh\n# Name: Calibre - Start Tailcat\n# Author: @kvn1351\nexec {_shell_quote(remote_start)}\n"
        doc_stop = f"#!/bin/sh\n# Name: Calibre - Stop Tailcat\n# Author: @kvn1351\nexec {_shell_quote(remote_stop)}\n"
        session.upload_atomic(doc_start.encode('utf-8'), posixpath.join(documents, "Calibre Start Tailcat.sh"), 0o755)
        session.upload_atomic(doc_stop.encode('utf-8'), posixpath.join(documents, "Calibre Stop Tailcat.sh"), 0o755)

        session.run_remote_allow_failure(f"rm -f {_shell_quote(posixpath.join(documents, 'Start Tailcat.sh'))} {_shell_quote(posixpath.join(documents, 'Stop Tailcat.sh'))}")
        _refresh_shortcuts(session, documents, log)
        _install_kual_if_present(session, user_store, remote_start, remote_stop, log)

        # Host key enrollment
        known_hosts = options.get('known_hosts')
        if known_hosts:
            enroll_host_key(options['host'], int(options.get('port', 22)), address, os.path.expanduser(known_hosts))

        # Save to tailcatctl registry
        cfg = broker.connection_config(options)
        cfg['password'] = ''
        dev_id = options.get('device_id') or options.get('id', '')
        if not dev_id:
            try:
                host_str = str(options.get('host') or '').strip('[]')
                port_num = int(options.get('port', 22) or 22)
                for d in broker.frontend().get('devices', []):
                    d_host = str(d.get('host') or '').strip('[]')
                    d_port = int(d.get('port', 22) or 22)
                    if d_host == host_str and d_port == port_num:
                        dev_id = d.get('device_id') or d.get('id', '').split(':')[0]
                        if dev_id:
                            break
            except Exception:
                pass
        if not dev_id:
            dev_id = f"kindle_{int(time.time())}"
        cfg.update({
            'device_id': dev_id,
            'alias': options.get('device_alias') or options.get('alias', 'Kindle'),
            'documents': documents,
            'user_store': user_store,
        })
        broker.configure(cfg)
        # Also configure the tailcat endpoint under the same device
        tc_cfg = dict(cfg)
        tc_cfg['device_id'] = dev_id
        tc_cfg['host'] = address
        tc_cfg['port'] = 22
        tc_cfg['endpoint'] = 'tailcat'
        broker.configure(tc_cfg)

        return address


def _reject_active_broker_peers(options: Dict[str, Any]) -> None:
    try:
        status = broker._run_json("status", args=["--json"])
    except Exception:
        status = {}
    host = str(options.get("host") or "").strip("[]")
    port = int(options.get("port", 22) or 22)
    user = str(options.get("username") or "")
    for conn in status.get("connections", []):
        conn_host = str(conn.get("host") or "").strip("[]")
        conn_port = int(conn.get("port", 22) or 22)
        conn_user = str(conn.get("username") or "")
        leases = int(conn.get("leases") or conn.get("lease_count") or conn.get("clients") or 0)
        host_match = (not host or not conn_host or conn_host == host)
        port_match = (not port or not conn_port or conn_port == port)
        user_match = (not user or not conn_user or conn_user == user)
        if host_match and port_match and user_match:
            if leases > 0:
                raise RuntimeError("cannot uninstall while another broker client holds the LAN lease")


def uninstall_tailcat(options: Dict[str, Any], logger: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    log = logger or (lambda msg: None)
    user_store = _validate_remote_path("user_store", options.get("user_store") or "/mnt/us")
    documents = _validate_remote_path("documents", options.get("documents") or "/mnt/us/documents")
    _ensure_within(documents, user_store, "documents")

    _reject_active_broker_peers(options)

    with RemoteSession(options) as session:
        tailcat = posixpath.join(user_store, "tailcat")
        binary = posixpath.join(tailcat, "bin/tailcat")
        pid_file = posixpath.join(tailcat, "tailcat.pid")
        extensions = posixpath.join(user_store, "extensions")
        start_launcher = posixpath.join(documents, "Calibre Start Tailcat.sh")
        stop_launcher = posixpath.join(documents, "Calibre Stop Tailcat.sh")

        _remote_storage_guard(session, user_store, documents, tailcat, False)
        _remote_storage_guard(session, user_store, user_store, extensions, False)

        tailcat_symlink = session.run_remote_allow_failure(
            f"test -L {_shell_quote(tailcat)} || test -h {_shell_quote(tailcat)}"
        )[0]
        if tailcat_symlink:
            raise RuntimeError("refusing to uninstall a symlinked Tailcat installation")

        extensions_symlink = session.run_remote_allow_failure(
            f"test -L {_shell_quote(extensions)} || test -h {_shell_quote(extensions)}"
        )[0]
        if extensions_symlink:
            raise RuntimeError("refusing to uninstall through a symlinked extensions directory")

        tailcat_present = session.run_remote_allow_failure(
            f"test -e {_shell_quote(tailcat)} || test -L {_shell_quote(tailcat)}"
        )[0]
        if tailcat_present:
            _remote_storage_guard(session, user_store, user_store, tailcat, True)
            binary_symlink = session.run_remote_allow_failure(
                f"test -L {_shell_quote(binary)} || test -h {_shell_quote(binary)}"
            )[0]
            if binary_symlink:
                raise RuntimeError("refusing to stop a symlinked Tailcat executable")

            log("Stopping running Tailcat process on device...")
            stop_cmd = (
                f"if [ ! -f {_shell_quote(pid_file)} ]; then exit 0; fi; "
                f"pid=$(cat {_shell_quote(pid_file)}) || exit 10; "
                f"case \"$pid\" in ''|*[!0-9]*) exit 10;; esac; "
                f"if ! kill -0 \"$pid\" 2>/dev/null; then exit 0; fi; "
                f"expected=$(readlink -f {_shell_quote(binary)}) || exit 11; "
                f"actual=$(readlink -f \"/proc/$pid/exe\") || exit 12; "
                f"[ \"$actual\" = \"$expected\" ] || exit 13; "
                f"kill \"$pid\" 2>/dev/null || exit 14; "
                f"i=0; while kill -0 \"$pid\" 2>/dev/null && [ $i -lt 5 ]; do sleep 1; i=$((i+1)); done; "
                f"if kill -0 \"$pid\" 2>/dev/null; then kill -9 \"$pid\" 2>/dev/null || exit 15; fi"
            )
            ok, stdout, stderr = session.run_remote_allow_failure(stop_cmd)
            if not ok:
                raise RuntimeError("refusing to stop a Tailcat process whose executable could not be verified")

        log("Removing Tailcat files and scriptlets...")
        session.run_remote(f"rm -rf {_shell_quote(tailcat)} {_shell_quote(start_launcher)} {_shell_quote(stop_launcher)}")

        kual_dir = posixpath.join(extensions, "tailcat")
        session.run_remote_allow_failure(f"rm -rf {_shell_quote(kual_dir)}")

        _refresh_shortcuts(session, documents, log)

    # Remove Tailcat endpoint from tailcatctl registry, keeping LAN endpoint
    device_id = options.get("device_id") or options.get("id", "")
    try:
        current = broker.frontend()
        devices = current.get("devices", [])
        if not device_id:
            host = str(options.get("host") or "").strip("[]")
            port = int(options.get("port", 22) or 22)
            for d in devices:
                d_host = str(d.get("host") or "").strip("[]")
                d_port = int(d.get("port", 22) or 22)
                if (not host or d_host == host) and (not port or d_port == port):
                    device_id = d.get("device_id") or d.get("id", "")
                    if device_id:
                        break
        if device_id:
            base_id = device_id.split(":")[0]
            updated_devices = [
                d for d in devices
                if not ((d.get("device_id") == base_id or d.get("id") == base_id or d.get("id") == f"{base_id}:tailcat")
                        and d.get("endpoint") == "tailcat")
            ]
            active_id = current.get("active_id")
            if active_id and active_id.endswith(":tailcat"):
                act_base = active_id.split(":")[0]
                if act_base == base_id:
                    active_id = f"{act_base}:lan"
            broker.sync_devices(updated_devices, active_id, current.get("revision"))
    except Exception as exc:
        log(f"Warning: could not update registry during uninstall: {exc}")

    return {"ok": True, "event": "result"}
