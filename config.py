"""Preferences shared by Calibre's device-thread and configuration instances."""

import base64
import hashlib
import os
import posixpath
import re
import shutil
import subprocess
import threading
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from calibre.utils.config import JSONConfig

FORMATS = ('azw3', 'mobi', 'pdf', 'txt', 'azw', 'prc', 'azw4', 'pobi', 'azw1', 'tpz')


def is_tailcat_address(host: str) -> bool:
    """Return True if host is formatted as a Tailcat/Tailscale server address."""
    if not host or not isinstance(host, str):
        return False
    host = host.strip()
    return host.startswith("tc") and "." not in host and len(host) >= 20


def format_device_label(dev: Optional[Dict[str, Any]]) -> str:
    """Format a clean display label for a Kindle device without raw hashes.

    For regular hosts, returns '<alias> (<host>:<port>)' or '<alias> (<host>)'.
    For Tailcat/Tailscale hosts, returns '<alias> (Tailscale)' without exposing the raw hash.
    """
    if not dev:
        return "Kindle"
    alias = (dev.get("alias") or "Kindle").strip()
    host = (dev.get("host") or "").strip()
    port = dev.get("port", 22)

    if is_tailcat_address(host):
        alias_clean = re.sub(r"\s*\((?:Tailcat|Tailscale)\)\s*$", "", alias, flags=re.IGNORECASE).strip()
        return f"{alias_clean} (Tailscale)" if alias_clean else "Kindle (Tailscale)"

    if not host:
        return f"{alias} (no host)"
    return f"{alias} ({host}:{port})" if port != 22 else f"{alias} ({host})"

DEFAULT_DEVICE = {
    'id': 'default',
    'alias': 'Kindle',
    'host': '',
    'port': 22,
    'username': 'root',
    'password': '',
    'remember_password': False,
    'identity_file': '',
    'documents': '/mnt/us/documents',
    'user_store': '',
}

DEFAULTS = {
    'enabled': True,
    'auto_connect': True,
    'devices': [],
    'active_device_id': '',
    'known_hosts': '~/.ssh/known_hosts',
    'timeout': 8,
    'read_metadata': True,
    'format_map': list(FORMATS),
    'save_template': '{author_sort}/{title} - {authors}_{id}',
    'generation': '',
    # Legacy flat keys retained for direct access / backwards compatibility:
    'host': '',
    'port': 22,
    'username': 'root',
    'password': '',
    'remember_password': False,
    'identity_file': '',
    'documents': '/mnt/us/documents',
    'user_store': '',
}

prefs = JSONConfig('plugins/kindle_sftp')
prefs.defaults.update(DEFAULTS)
_lock = threading.RLock()
_session_passwords: Dict[str, str] = {}


def get_devices() -> List[Dict[str, Any]]:
    """Return the list of configured Kindle device profiles."""
    with _lock:
        raw = prefs.get('devices')
        if isinstance(raw, list) and raw:
            result = []
            for d in raw:
                d_copy = dict(d)
                if is_tailcat_address(d_copy.get('host', '')):
                    alias = d_copy.get('alias', '')
                    if '(Tailcat)' in alias:
                        d_copy['alias'] = alias.replace('(Tailcat)', '(Tailscale)')
                result.append(d_copy)
            return result
        # Legacy migration: check if legacy flat host key exists
        legacy_host = prefs.get('host', '').strip()
        if legacy_host:
            legacy = {
                'id': 'default',
                'alias': 'Kindle',
                'host': legacy_host,
                'port': prefs.get('port', 22),
                'username': prefs.get('username', 'root'),
                'documents': prefs.get('documents', '/mnt/us/documents'),
                'user_store': prefs.get('user_store', ''),
                'identity_file': prefs.get('identity_file', ''),
                'password': prefs.get('password', ''),
                'remember_password': prefs.get('remember_password', False),
            }
            return [legacy]
        return []


def get_active_device_id() -> str:
    """Return the ID of the currently active Kindle profile."""
    with _lock:
        devs = get_devices()
        if not devs:
            return ''
        active_id = prefs.get('active_device_id')
        if active_id and any(d.get('id') == active_id for d in devs):
            return active_id
        return devs[0].get('id', '')


