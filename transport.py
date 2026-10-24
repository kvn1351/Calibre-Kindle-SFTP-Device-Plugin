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
import socket
import stat as stat_module
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable, Dict, List, Optional, Sequence, Tuple, Union


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
            # OpenSSH control sockets have a short AF_UNIX path limit.  Keep
            # the private runtime directory directly under /tmp rather than
            # inheriting Calibre's potentially deep temporary-directory path.
            self._runtime_dir = tempfile.mkdtemp(prefix="cks-", dir="/tmp")
        except OSError:
            self._runtime_dir = tempfile.mkdtemp(prefix="cks-")
        os.chmod(self._runtime_dir, 0o700)
        self._control_path = os.path.join(self._runtime_dir, "m.sock")

    def _askpass_environment(self) -> Dict[str, str]:
        env = os.environ.copy()
        if not self.password:
            env["SSH_ASKPASS_REQUIRE"] = "never"
            env.pop("SSH_ASKPASS", None)
            env.pop("CALIBRE_SFTP_PASSWORD", None)
            return env
        self._ensure_runtime()
        askpass = os.path.join(self._runtime_dir or "", "askpass.sh")
        if not os.path.exists(askpass):
            with open(askpass, "w", encoding="utf-8") as stream:
                stream.write("#!/bin/sh\nprintf '%s\\n' \"$CALIBRE_SFTP_PASSWORD\"\n")
            os.chmod(askpass, 0o700)
        env["SSH_ASKPASS"] = askpass
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env["CALIBRE_SFTP_PASSWORD"] = self.password
        # OpenSSH requires DISPLAY to be set before invoking askpass on some
        # versions, even with SSH_ASKPASS_REQUIRE=force.
        env.setdefault("DISPLAY", "calibre-ssh-askpass")
        return env

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
        if self.identity_file:
            if not os.path.isfile(self.identity_file):
                raise TransportAuthenticationError(f"identity file does not exist: {self.identity_file}")
            args += ["-i", self.identity_file]
        if is_tailcat_address(self.host):
            tailcat_bin = find_host_tailcat()
            if not tailcat_bin:
                raise TransportConnectionError(
                    "Tailcat host address detected, but the 'tailcat' binary was not found on your system.\n"
                    "Please install tailcat (e.g. 'go install github.com/tailscale/tailcat/cmd/tailcat@latest') "
                    "or place it in PATH or ~/go/bin/tailcat."
                )
            args += ["-o", f"ProxyCommand='{tailcat_bin}' '{self.host}' '{self.port}' 2>/dev/null"]
        return args

    def _auth_args(self, *, password_prompt: bool) -> List[str]:
        if password_prompt:
            return [
                "-o",
                "PasswordAuthentication=yes",
                "-o",
                "KbdInteractiveAuthentication=yes",
                "-o",
                "PreferredAuthentications=publickey,password,keyboard-interactive",
            ]
        return ["-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey"]

    def _process_failure(self, result: _Completed, operation: str) -> TransportError:
        detail = self._decode(result.stderr) or self._decode(result.stdout) or "no diagnostic output"
        lower = detail.lower()
        if "documents root" in lower and ("missing" in lower or "symlink" in lower):
            return TransportSecurityError(f"{operation} rejected the configured documents root: {detail}")
        if (
            "host key" in lower
            and any(token in lower for token in ("known", "verify", "offending"))
        ) or "host key verification failed" in lower or "man-in-the-middle" in lower:
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
        if self._runtime_dir is not None:
            self.close()
        self._ensure_runtime()
        args = [self.ssh_executable]
        args += self._common_args(control=True)
        args += ["-M", "-N", "-f"]
        args += self._auth_args(password_prompt=bool(self.password))
        args += ["-o", "ControlMaster=yes", self._destination]
        result = self._run_process(args, timeout=self.timeout, env=self._askpass_environment())
        if result.returncode:
            error = self._process_failure(result, "SSH connection")
            self.close()
            raise error
        self._connected = True
        try:
            self._check_root()
            # A successful SSH shell connection is not sufficient: ensure the
            # server exposes the SFTP subsystem before the driver proceeds.
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
        return self._run_process(args, timeout=self.timeout, env=self._askpass_environment())

    def is_alive(self) -> bool:
        if not self._connected or self._control_path is None:
            return False
        result = self._control_operation("check")
        return result.returncode == 0

    def close(self) -> None:
        self._connected = False
        try:
            self._control_operation("exit")
        except Exception:
            pass
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
        args += self._auth_args(password_prompt=False)
        args += [self._destination, command]
        result = self._run_process(args, timeout=timeout or self.timeout, env=self._askpass_environment())
        if check and result.returncode:
            raise self._process_failure(result, "Remote command")
        return result

    def _sftp_batch(self, commands: List[str], *, timeout: Optional[int] = None) -> _Completed:
        self._require_connection()
        args = [self.sftp_executable, "-F", "/dev/null", "-q", "-b", "-"]
        args += ["-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={self.known_hosts}"]
        args += ["-o", "GlobalKnownHostsFile=/dev/null", "-o", f"ConnectTimeout={self.timeout}"]
        args += ["-o", f"ControlPath={self._control_path}", "-o", "ControlMaster=no", "-o", "BatchMode=yes"]
        args += ["-P", str(self.port)]
        if self.identity_file:
            args += ["-i", self.identity_file]
        if is_tailcat_address(self.host):
            tailcat_bin = find_host_tailcat()
            if tailcat_bin:
                args += ["-o", f"ProxyCommand='{tailcat_bin}' '{self.host}' '{self.port}' 2>/dev/null"]
        args += [self._sftp_destination]
        batch = ("\n".join(commands) + "\n").encode("utf-8")
        result = self._run_process(args, input_data=batch, timeout=timeout or self.transfer_timeout, env=self._askpass_environment())
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

    def refresh_library(self, paths: Optional[Sequence[str]] = None) -> None:
        """Request native Kindle indexing on the remote host, not on the PC.

        If specific book paths are provided, attempts targeted file re-indexing via
        `lipc-set-prop -i com.lab126.scanner reScanFile <path>` for each path.
        If targeted indexing is not successful or no paths are provided, falls back
        to a full library scan via `lipc-set-prop -i com.lab126.scanner doFullScan 1`.
        """
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
                return

        self._remote_command("lipc-set-prop -i com.lab126.scanner doFullScan 1")

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


