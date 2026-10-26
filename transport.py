"""OpenSSH-backed transport for the Kindle SFTP device plugin.

The transport intentionally delegates SSH authentication, host-key validation,
and SFTP protocol handling to the platform OpenSSH clients.  It does not use
paramiko (or any other third-party package), and never accepts an unknown host
key automatically.
"""

from __future__ import annotations

import io
import os
import posixpath
import re
import shlex
import shutil
import signal
import stat as stat_module
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable, Dict, List, Optional, Sequence, Tuple, Union

from calibre_plugins.kindle_sftp.broker import acquire


class TransportError(Exception):
    """Base class for transport failures."""


class TransportConnectionError(TransportError):
    """The SSH connection could not be established or was lost."""


class TransportAuthenticationError(TransportConnectionError):
    """SSH authentication failed."""


class TransportSecurityError(TransportConnectionError):
    """Host-key verification or path-confinement validation failed."""


class TransportCommandError(TransportError):
    """A remote command or SFTP batch command failed."""


class TransportPathError(TransportSecurityError):
    """A path is malformed or escapes the configured document root."""


@dataclass
class _Completed:
    returncode: int
    stdout: bytes
    stderr: bytes


PathLike = Union[str, os.PathLike]
UploadSource = Union[PathLike, BinaryIO]
UploadCallback = Callable[[int, int], Any]