def get_active_device() -> Dict[str, Any]:
    """Return a copy of the active device profile dictionary."""
    with _lock:
        devs = get_devices()
        active_id = get_active_device_id()
        for d in devs:
            if d.get('id') == active_id:
                return dict(d)
        if devs:
            return dict(devs[0])
        # Fallback to legacy keys or empty default
        return {
            'id': 'default',
            'alias': 'Kindle',
            'host': prefs.get('host', ''),
            'port': prefs.get('port', 22),
            'username': prefs.get('username', 'root'),
            'documents': prefs.get('documents', '/mnt/us/documents'),
            'user_store': prefs.get('user_store', ''),
            'identity_file': prefs.get('identity_file', ''),
            'password': prefs.get('password', ''),
            'remember_password': prefs.get('remember_password', False),
        }


def set_active_device_id(device_id: str) -> None:
    """Set the active Kindle device profile ID and bump generation."""
    with _lock:
        with prefs:
            prefs['active_device_id'] = device_id
            prefs['generation'] = uuid.uuid4().hex


def save_devices(devices: List[Dict[str, Any]], active_id: Optional[str] = None) -> None:
    """Save the updated list of device profiles and update active device."""
    with _lock:
        with prefs:
            prefs['devices'] = [dict(d) for d in devices]
            if active_id:
                prefs['active_device_id'] = active_id
            active = get_active_device()
            # Sync active device properties to legacy flat keys for backward compatibility
            prefs['host'] = active.get('host', '')
            prefs['port'] = active.get('port', 22)
            prefs['username'] = active.get('username', 'root')
            prefs['documents'] = active.get('documents', '/mnt/us/documents')
            prefs['user_store'] = active.get('user_store', '')
            prefs['identity_file'] = active.get('identity_file', '')
            prefs['password'] = active.get('password', '') if active.get('remember_password') else ''
            prefs['remember_password'] = active.get('remember_password', False)
            prefs['generation'] = uuid.uuid4().hex


def set_device_enabled(enabled: bool) -> None:
    """Enable or disable device connection."""
    with _lock:
        with prefs:
            prefs['enabled'] = enabled
            prefs['generation'] = uuid.uuid4().hex


def connection_options() -> Dict[str, Any]:
    """Construct options dictionary for SSHTransport and device driver."""
    with _lock:
        dev = get_active_device()
        dev_id = dev.get('id', 'default')

        # Read host and port from active device, falling back to legacy flat keys
        host = dev.get('host') or prefs.get('host', '')
        port = dev.get('port') or prefs.get('port', 22)
        username = dev.get('username') or prefs.get('username', 'root')
        documents = dev.get('documents') or prefs.get('documents', '/mnt/us/documents')
        user_store = dev.get('user_store') or prefs.get('user_store', '')
        identity_file = dev.get('identity_file') or prefs.get('identity_file', '')
        remember = dev.get('remember_password', False)
        pwd = dev.get('password', '') if remember else _session_passwords.get(dev_id, prefs.get('password', ''))

        result = {
            'enabled': prefs.get('enabled', True),
            'auto_connect': prefs.get('auto_connect', prefs.get('enabled', True)),
            'host': host,
            'port': port,
            'username': username,
            'documents': documents,
            'user_store': user_store,
            'identity_file': os.path.expanduser(identity_file),
            'known_hosts': os.path.expanduser(prefs.get('known_hosts', '~/.ssh/known_hosts')),
            'password': pwd,
            'remember_password': remember,
            'timeout': prefs.get('timeout', 8),
            'read_metadata': prefs.get('read_metadata', True),
            'format_map': list(prefs.get('format_map', FORMATS)),
            'save_template': prefs.get('save_template', '{author_sort}/{title} - {authors}_{id}'),
            'generation': prefs.get('generation', ''),
            'device_id': dev_id,
            'device_alias': dev.get('alias', 'Kindle'),
        }
        return result


def device_settings() -> SimpleNamespace:
    options = connection_options()
    return SimpleNamespace(
        format_map=list(options['format_map']),
        save_template=options['save_template'],
        use_subdirs=True,
        read_metadata=options['read_metadata'],
        use_author_sort=False,
        extra_customization=None,
    )