def find_host_tailcat() -> Optional[str]:
    """Find the tailcat client executable on the host system."""
    for name in ("tailcat", "tailcat.exe"):
        which = shutil.which(name)
        if which and os.path.isfile(which) and os.access(which, os.X_OK):
            return which
    candidates = [
        os.path.expanduser("~/go/bin/tailcat"),
        os.path.expanduser("~/go/bin/tailcat.exe"),
        os.path.expanduser("~/.local/bin/tailcat"),
        "/opt/homebrew/bin/tailcat",
        "/usr/local/bin/tailcat",
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def probe_host(host: str, port: int, timeout: float = 1.0) -> bool:
    """Probe if the remote host/port is reachable."""
    if not host:
        return False
    if is_tailcat_address(host):
        tailcat_bin = find_host_tailcat()
        if not tailcat_bin:
            return False
        timeout_sec = max(1, int(timeout))
        try:
            res = subprocess.run(
                [tailcat_bin, "ping", f"--timeout={timeout_sec}s", host],
                capture_output=True,
                timeout=timeout_sec + 2,
                check=False,
            )
            return res.returncode == 0
        except (subprocess.SubprocessError, OSError):
            return False
    else:
        norm_host = host[1:-1] if host.startswith("[") and host.endswith("]") else host
        try:
            with socket.create_connection((norm_host, int(port)), timeout=max(0.5, timeout)):
                return True
        except OSError:
            return False


DEFAULT_TAILCAT_VERSION = "v0.7.0"
TAILCAT_RELEASE_BASE_URL = "https://github.com/tailscale/tailcat/releases/download"


def get_kindle_architecture(options: Dict[str, Any]) -> str:
    """Query Kindle CPU architecture over SSH (returns 'armv7' or 'arm64')."""
    transport = SSHTransport(options)
    transport.connect()
    try:
        res = transport._remote_command("uname -m 2>/dev/null || echo armv7l", check=False)
        raw_arch = transport._decode(res.stdout).strip().lower()
        if "aarch64" in raw_arch or "arm64" in raw_arch:
            return "arm64"
        return "armv7"
    finally:
        transport.close()


def get_tailcat_cache_dir() -> str:
    """Return local cache directory for downloaded Tailcat release binaries."""
    cache_base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    path = os.path.join(cache_base, "calibre-kindle-sftp", "bin")
    os.makedirs(path, exist_ok=True)
    return path


def download_tailcat_binary(
    arch: str = "armv7",
    version: str = DEFAULT_TAILCAT_VERSION,
    logger: Optional[Callable[[str], Any]] = None,
) -> Tuple[str, Optional[str]]:
    """Download official Tailcat release binary from GitHub if not already cached.

    Returns a tuple of (binary_path, license_path).
    """
    import io
    import tarfile
    import urllib.request

    def log(msg: str):
        if logger:
            logger(msg)

    cache_dir = get_tailcat_cache_dir()
    bin_filename = f"tailcat_{version}_{arch}"
    lic_filename = f"LICENSE_tailcat_{version}"
    bin_path = os.path.join(cache_dir, bin_filename)
    lic_path = os.path.join(cache_dir, lic_filename)

    if os.path.isfile(bin_path) and os.access(bin_path, os.X_OK):
        log(f"Using cached official Tailcat binary: {bin_path}")
        return bin_path, lic_path if os.path.isfile(lic_path) else None

    ver_num = version.lstrip("v")
    archive_name = f"tailcat_{ver_num}_linux_{arch}.tar.gz"
    url = f"{TAILCAT_RELEASE_BASE_URL}/{version}/{archive_name}"

    log(f"Downloading official Tailcat release ({version}, {arch}) from GitHub...")
    log(f"URL: {url}")

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Calibre-Kindle-SFTP-Plugin/1.0"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            total_size_hdr = resp.headers.get("Content-Length")
            total_size = int(total_size_hdr) if total_size_hdr and total_size_hdr.isdigit() else 0
            chunks = []
            downloaded = 0
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                downloaded += len(chunk)
                if total_size > 0 and len(chunks) % 20 == 0:
                    pct = int(downloaded * 100 / total_size)
                    log(f"Downloading Tailcat... {downloaded / 1024 / 1024:.1f} MB / {total_size / 1024 / 1024:.1f} MB ({pct}%)")

        archive_data = b"".join(chunks)
        log(f"Download complete ({len(archive_data) / 1024 / 1024:.1f} MB). Extracting binary...")
    except Exception as exc:
        raise RuntimeError(f"Failed to download Tailcat from {url}: {exc}") from exc

    try:
        with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz") as tar:
            bin_member = None
            lic_member = None
            for member in tar.getmembers():
                name = os.path.basename(member.name)
                if name == "tailcat":
                    bin_member = member
                elif name.startswith("LICENSE"):
                    lic_member = member

            if not bin_member:
                raise RuntimeError(f"The downloaded archive {archive_name} does not contain an executable named 'tailcat'.")

            extracted_bin = tar.extractfile(bin_member)
            if not extracted_bin:
                raise RuntimeError("Failed to extract 'tailcat' binary from archive.")

            temp_bin = bin_path + ".tmp"
            with open(temp_bin, "wb") as f:
                f.write(extracted_bin.read())
            os.chmod(temp_bin, 0o755)
            os.replace(temp_bin, bin_path)

            if lic_member:
                extracted_lic = tar.extractfile(lic_member)
                if extracted_lic:
                    temp_lic = lic_path + ".tmp"
                    with open(temp_lic, "wb") as f:
                        f.write(extracted_lic.read())
                    os.replace(temp_lic, lic_path)
    except Exception as exc:
        if os.path.exists(bin_path + ".tmp"):
            os.unlink(bin_path + ".tmp")
        raise RuntimeError(f"Failed to unpack Tailcat archive: {exc}") from exc

    log(f"Extracted Tailcat executable to {bin_path}")
    return bin_path, lic_path if os.path.isfile(lic_path) else None


def get_bundled_tailcat_arm_path() -> Optional[str]:
    """Locate bundled or cached ARM tailcat binary if present."""
    local = os.path.join(os.path.dirname(__file__), "bin", "tailcat-arm")
    if os.path.isfile(local) and os.access(local, os.R_OK):
        return os.path.abspath(local)
    cache_bin = os.path.join(get_tailcat_cache_dir(), f"tailcat_{DEFAULT_TAILCAT_VERSION}_armv7")
    if os.path.isfile(cache_bin) and os.access(cache_bin, os.R_OK):
        return cache_bin
    return None


def enroll_host_key_for_tailcat(
    original_host: str,
    original_port: int,
    tc_addr: str,
    known_hosts_path: str,
) -> bool:
    """Copy or record the trusted SSH host key from original_host to tc_addr in known_hosts."""
    expanded_path = os.path.abspath(os.path.expanduser(known_hosts_path))
    if not os.path.isfile(expanded_path):
        return False
    try:
        with open(expanded_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return False

    norm_orig = original_host[1:-1] if original_host.startswith("[") and original_host.endswith("]") else original_host
    orig_prefix = f"[{norm_orig}]:{original_port}" if original_port != 22 else norm_orig

    keys_to_add = []
    tc_lower = tc_addr.lower()
    tc_prefix = f"{tc_lower},{tc_addr}"

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) >= 3:
            hosts = parts[0].split(",")
            if tc_addr in hosts or tc_lower in hosts:
                return True  # Already enrolled
            if orig_prefix in hosts or norm_orig in hosts:
                keys_to_add.append((parts[1], parts[2]))

    if not keys_to_add:
        return False

    new_entries = []
    for key_type, key_b64 in keys_to_add:
        new_entries.append(f"{tc_prefix} {key_type} {key_b64}\n")

    try:
        with open(expanded_path, "a", encoding="utf-8") as f:
            f.writelines(new_entries)
        return True
    except OSError:
        return False


def deploy_tailcat(
    options: Dict[str, Any],
    binary_path: Optional[str] = None,
    license_path: Optional[str] = None,
    arch: str = "auto",
    force_new_key: bool = False,
    logger: Optional[Callable[[str], Any]] = None,
) -> str:
    """Deploy Tailcat to a jailbroken Kindle via SFTP/SSH into /mnt/us/tailcat.

    If binary_path is None, automatically resolves the Kindle architecture and downloads
    the official Tailcat release from GitHub on demand.
    Uploads the tailcat binary, license, and scriptlets, generates or reads the
    permanent Tailcat address, and ensures permissions are set.
    Returns the permanent tailcat address (e.g. 'tcpGFw...').
    """
    def log(msg: str):
        if logger:
            logger(msg)

    if not binary_path:
        target_arch = arch
        if not target_arch or target_arch == "auto":
            log("Resolving Kindle architecture...")
            target_arch = get_kindle_architecture(options)
            log(f"Kindle architecture resolved: {target_arch}")
        binary_path, dl_lic = download_tailcat_binary(arch=target_arch, logger=logger)
        if not license_path:
            license_path = dl_lic

    if not os.path.isfile(binary_path):
        raise ValueError(f"Tailcat binary not found at: {binary_path}")

    log(f"Connecting to Kindle at {options.get('host')}:{options.get('port', 22)}...")
    transport = SSHTransport(options)
    transport.connect()
    try:
        remote_tailcat_dir = posixpath.join(transport.user_store, "tailcat")
        remote_bin = posixpath.join(remote_tailcat_dir, "bin", "tailcat")
        remote_key = posixpath.join(remote_tailcat_dir, "kindle.private.json")
        remote_addr_file = posixpath.join(remote_tailcat_dir, "tailcat.addr")
        start_script = posixpath.join(remote_tailcat_dir, "start-tailcat.sh")
        stop_script = posixpath.join(remote_tailcat_dir, "stop-tailcat.sh")

        local_size = os.path.getsize(binary_path)
        stat_res = transport._remote_command(f"ls -l {SSHTransport._shell_quote(remote_bin)} 2>/dev/null || true", check=False)
        out = transport._decode(stat_res.stdout)
        if str(local_size) in out:
            log(f"Tailcat binary already present on Kindle ({local_size / 1024 / 1024:.1f} MB). Skipping re-upload.")
        else:
            log(f"Uploading Tailcat binary ({local_size / 1024 / 1024:.1f} MB) to /mnt/us/tailcat/bin/tailcat...")
            transport.upload_user_store_file(binary_path, "tailcat/bin/tailcat", mode=0o755)

        if license_path and os.path.isfile(license_path):
            log("Uploading Tailcat license (BSD 3-Clause) to /mnt/us/tailcat/LICENSE...")
            transport.upload_user_store_file(license_path, "tailcat/LICENSE", mode=0o644)

        # Check if key already exists on Kindle
        key_exists = False
        res = transport._remote_command(f"[ -f {SSHTransport._shell_quote(remote_key)} ] && echo EXISTS || echo NO", check=False)
        if "EXISTS" in transport._decode(res.stdout):
            key_exists = True

        tc_addr = ""
        if not key_exists or force_new_key:
            log("Generating permanent server key with --fixed-region...")
            gen_res = transport._remote_command(
                f"{SSHTransport._shell_quote(remote_bin)} genkey --fixed-region --force "
                f"--key={SSHTransport._shell_quote(remote_key)}"
            )
            out = transport._decode(gen_res.stdout)
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("tc") and len(line) >= 20:
                    tc_addr = line
                    break
            if not tc_addr:
                err = transport._decode(gen_res.stderr)
                raise TransportCommandError(f"Failed to generate permanent Tailcat key. Output: {out} {err}")
        else:
            log("Reading existing Tailcat address...")
            addr_res = transport._remote_command(f"[ -f {SSHTransport._shell_quote(remote_addr_file)} ] && cat {SSHTransport._shell_quote(remote_addr_file)} || true", check=False)
            addr_text = transport._decode(addr_res.stdout).strip()
            if addr_text.startswith("tc") and len(addr_text) >= 20:
                tc_addr = addr_text

            if not tc_addr:
                log("Determining permanent address from existing key...")
                transport._remote_command(
                    f"TAILCAT_ADDR_FILE={SSHTransport._shell_quote(remote_addr_file)} "
                    f"{SSHTransport._shell_quote(remote_bin)} serve --key={SSHTransport._shell_quote(remote_key)} --json 22 & "
                    f"PID=$!; sleep 1; kill $PID 2>/dev/null || true",
                    check=False
                )
                addr_res = transport._remote_command(f"cat {SSHTransport._shell_quote(remote_addr_file)} 2>/dev/null || true", check=False)
                addr_text = transport._decode(addr_res.stdout).strip()
                if addr_text.startswith("tc") and len(addr_text) >= 20:
                    tc_addr = addr_text

        if not tc_addr:
            raise TransportCommandError("Could not retrieve Tailcat permanent address from Kindle.")

        log(f"Permanent Tailcat address: {tc_addr}")
        transport.upload_user_store_file(io.BytesIO(tc_addr.encode("utf-8") + b"\n"), "tailcat/tailcat.addr", mode=0o644)

        log("Uploading start-tailcat.sh and stop-tailcat.sh scriptlets...")
        start_sh = f"""#!/bin/sh
# Name: Calibre - Start Tailcat
# Author: @kvn1351
# Start Tailcat server on Kindle (port 22 -> Dropbear)
DIR="{remote_tailcat_dir}"
BIN="$DIR/bin/tailcat"
KEY="$DIR/kindle.private.json"
PIDFILE="$DIR/tailcat.pid"
LOGFILE="$DIR/tailcat.log"
ADDRFILE="$DIR/tailcat.addr"

if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        echo "Tailcat is already running (PID $PID)."
        exit 0
    fi
    rm -f "$PIDFILE"
fi

if [ ! -x "$BIN" ]; then
    chmod +x "$BIN" 2>/dev/null
fi

if [ ! -f "$KEY" ]; then
    echo "Tailcat key not found at $KEY. Run setup wizard first."
    exit 1
fi

export TAILCAT_ADDR_FILE="$ADDRFILE"
nohup "$BIN" serve --key="$KEY" 22 > "$LOGFILE" 2>&1 &
PID=$!
echo "$PID" > "$PIDFILE"
echo "Tailcat started (PID $PID)."
"""
        stop_sh = f"""#!/bin/sh
# Name: Calibre - Stop Tailcat
# Author: @kvn1351
# Stop Tailcat server on Kindle
DIR="{remote_tailcat_dir}"
PIDFILE="$DIR/tailcat.pid"

if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if [ -n "$PID" ]; then
        kill "$PID" 2>/dev/null
        sleep 1
        kill -9 "$PID" 2>/dev/null
    fi
    rm -f "$PIDFILE"
    echo "Tailcat stopped."
else
    killall tailcat 2>/dev/null
    echo "Tailcat stopped (killall)."
fi
"""
        transport.upload_user_store_file(io.BytesIO(start_sh.encode("utf-8")), "tailcat/start-tailcat.sh", mode=0o755)
        transport.upload_user_store_file(io.BytesIO(stop_sh.encode("utf-8")), "tailcat/stop-tailcat.sh", mode=0o755)

        # 1. Documents library shortcuts (tap directly on Kindle Home Screen / Library)
        doc_start = '#!/bin/sh\n# Name: Calibre - Start Tailcat\n# Author: @kvn1351\n/mnt/us/tailcat/start-tailcat.sh\n'
        doc_stop = '#!/bin/sh\n# Name: Calibre - Stop Tailcat\n# Author: @kvn1351\n/mnt/us/tailcat/stop-tailcat.sh\n'
        transport.upload_user_store_file(io.BytesIO(doc_start.encode("utf-8")), "documents/Calibre Start Tailcat.sh", mode=0o755)
        transport.upload_user_store_file(io.BytesIO(doc_stop.encode("utf-8")), "documents/Calibre Stop Tailcat.sh", mode=0o755)
        # Clean up legacy un-prefixed launchers if present
        transport._remote_command("rm -f /mnt/us/documents/'Start Tailcat.sh' /mnt/us/documents/'Stop Tailcat.sh' 2>/dev/null || true", check=False)
        transport._remote_command('lipc-set-prop -i com.lab126.scanner reScanFile "/mnt/us/documents/Calibre Start Tailcat.sh" 2>/dev/null || true', check=False)
        transport._remote_command('lipc-set-prop -i com.lab126.scanner reScanFile "/mnt/us/documents/Calibre Stop Tailcat.sh" 2>/dev/null || true', check=False)
        log("Installed Library shortcuts in /mnt/us/documents/ (Calibre Start/Stop Tailcat).")

        # 2. KUAL menu entry (if /mnt/us/extensions exists)
        ext_check = transport._remote_command("[ -d /mnt/us/extensions ] && echo YES || echo NO", check=False)
        if "YES" in transport._decode(ext_check.stdout):
            kual_config = """<?xml version="1.0" encoding="UTF-8"?>
<extension>
\t<information>
\t\t<name>Calibre Tailcat</name>
\t\t<version>1.0</version>
\t\t<author>@kvn1351</author>
\t\t<id>tailcat</id>
\t</information>
\t<menus>
\t\t<menu type="json" dynamic="true">menu.json</menu>
\t</menus>
</extension>
"""
            kual_menu = """{
\t"items": [
\t\t{
\t\t\t"name": "Calibre Tailcat",
\t\t\t"priority": 1,
\t\t\t"items": [
\t\t\t\t{
\t\t\t\t\t"name": "Start Calibre Tailcat",
\t\t\t\t\t"priority": 1,
\t\t\t\t\t"action": "/mnt/us/tailcat/start-tailcat.sh"
\t\t\t\t},
\t\t\t\t{
\t\t\t\t\t"name": "Stop Calibre Tailcat",
\t\t\t\t\t"priority": 2,
\t\t\t\t\t"action": "/mnt/us/tailcat/stop-tailcat.sh"
\t\t\t\t}
\t\t\t]
\t\t}
\t]
}
"""
            transport.upload_user_store_file(io.BytesIO(kual_config.encode("utf-8")), "extensions/tailcat/config.xml", mode=0o644)
            transport.upload_user_store_file(io.BytesIO(kual_menu.encode("utf-8")), "extensions/tailcat/menu.json", mode=0o644)
            log("Installed KUAL extension in /mnt/us/extensions/tailcat/.")

        known_hosts = options.get("known_hosts") or "~/.ssh/known_hosts"
        enroll_host_key_for_tailcat(options.get("host", ""), int(options.get("port", 22)), tc_addr, known_hosts)
        log("Saved host key mapping in known_hosts.")

        return tc_addr
    finally:
        transport.close()