class SSHTransport:
    """Small, dependency-free SSH/SFTP transport.

    ``options`` contains ``host``, ``port``, ``username``, ``identity_file``,
    ``password``, ``known_hosts``, ``timeout`` and ``documents``.  The optional
    ``transfer_timeout`` controls whole-book SFTP operations; ``ssh_executable``
    and ``sftp_executable`` may select explicit OpenSSH client paths.
    """

    def __init__(self, options: Dict[str, Any]):
        if not isinstance(options, dict):
            raise TypeError("SSHTransport options must be a dict")
        self._options = dict(options)
        self.host = self._validate_host(options.get("host"))
        self.port = self._validate_port(options.get("port", 22))
        self.username = self._validate_username(options.get("username"))
        self.identity_file = self._local_option_path(options.get("identity_file", ""))
        self.password = options.get("password") or ""
        if not isinstance(self.password, str):
            raise TypeError("password must be a string")
        if "\x00" in self.password or "\r" in self.password or "\n" in self.password:
            raise ValueError("password must not contain NUL or newline characters")
        known_hosts = options.get("known_hosts") or os.path.expanduser("~/.ssh/known_hosts")
        if not isinstance(known_hosts, str) or not known_hosts:
            raise ValueError("known_hosts must be a non-empty path")
        if "\x00" in known_hosts:
            raise ValueError("known_hosts contains NUL")
        self.known_hosts = os.path.abspath(os.path.expanduser(known_hosts))
        self.timeout = self._positive_int(options.get("timeout", 15), "timeout")
        self.transfer_timeout = self._positive_int(
            options.get("transfer_timeout", max(300, self.timeout)), "transfer_timeout"
        )
        self.documents = self._normalise_root(options.get("documents", "/mnt/us/documents"))
        user_store_val = options.get("user_store")
        if user_store_val:
            self.user_store = self._normalise_remote_path(user_store_val)
        else:
            self.user_store = posixpath.dirname(self.documents)

        ssh_value = options.get("ssh_executable", options.get("ssh_path", "ssh"))
        sftp_value = options.get("sftp_executable", options.get("sftp_path", "sftp"))
        self.ssh_executable = self._resolve_executable(ssh_value, "ssh")
        self.sftp_executable = self._resolve_executable(sftp_value, "sftp")

        self._runtime_dir: Optional[str] = None
        self._control_path: Optional[str] = None
        self._connected = False
        self._lease = None

    @staticmethod
    def _positive_int(value: Any, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return value

    @staticmethod
    def _validate_host(value: Any) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("host must be a non-empty string")
        if any(ch.isspace() or ord(ch) < 32 or ch == "\x7f" for ch in value):
            raise ValueError("host must not contain whitespace or control characters")
        if any(ch in value for ch in ("@", "%")) or value.startswith("-"):
            raise ValueError("host contains unsupported SSH destination characters")
        if value.startswith("[") or value.endswith("]"):
            if not (value.startswith("[") and value.endswith("]")):
                raise ValueError("invalid bracketed host")
        return value[1:-1] if value.startswith("[") else value

    @staticmethod
    def _validate_username(value: Any) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("username must be a non-empty string")
        if any(ch.isspace() or ord(ch) < 32 or ch == "\x7f" for ch in value):
            raise ValueError("username must not contain whitespace or control characters")
        if any(ch in value for ch in ("@", ":", "/")) or value.startswith("-"):
            raise ValueError("username contains unsupported SSH destination characters")
        return value

    @staticmethod
    def _validate_port(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
            raise ValueError("port must be an integer from 1 to 65535")
        return value

    @staticmethod
    def _local_option_path(value: Any) -> str:
        if value in (None, ""):
            return ""
        if not isinstance(value, str):
            raise TypeError("identity_file must be a path string")
        if "\x00" in value:
            raise ValueError("identity_file contains NUL")
        return os.path.abspath(os.path.expanduser(value))

    @staticmethod
    def _resolve_executable(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} executable must be a non-empty string")
        path = os.path.expanduser(value)
        resolved = path if os.path.dirname(path) else shutil.which(path)
        if not resolved or not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
            raise TransportConnectionError(f"{label} executable is not available: {value}")
        return os.path.abspath(resolved)

    @staticmethod
    def _normalise_root(value: Any) -> str:
        if not isinstance(value, str) or not value.startswith("/"):
            raise ValueError("documents must be an absolute POSIX path")
        if "\x00" in value:
            raise ValueError("documents contains NUL")
        if any(ord(ch) < 32 or ch == "\x7f" for ch in value):
            raise ValueError("documents must not contain control characters")
        normal = posixpath.normpath(value)
        if normal == "/" or any(part in (".", "..") for part in value.split("/")):
            raise ValueError("documents must not contain . or .. path components")
        return normal

    @property
    def _destination(self) -> str:
        return f"{self.username}@{self.host}"

    @property
    def _sftp_destination(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.username}@{host}"

    def _ensure_runtime(self) -> None:
        if self._runtime_dir is not None:
            return
        try:
            # Per-client transfer staging only; the Rust broker owns SSH sockets.
            self._runtime_dir = tempfile.mkdtemp(prefix="cks-", dir="/tmp")
        except OSError:
            self._runtime_dir = tempfile.mkdtemp(prefix="cks-")
        os.chmod(self._runtime_dir, 0o700)

    @staticmethod
    def _decode(data: bytes) -> str:
        return data.decode("utf-8", "replace").strip()

    def _run_process(
        self,
        args: List[str],
        *,
        input_data: Optional[bytes] = None,
        timeout: int,
        env: Optional[Dict[str, str]] = None,
    ) -> _Completed:
        try:
            process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            raise TransportConnectionError(f"could not start {args[0]}: {exc}") from exc
        try:
            stdout, stderr = process.communicate(input=input_data, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
            stdout, stderr = process.communicate()
            detail = self._decode(stderr) or "no diagnostic output"
            raise TransportConnectionError(f"SSH command timed out after {timeout}s: {detail}") from exc
        return _Completed(process.returncode, stdout, stderr)

    def _common_args(self, *, control: bool) -> List[str]:
        if self._control_path is None:
            raise TransportConnectionError("SSH transport is not connected")
        args = [
            "-F",
            "/dev/null",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.known_hosts}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            f"ConnectTimeout={self.timeout}",
            "-o",
            "ConnectionAttempts=1",
            "-o",
            "NumberOfPasswordPrompts=1",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=2",
            "-o",
            "ControlPersist=no",
            "-p",
            str(self.port),
        ]
        if control:
            args += ["-S", self._control_path]
        else:
            args += ["-o", f"ControlPath={self._control_path}", "-o", "ControlMaster=no"]
        args += ["-o", "ProxyCommand=false"]
        if self.identity_file:
            if not os.path.isfile(self.identity_file):
                raise TransportAuthenticationError(f"identity file does not exist: {self.identity_file}")
            args += ["-i", self.identity_file]
        return args

    def _process_failure(self, result: _Completed, operation: str) -> TransportError:
        detail = self._decode(result.stderr) or self._decode(result.stdout) or "no diagnostic output"
        lower = detail.lower()
        if "documents root" in lower and ("missing" in lower or "symlink" in lower):
            return TransportSecurityError(f"{operation} rejected the configured documents root: {detail}")
        if (
            "host key" in lower
            and any(token in lower for token in ("known", "verify", "offending"))
        ) or "host key verification failed" in lower or "man-in-the-middle" in lower or "incompatible connection parameters" in lower:
            return TransportSecurityError(
                f"{operation} rejected the host key; verify {self.known_hosts} contains the expected key: {detail}"
            )
        if any(token in lower for token in ("permission denied", "authentication failed", "could not authenticate")):
            return TransportAuthenticationError(f"{operation} authentication failed: {detail}")
        if any(token in lower for token in ("connection refused", "could not resolve hostname", "no route to host", "connection timed out")):
            return TransportConnectionError(f"{operation} could not reach the host: {detail}")
        return TransportCommandError(f"{operation} failed (exit {result.returncode}): {detail}")

    def connect(self) -> None:
        if self._connected and self.is_alive():
            return
        self.close()
        options = {
            **self._options, 'host': self.host, 'port': self.port,
            'username': self.username, 'identity_file': self.identity_file,
            'known_hosts': self.known_hosts, 'ssh_executable': self.ssh_executable,
            'sftp_executable': self.sftp_executable,
        }
        try:
            self._lease = acquire(options)
        except Exception as error:
            raise self._process_failure(
                _Completed(1, b"", str(error).encode()), "Shared SSH connection",
            ) from error
        self._control_path = self._lease.control_path
        self._connected = True
        try:
            self._check_root()
            self._sftp_batch(["pwd"], timeout=self.timeout)
        except Exception:
            self.close()
            raise

    def _control_operation(self, operation: str) -> _Completed:
        if self._control_path is None or not os.path.exists(self._control_path):
            return _Completed(1, b"", b"control socket is absent")
        args = [self.ssh_executable]
        args += self._common_args(control=True)
        args += ["-O", operation, self._destination]
        return self._run_process(args, timeout=self.timeout)

    def is_alive(self) -> bool:
        if not self._connected or self._control_path is None or self._lease is None or not self._lease.is_alive():
            return False
        result = self._control_operation("check")
        return result.returncode == 0

    def close(self) -> None:
        self._connected = False
        lease, self._lease = self._lease, None
        if lease is not None:
            lease.close()
        runtime = self._runtime_dir
        self._control_path = None
        self._runtime_dir = None
        if runtime:
            shutil.rmtree(runtime, ignore_errors=True)

    def _require_connection(self) -> None:
        if not self._connected:
            raise TransportConnectionError("SSH transport is not connected")
        if not self.is_alive():
            self._connected = False
            raise TransportConnectionError("SSH control connection is no longer alive")

    @staticmethod
    def _shell_quote(value: str) -> str:
        return shlex.quote(value)

    @staticmethod
    def _sftp_quote(value: str) -> str:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise TransportPathError("SFTP path must be a non-empty string without NUL")
        if any(
            ord(ch) < 32 or ch == "\x7f" or 0xD800 <= ord(ch) <= 0xDFFF
            for ch in value
        ):
            raise TransportPathError(
                "SFTP paths containing control or non-UTF-8 characters are unsupported"
            )
        # sftp parses its own command language, not a shell.  Double quotes
        # preserve spaces, apostrophes and '#'; its parser additionally
        # escapes glob metacharacters found inside quoted arguments.
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def _remote_command(self, command: str, *, timeout: Optional[int] = None, check: bool = True) -> _Completed:
        self._require_connection()
        args = [self.ssh_executable]
        args += self._common_args(control=False)
        args += ["-o", "BatchMode=yes"]
        args += [self._destination, command]
        result = self._run_process(args, timeout=timeout or self.timeout)
        if check and result.returncode:
            raise self._process_failure(result, "Remote command")
        return result

    def _sftp_batch(self, commands: List[str], *, timeout: Optional[int] = None) -> _Completed:
        self._require_connection()
        args = [self.sftp_executable, "-F", "/dev/null", "-q", "-b", "-"]
        args += ["-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={self.known_hosts}"]
        args += ["-o", "GlobalKnownHostsFile=/dev/null", "-o", f"ConnectTimeout={self.timeout}"]
        args += ["-o", f"ControlPath={self._control_path}", "-o", "ControlMaster=no",
                 "-o", "BatchMode=yes", "-o", "ProxyCommand=false"]
        args += ["-P", str(self.port)]
        if self.identity_file:
            args += ["-i", self.identity_file]
        args += [self._sftp_destination]
        batch = ("\n".join(commands) + "\n").encode("utf-8")
        result = self._run_process(args, input_data=batch, timeout=timeout or self.transfer_timeout)
        if result.returncode:
            raise self._process_failure(result, "SFTP batch")
        return result

    def _remote_path_state(self, path: str) -> Tuple[bool, Optional[str]]:
        """Return (exists, canonical_path) without confusing missing paths and errors."""
        quoted = self._shell_quote(path)
        command = (
            f"if [ -e {quoted} ] || [ -L {quoted} ]; then "
            f"resolved=$(readlink -f {quoted}) || exit 125; "
            "printf 'PRESENT\\t%s\\0' \"$resolved\"; "
            "else "
            f"ancestor={quoted}; "
            'while [ ! -d \"$ancestor\" ]; do '
            '[ \"$ancestor\" = / ] && exit 125; '
            'ancestor=${ancestor%/*}; [ -n \"$ancestor\" ] || ancestor=/; '
            'done; '
            '[ -x \"$ancestor\" ] || { printf \"%s\\n\" \"Permission denied resolving path\" >&2; exit 13; }; '
            "printf 'MISSING\\t\\0'; fi"
        )
        result = self._remote_command(command, check=False)
        if result.returncode:
            detail = self._decode(result.stderr).lower()
            if "no such file or directory" in detail or "does not exist" in detail:
                return False, None
            raise self._process_failure(result, "Remote path validation")
        records = result.stdout.split(b"\x00")
        if len(records) != 2 or b"\t" not in records[0]:
            raise TransportCommandError("remote path validation returned malformed output")
        status, raw_path = records[0].split(b"\t", 1)
        if status == b"MISSING":
            return False, None
        resolved = raw_path.decode("utf-8", "surrogateescape")
        if not resolved or "\n" in resolved or "\r" in resolved:
            raise TransportCommandError("remote path validation returned malformed path")
        if status == b"PRESENT":
            return True, posixpath.normpath(resolved)
        raise TransportCommandError("remote path validation returned an unknown status")

    def _remote_realpath(self, path: str) -> str:
        present, resolved = self._remote_path_state(path)
        if not present or resolved is None:
            raise FileNotFoundError(path)
        return resolved

    def _remote_realpath_optional(self, path: str) -> Optional[str]:
        _present, resolved = self._remote_path_state(path)
        return resolved if _present else None

    def _within_root(self, path: str) -> bool:
        return path == self.documents or path.startswith(self.documents + "/")

    def _check_root(self) -> None:
        present, resolved = self._remote_path_state(self.documents)
        if not present or resolved is None:
            raise TransportSecurityError(f"configured documents root does not exist: {self.documents}")
        if not self._within_root(resolved):
            raise TransportSecurityError(
                f"configured documents root resolves outside itself: {self.documents} -> {resolved}"
            )
        quoted = self._shell_quote(self.documents)
        result = self._remote_command(f"test -d {quoted} && test ! -L {quoted}", check=False)
        if result.returncode:
            raise TransportSecurityError(
                f"configured documents root is not a real directory or is a symlink: {self.documents}"
            )

    @staticmethod
    def _normalise_remote_path(value: Any) -> str:
        if not isinstance(value, str) or not value.startswith("/"):
            raise TransportPathError("remote path must be an absolute POSIX path")
        if "\x00" in value:
            raise TransportPathError("remote path contains NUL")
        if any(part in (".", "..") for part in value.split("/")):
            raise TransportPathError("remote path must not contain . or .. components")
        return posixpath.normpath(value)

    def _ensure_parent_directory(self, parent: str) -> None:
        if not self._within_root(parent):
            raise TransportPathError(f"remote parent is outside documents root: {parent}")
        relative = parent[len(self.documents):].strip("/")
        current = self.documents
        if relative:
            for component in relative.split("/"):
                current = posixpath.join(current, component)
                present, resolved = self._remote_path_state(current)
                if present:
                    if resolved is None or not self._within_root(resolved):
                        raise TransportSecurityError(
                            f"remote directory escapes documents root: {current} -> {resolved}"
                        )
                else:
                    if resolved is not None and not self._within_root(resolved):
                        raise TransportSecurityError(
                            f"remote directory escapes documents root: {current} -> {resolved}"
                        )
                    result = self._remote_command(f"mkdir {self._shell_quote(current)}", check=False)
                    if result.returncode:
                        # A concurrent creator is harmless; revalidate the
                        # resulting path before proceeding.
                        present, resolved = self._remote_path_state(current)
                        if not present:
                            raise self._process_failure(result, "Remote directory creation")
                    present, resolved = self._remote_path_state(current)
                    if resolved is None or not self._within_root(resolved):
                        raise TransportSecurityError(
                            f"created remote directory escapes documents root: {current} -> {resolved}"
                        )
                result = self._remote_command(f"test -d {self._shell_quote(current)}", check=False)
                if result.returncode:
                    raise self._process_failure(result, "Remote directory validation")

    def _prepare_target(
        self, path: Any, *, allow_root: bool = False, create_parents: bool = False
    ) -> str:
        target = self._normalise_remote_path(path)
        if any(ord(ch) < 32 or ch == "\x7f" or 0xD800 <= ord(ch) <= 0xDFFF for ch in target):
            raise TransportPathError("remote path contains unsupported control or non-UTF-8 characters")
        if not self._within_root(target) or (not allow_root and target == self.documents):
            raise TransportPathError(f"remote path is outside documents root: {target}")
        parent = posixpath.dirname(target)
        parent_present, parent_real = self._remote_path_state(parent)
        if not parent_present:
            if create_parents:
                self._ensure_parent_directory(parent)
                parent_present, parent_real = self._remote_path_state(parent)
            else:
                _target_present, target_real = self._remote_path_state(target)
                if target_real is not None and not self._within_root(target_real):
                    raise TransportSecurityError(
                        f"remote path resolves outside documents root: {target} -> {target_real}"
                    )
                return target
        if parent_real is None or not self._within_root(parent_real):
            raise TransportSecurityError(f"remote parent escapes documents root: {parent} -> {parent_real}")
        parent_result = self._remote_command(f"test -d {self._shell_quote(parent)}", check=False)
        if parent_result.returncode:
            raise self._process_failure(parent_result, "Remote parent validation")
        _target_present, existing_real = self._remote_path_state(target)
        if existing_real is not None and not self._within_root(existing_real):
            raise TransportSecurityError(f"remote path resolves outside documents root: {target} -> {existing_real}")
        return target

    def _existing_file(self, path: Any) -> str:
        target = self._prepare_target(path)
        quoted = self._shell_quote(target)
        result = self._remote_command(
            f"if [ -d {quoted} ] && [ ! -L {quoted} ]; then exit 4; fi; test -f {quoted}", check=False
        )
        if result.returncode:
            detail = self._decode(result.stderr).lower()
            if "no such file or directory" in detail or "does not exist" in detail or result.returncode == 1:
                # realpath validation above already distinguished inaccessible
                # paths; this branch is a genuine missing/non-regular target.
                raise FileNotFoundError(target)
            raise self._process_failure(result, "Remote file validation")
        return target

    def list_books(self) -> List[Dict[str, Any]]:
        self._require_connection()
        root = self._shell_quote(self.documents)
        listing_script = (
            'for path do '
            'size=$(stat -c %s "$path") || exit 1; '
            'mtime=$(stat -c %Y "$path") || exit 1; '
            'printf \'%s\\t%s\\t%s\\0\' "$size" "$mtime" "$path"; '
            'done'
        )
        result = self._remote_command(
            f"find {root} -type f -exec sh -c {self._shell_quote(listing_script)} sh {{}} \\;",
            timeout=self.transfer_timeout,
        )
        books: List[Dict[str, Any]] = []
        for record in result.stdout.split(b"\x00"):
            if not record:
                continue
            fields = record.split(b"\t", 2)
            if len(fields) != 3:
                raise TransportCommandError("remote file listing returned malformed stat record")
            try:
                size = int(fields[0])
                mtime = float(int(fields[1]))
            except ValueError as exc:
                raise TransportCommandError("remote file listing returned non-numeric stat fields") from exc
            path = posixpath.normpath(fields[2].decode("utf-8", "surrogateescape"))
            if not self._within_root(path):
                raise TransportSecurityError(f"remote listing escaped documents root: {path}")
            books.append({"path": path, "size": size, "mtime": mtime})
        return books

    def space(self) -> Tuple[int, int]:
        result = self._remote_command(f"df -Pk {self._shell_quote(self.documents)}")
        for raw_line in result.stdout.splitlines()[1:]:
            fields = raw_line.split()
            if len(fields) < 4:
                continue
            try:
                total_kib = int(fields[1])
                free_kib = int(fields[3])
            except (ValueError, IndexError):
                continue
            return total_kib * 1024, free_kib * 1024
        raise TransportCommandError("remote df returned no parseable filesystem record")

    def refresh_library(self, paths: Optional[Sequence[str]] = None) -> bool:
        """Return whether native indexing was requested; skip while KOReader is active.

        Specific paths use reScanFile, falling back to doFullScan on failure.
        Unknown foreground state preserves scanner errors rather than hiding them.
        """
        app = self._remote_command(
            "lipc-get-prop com.lab126.appmgrd activeApp", check=False,
        )
        context = self._remote_command(
            "lipc-get-prop com.lab126.appmgrd activeContext", check=False,
        )
        if (
            app.returncode == 0 and context.returncode == 0
            and app.stdout.strip() == b"tech.hackerdude.shell_integration.launcher"
            and context.stdout.strip().lower().endswith(b"/koreader.sh")
        ):
            return False

        if paths:
            all_succeeded = True
            for path in paths:
                target = self._prepare_target(path)
                quoted = self._shell_quote(target)
                res = self._remote_command(f"lipc-set-prop -i com.lab126.scanner reScanFile {quoted}", check=False)
                if res.returncode != 0:
                    all_succeeded = False
                    break
            if all_succeeded:
                return True

        self._remote_command("lipc-set-prop -i com.lab126.scanner doFullScan 1")
        return True

    def _choose_stage(self, parent: str) -> str:
        for _ in range(5):
            candidate = posixpath.join(parent, f".calibre-sftp-{uuid.uuid4().hex}.part")
            if self._remote_realpath_optional(candidate) is None:
                return candidate
        raise TransportCommandError("could not allocate a unique remote staging path")

    def _remove_stage(self, stage: str) -> None:
        try:
            self._sftp_batch(["rm " + self._sftp_quote(stage)])
        except Exception:
            pass

    @staticmethod
    def _local_source(source: UploadSource, runtime_dir: str) -> Tuple[str, int, Optional[str]]:
        if isinstance(source, (str, bytes, os.PathLike)):
            local = os.fspath(source)
            if isinstance(local, bytes):
                local = os.fsdecode(local)
            if "\x00" in local or any(ord(ch) < 32 or ord(ch) == 127 for ch in local):
                raise ValueError("local source path contains unsupported control characters")
            try:
                info = os.stat(local)
            except OSError as exc:
                raise TransportError(f"cannot read local upload source {local}: {exc}") from exc
            if not stat_module.S_ISREG(info.st_mode):
                raise TransportError(f"local upload source is not a regular file: {local}")
            return local, info.st_size, None
        if not hasattr(source, "read"):
            raise TypeError("upload source must be a local path or readable binary stream")
        fd, temp_path = tempfile.mkstemp(prefix="upload-", dir=runtime_dir)
        total = 0
        try:
            with os.fdopen(fd, "wb") as output:
                while True:
                    chunk = source.read(1024 * 1024)
                    if chunk == b"":
                        break
                    if isinstance(chunk, memoryview):
                        chunk = chunk.tobytes()
                    if not isinstance(chunk, bytes):
                        raise TypeError("upload stream must return bytes")
                    output.write(chunk)
                    total += len(chunk)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
        return temp_path, total, temp_path

    def upload(self, local_path_or_readable_stream: UploadSource, remote_path: Any, callback: Optional[UploadCallback] = None) -> None:
        self._require_connection()
        self._ensure_runtime()
        local_path, total, temporary_source = self._local_source(
            local_path_or_readable_stream, self._runtime_dir or ""
        )
        try:
            target = self._prepare_target(remote_path, create_parents=True)
            parent = posixpath.dirname(target)
            stage = self._choose_stage(parent)
            try:
                if callback is not None:
                    callback(0, total)
                self._sftp_batch(["put " + self._sftp_quote(local_path) + " " + self._sftp_quote(stage)])
                # A same-directory POSIX rename replaces an existing book atomically.
                # Using the shell's quoted mv avoids sftp's glob-aware rename parser.
                self._prepare_target(target)
                quoted_target = self._shell_quote(target)
                self._remote_command(
                    f"test ! -d {quoted_target} && "
                    f"mv -f {self._shell_quote(stage)} {quoted_target}"
                )
                if callback is not None:
                    callback(total, total)
            except Exception:
                self._remove_stage(stage)
                raise
        finally:
            if temporary_source:
                try:
                    os.unlink(temporary_source)
                except OSError:
                    pass

    def download(self, remote_path: Any, writable_stream: BinaryIO) -> None:
        if not hasattr(writable_stream, "write"):
            raise TypeError("download destination must be a writable binary stream")
        target = self._existing_file(remote_path)
        self._ensure_runtime()
        fd, local_temp = tempfile.mkstemp(prefix="download-", dir=self._runtime_dir)
        os.close(fd)
        try:
            self._sftp_batch(["get " + self._sftp_quote(target) + " " + self._sftp_quote(local_temp)])
            with open(local_temp, "rb") as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    writable_stream.write(chunk)
        finally:
            try:
                os.unlink(local_temp)
            except OSError:
                pass

    def read_bytes(self, path: Any) -> bytes:
        output = io.BytesIO()
        self.download(path, output)
        return output.getvalue()

    def write_bytes_atomic(self, path: Any, data: Union[bytes, bytearray, memoryview]) -> None:
        if isinstance(data, memoryview):
            data = data.tobytes()
        elif isinstance(data, bytearray):
            data = bytes(data)
        if not isinstance(data, bytes):
            raise TypeError("write_bytes_atomic data must be bytes-like")
        self.upload(io.BytesIO(data), path)

    def upload_thumbnail(
        self,
        filename: str,
        data: Union[bytes, bytearray, memoryview],
    ) -> None:
        """Upload a companion cover thumbnail to the Kindle's thumbnail store.

        Kindle cover thumbnails are sideloaded into `/mnt/us/system/thumbnails/`
        and cached in `/mnt/us/amazon-cover-bug/` to survive Amazon sync purges.
        The filename must strictly follow Amazon's `thumbnail_<uuid>_<cdetype>_portrait.jpg`
        naming pattern.
        """
        self._require_connection()
        if not isinstance(filename, str):
            raise TypeError("thumbnail filename must be a string")
        if not re.match(r"^thumbnail_[A-Za-z0-9._-]+_portrait\.jpg$", filename):
            raise ValueError(f"invalid Kindle thumbnail filename: {filename!r}")
        if isinstance(data, memoryview):
            data = data.tobytes()
        elif isinstance(data, bytearray):
            data = bytes(data)
        if not isinstance(data, bytes) or len(data) == 0:
            raise ValueError("thumbnail data must be non-empty bytes")

        self._ensure_runtime()
        fd, local_temp = tempfile.mkstemp(prefix="thumb-", dir=self._runtime_dir)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)

            destinations = [
                posixpath.join(self.user_store, "system", "thumbnails"),
                posixpath.join(self.user_store, "amazon-cover-bug"),
            ]
            errors = []
            for dest_dir in destinations:
                try:
                    quoted_dest = self._shell_quote(dest_dir)
                    self._remote_command(f"mkdir -p {quoted_dest}")

                    target = posixpath.join(dest_dir, filename)
                    stage = posixpath.join(dest_dir, f".thumb-{uuid.uuid4().hex}.part")
                    try:
                        self._sftp_batch(["put " + self._sftp_quote(local_temp) + " " + self._sftp_quote(stage)])
                        quoted_stage = self._shell_quote(stage)
                        quoted_target = self._shell_quote(target)
                        self._remote_command(f"mv -f {quoted_stage} {quoted_target}")
                    except Exception:
                        try:
                            self._sftp_batch(["rm " + self._sftp_quote(stage)])
                        except Exception:
                            pass
                        raise
                except Exception as exc:
                    errors.append(exc)

            if len(errors) == len(destinations):
                raise errors[0]
        finally:
            try:
                os.unlink(local_temp)
            except OSError:
                pass

    def upload_user_store_file(
        self,
        local_source: UploadSource,
        rel_path: str,
        mode: Optional[int] = None,
    ) -> str:
        """Upload a file to a path relative to user_store (/mnt/us) atomically."""
        self._require_connection()
        norm_rel = posixpath.normpath(rel_path.lstrip("/"))
        if norm_rel.startswith("..") or any(part == ".." for part in norm_rel.split("/")):
            raise TransportSecurityError(f"rel_path escapes user store: {rel_path}")
        target = posixpath.join(self.user_store, norm_rel)

        self._ensure_runtime()
        local_path, _total, temporary_source = self._local_source(
            local_source, self._runtime_dir or ""
        )
        try:
            dest_dir = posixpath.dirname(target)
            quoted_dest = self._shell_quote(dest_dir)
            self._remote_command(f"mkdir -p {quoted_dest}")

            stage = posixpath.join(dest_dir, f".stage-{uuid.uuid4().hex}.part")
            try:
                self._sftp_batch(["put " + self._sftp_quote(local_path) + " " + self._sftp_quote(stage)])
                quoted_stage = self._shell_quote(stage)
                quoted_target = self._shell_quote(target)
                self._remote_command(f"mv -f {quoted_stage} {quoted_target}")
                if mode is not None:
                    self._remote_command(f"chmod {oct(mode)[2:]} {quoted_target}")
            except Exception:
                try:
                    self._sftp_batch(["rm " + self._sftp_quote(stage)])
                except Exception:
                    pass
                raise
            return target
        finally:
            if temporary_source:
                try:
                    os.unlink(local_path)
                except OSError:
                    pass

    def remove(self, path: Any) -> None:
        target = self._prepare_target(path)
        existing = self._remote_realpath_optional(target)
        if existing is None:
            raise FileNotFoundError(target)
        if not self._within_root(existing):
            raise TransportSecurityError(f"refusing to remove path outside documents root: {target} -> {existing}")
        quoted = self._shell_quote(target)
        result = self._remote_command(
            f"if [ -d {quoted} ] && [ ! -L {quoted} ]; then exit 4; fi; test -e {quoted} || test -L {quoted}",
            check=False,
        )
        if result.returncode:
            raise self._process_failure(result, "Remote delete validation")
        self._sftp_batch(["rm " + self._sftp_quote(target)])

    def __enter__(self) -> "SSHTransport":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()


def is_tailcat_address(host: str) -> bool:
    """Return True if host is formatted as a Tailcat server address."""
    if not host or not isinstance(host, str):
        return False
    host = host.strip()
    return host.startswith("tc") and "." not in host and len(host) >= 20