def validate_device(dev: Dict[str, Any]) -> None:
    """Validate a single device configuration."""
    host = dev.get('host', '').strip()
    username = dev.get('username', '').strip()
    if host.startswith('-') or any(c.isspace() or ord(c) < 32 for c in host):
        raise ValueError('Host cannot start with a dash or contain whitespace/control characters.')
    if username.startswith('-') or any(c.isspace() or ord(c) < 32 for c in username):
        raise ValueError('Username cannot start with a dash or contain whitespace/control characters.')
    if not username:
        raise ValueError('An SSH username is required.')
    try:
        port = int(dev.get('port', 22))
        if not 1 <= port <= 65535:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('SSH port must be an integer between 1 and 65535.')
    documents = dev.get('documents', '/mnt/us/documents')
    if (not documents.startswith('/') or '\\' in documents or '\x00' in documents
            or any(ord(c) < 32 for c in documents)
            or '..' in documents.split('/')):
        raise ValueError('Use an absolute POSIX documents path without parent traversal or control characters.')
    identity = os.path.expanduser(dev.get('identity_file', ''))
    if identity and not os.path.isfile(identity):
        raise ValueError('The private key file does not exist. Leave it empty to use your SSH agent/default keys.')


def validate_options(options: Dict[str, Any], dev: Optional[Dict[str, Any]] = None) -> None:
    """Validate global plugin options and active device if enabled."""
    if dev is None:
        dev = get_active_device()
    host = (options.get('host') or dev.get('host', '')).strip()
    if options.get('enabled') and not host:
        raise ValueError('Enter the Kindle hostname or IP address before enabling the connection.')
    if host:
        dev_copy = dict(dev)
        dev_copy['host'] = host
        validate_device(dev_copy)
    formats = options.get('format_map', [])
    if not formats or len(set(formats)) != len(formats) or set(formats) - set(FORMATS):
        raise ValueError('Choose distinct native Kindle formats from: ' + ', '.join(FORMATS)
                         + '. EPUB is converted by Calibre before sending, not sideloaded directly.')
    known_hosts = options.get('known_hosts', '')
    if '\x00' in known_hosts or '\n' in known_hosts or '\r' in known_hosts:
        raise ValueError('known_hosts cannot contain NUL or newline characters.')
    from calibre.utils.formatter import validation_formatter
    validation_formatter.validate(options.get('save_template', ''))


def store_options(options: Dict[str, Any], dev: Optional[Dict[str, Any]] = None) -> None:
    """Store global preferences and active device settings."""
    validate_options(options, dev=dev)
    with _lock:
        with prefs:
            prefs['enabled'] = options.get('enabled', True)
            prefs['auto_connect'] = options.get('auto_connect', True)
            prefs['timeout'] = options.get('timeout', 8)
            prefs['known_hosts'] = options.get('known_hosts', '~/.ssh/known_hosts')
            prefs['read_metadata'] = options.get('read_metadata', True)
            prefs['format_map'] = options.get('format_map', list(FORMATS))
            prefs['save_template'] = options.get('save_template', '{author_sort}/{title} - {authors}_{id}')
            prefs['generation'] = uuid.uuid4().hex


class ConfigWidget:
    """Factory kept lazy so loading a device driver never initializes Qt."""

    def __new__(cls):
        from qt.core import (
            QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout,
            QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
            QRadioButton, QSpinBox, QVBoxLayout, QWidget,
        )

        from calibre.gui2 import error_dialog, info_dialog

        class Widget(QWidget):
            validate_before_accept = True

            def __init__(self):
                super().__init__()
                options = connection_options()
                layout = QVBoxLayout(self)

                form = QFormLayout()
                form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
                layout.addLayout(form)

                # Active Kindle row with Manage Connections button
                active_row = QHBoxLayout()
                self.combo_devices = QComboBox(self)
                self._populate_devices_combo()
                active_row.addWidget(self.combo_devices, stretch=1)
                btn_manage = QPushButton('Manage Connections…', self)
                btn_manage.clicked.connect(self._open_manage_connections)
                active_row.addWidget(btn_manage)
                form.addRow('Active Kindle:', active_row)

                self.radio_auto = QRadioButton('Automatically connect as soon as the active Kindle is available on Wi-Fi')
                self.radio_manual = QRadioButton('Connect manually via toolbar button ("Link Kindle Wi-Fi")')
                self.mode_group = QButtonGroup(self)
                self.mode_group.addButton(self.radio_auto)
                self.mode_group.addButton(self.radio_manual)

                auto_val = options.get('auto_connect', True)
                self.radio_auto.setChecked(bool(auto_val))
                self.radio_manual.setChecked(not bool(auto_val))

                mode_box = QVBoxLayout()
                mode_box.addWidget(self.radio_auto)
                mode_box.addWidget(self.radio_manual)
                form.addRow('Connection mode:', mode_box)

                self.timeout = QSpinBox()
                self.timeout.setRange(2, 60)
                self.timeout.setValue(options['timeout'])
                form.addRow('Connection timeout (seconds):', self.timeout)

                self.known_hosts = QLineEdit(options['known_hosts'])
                row_kh = QHBoxLayout()
                row_kh.addWidget(self.known_hosts)
                browse_kh = QPushButton('Browse…')
                browse_kh.clicked.connect(self._browse_known_hosts)
                row_kh.addWidget(browse_kh)
                form.addRow('Known hosts file:', row_kh)

                self.formats = QLineEdit(', '.join(options['format_map']))
                form.addRow('Formats, preferred first:', self.formats)

                self.save_template = QLineEdit(options['save_template'])
                form.addRow('Calibre filename template:', self.save_template)

                self.read_metadata = QCheckBox('Read embedded metadata of uncached books (downloads them once)')
                self.read_metadata.setChecked(options['read_metadata'])
                form.addRow(self.read_metadata)

                # Test connection button for active device
                btn_row = QHBoxLayout()
                test_btn = QPushButton('Test Active Connection')
                test_btn.clicked.connect(self._test_active_connection)
                btn_row.addWidget(test_btn)
                layout.addLayout(btn_row)

                note = QLabel(
                    'Host keys are checked strictly against known_hosts. '
                    'Use "Link Kindle Wi-Fi" on the main Calibre toolbar to toggle connection at any time.'
                )
                note.setWordWrap(True)
                layout.addWidget(note)
                layout.addStretch(1)

            def _populate_devices_combo(self):
                self.combo_devices.clear()
                devs = get_devices()
                active_id = get_active_device_id()
                curr_idx = 0
                for i, dev in enumerate(devs):
                    dev_id = dev.get('id', '')
                    label = format_device_label(dev)
                    self.combo_devices.addItem(label, dev_id)
                    if dev_id == active_id:
                        curr_idx = i
                if devs:
                    self.combo_devices.setCurrentIndex(curr_idx)

            def _browse_known_hosts(self):
                filename, _ = QFileDialog.getOpenFileName(self, 'Choose Known Hosts File', os.path.expanduser('~/.ssh'))
                if filename:
                    self.known_hosts.setText(filename)

            def _open_manage_connections(self):
                from calibre_plugins.kindle_sftp.ui import ManageConnectionsDialog
                dlg = ManageConnectionsDialog(self)
                if dlg.exec():
                    self._populate_devices_combo()

            def _test_active_connection(self):
                from calibre_plugins.kindle_sftp.transport import SSHTransport
                options = connection_options()
                if not options.get('host'):
                    error_dialog(self, 'No Active Kindle Host', 'Configure a Kindle host in Manage Connections first.', show=True)
                    return
                try:
                    transport = SSHTransport(options)
                    transport.connect()
                    total, free = transport.space()
                    dev = self._selected_device()
                    disp = format_device_label(dev)
                    info_dialog(
                        self, 'Kindle SFTP Connection Successful',
                        f"Connected to {disp}!\n\n"
                        f"SSH authentication, SFTP, and documents directory verified.\n"
                        f"{free / 1024**3:.2f} GiB free of {total / 1024**3:.2f} GiB.",
                        show=True,
                    )
                except Exception as error:
                    error_dialog(self, 'Connection Failed', str(error), show=True)
                finally:
                    if 'transport' in locals() and transport is not None:
                        transport.close()

            def options(self):
                return {
                    'enabled': True,
                    'auto_connect': self.radio_auto.isChecked(),
                    'timeout': self.timeout.value(),
                    'known_hosts': self.known_hosts.text().strip() or '~/.ssh/known_hosts',
                    'read_metadata': self.read_metadata.isChecked(),
                    'format_map': [x.strip().lower() for x in self.formats.text().split(',') if x.strip()],
                    'save_template': self.save_template.text().strip(),
                }

            def _selected_device(self):
                idx = self.combo_devices.currentIndex()
                if idx >= 0:
                    selected_id = self.combo_devices.itemData(idx)
                    if selected_id:
                        for d in get_devices():
                            if d.get('id') == selected_id:
                                return d
                return get_active_device()

            def validate(self):
                try:
                    validate_options(self.options(), dev=self._selected_device())
                except Exception as error:
                    error_dialog(self, 'Invalid Settings', str(error), show=True)
                    return False
                return True

            def save_settings(self):
                dev = self._selected_device()
                if dev and dev.get('id'):
                    set_active_device_id(dev['id'])
                store_options(self.options(), dev=dev)

        return Widget()
