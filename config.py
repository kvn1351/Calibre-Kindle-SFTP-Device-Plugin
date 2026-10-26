"""Preferences shared by Calibre's device-thread and configuration instances."""

import os
import re
import threading
from types import SimpleNamespace
import uuid
from typing import Any, Dict, List, Optional

from calibre.utils.config import JSONConfig

from calibre_plugins.kindle_sftp import broker as _broker

FORMATS = ('azw3', 'mobi', 'pdf', 'txt', 'azw', 'prc', 'azw4', 'pobi', 'azw1', 'tpz')


def is_tailcat_address(host: str) -> bool:
    """Return True if host is formatted as a Tailcat/Tailscale server address."""
    if not host or not isinstance(host, str):
        return False
    host = host.strip()
    return host.startswith("tc") and "." not in host and len(host) >= 20


def format_device_label(dev: Optional[Dict[str, Any]]) -> str:
    """Format a clean display label for a Kindle device without raw hashes."""
    if not dev:
        return "Kindle"
    alias = (dev.get("alias") or "Kindle").strip()
    host = (dev.get("host") or "").strip()
    port = dev.get("port", 22)
    if is_tailcat_address(host) or dev.get("endpoint") == "tailcat":
        alias_clean = re.sub(r"\s*\((?:Tailcat|Tailscale)\)\s*$", "", alias, flags=re.IGNORECASE).strip()
        return f"{alias_clean} (Tailscale)" if alias_clean else "Kindle (Tailscale)"
    if not host:
        return f"{alias} (no host)"
    return f"{alias} ({host}:{port})" if port != 22 else f"{alias} ({host})"


DEFAULTS = {
    # These are Calibre/UI preferences. Endpoint state belongs to kindle-link.
    'enabled': True,
    'auto_connect': True,
    'read_metadata': True,
    'format_map': list(FORMATS),
    'save_template': '{author_sort}/{title} - {authors}_{id}',
    'generation': '',
    'registry_migrated': False,
}

prefs = JSONConfig('plugins/kindle_sftp')
prefs.defaults.update(DEFAULTS)
_lock = threading.RLock()
_session_passwords: Dict[str, str] = {}
_registry_cache: Optional[Dict[str, Any]] = None
_registry_stamp: Any = None


def _legacy_value(name: str, default: Any) -> Any:
    """Read a pre-registry value without making it part of the new schema."""
    return prefs.get(name, default)


def _legacy_profiles() -> List[Dict[str, Any]]:
    raw = prefs.get('devices')
    profiles = [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    if profiles:
        return profiles
    host = str(_legacy_value('host', '') or '').strip()
    if not host:
        return []
    return [{
        'id': 'default',
        'alias': 'Kindle',
        'host': host,
        'port': _legacy_value('port', 22),
        'username': _legacy_value('username', 'root'),
        'password': _legacy_value('password', ''),
        'remember_password': _legacy_value('remember_password', False),
        'identity_file': _legacy_value('identity_file', ''),
        'documents': _legacy_value('documents', '/mnt/us/documents'),
        'user_store': _legacy_value('user_store', ''),
    }]


def _base_alias(alias: str) -> str:
    return re.sub(r"\s*\((?:Tailcat|Tailscale)\)\s*$", "", alias or "", flags=re.IGNORECASE).strip() or "Kindle"


def _legacy_rows(profiles: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], str, List[tuple]]:
    """Prepare old rows for Rust's one-time migration logic.

    Pairing and stable device identity are deliberately owned by Rust. Python
    only removes the old password field and fills endpoint defaults that used
    to be global preferences.
    """
    old_active = str(_legacy_value('active_device_id', '') or '')
    known_hosts = _legacy_value('known_hosts', '~/.ssh/known_hosts')
    timeout = _legacy_value('timeout', 8)
    rows = []
    passwords = []
    for profile in profiles:
        row = dict(profile)
        password = row.pop('password', '')
        row.pop('remember_password', None)
        if 'endpoint' not in row:
            row['endpoint'] = 'tailcat' if is_tailcat_address(row.get('host')) else 'lan'
        row['known_hosts'] = profile.get('known_hosts') or known_hosts or '~/.ssh/known_hosts'
        row['timeout'] = int(profile.get('timeout', timeout) or 8)
        row['documents'] = profile.get('documents') or '/mnt/us/documents'
        row['user_store'] = profile.get('user_store') or ''
        rows.append(row)
        if password:
            try:
                port = int(profile.get('port', 22) or 22)
            except (TypeError, ValueError):
                port = 22
            passwords.append((str(profile.get('host') or '').strip(), port,
                              str(profile.get('username') or 'root'), str(password),
                              str(profile.get('alias') or '')))
    return rows, old_active, passwords


def _discard_legacy_values() -> None:
    for key in (
        'devices', 'active_device_id', 'host', 'port', 'username', 'password',
        'remember_password', 'identity_file', 'documents', 'user_store',
        'known_hosts', 'timeout',
    ):
        try:
            del prefs[key]
        except (KeyError, TypeError):
            pass


def _cache_result(result: Dict[str, Any]) -> Dict[str, Any]:
    global _registry_cache, _registry_stamp
    _registry_cache = {
        'revision': int(result.get('revision', 0)),
        'active_id': str(result.get('active_id') or ''),
        'devices': [dict(row) for row in result.get('devices', []) if isinstance(row, dict)],
    }
    _registry_stamp = None
    return _registry_cache


def _frontend() -> Dict[str, Any]:
    global _registry_cache, _registry_stamp
    stamp = _broker.registry_stamp()
    if _registry_cache is None or stamp != _registry_stamp:
        result = _broker.frontend()
        if not isinstance(result, dict):
            raise RuntimeError('kindle-link returned an invalid registry response.')
        _registry_cache = {
            'revision': int(result.get('revision', 0)),
            'active_id': str(result.get('active_id') or ''),
            'devices': [dict(row) for row in result.get('devices', []) if isinstance(row, dict)],
        }
        _registry_stamp = stamp
    return _registry_cache


def _ensure_registry() -> Dict[str, Any]:
    with _lock:
        result = _frontend()
        if prefs.get('registry_migrated', False):
            return result
        profiles = _legacy_profiles()
        if profiles:
            legacy_rows, old_active, passwords = _legacy_rows(profiles)
            # A registry may have been populated by the CLI before Calibre's
            # first launch. Preserve those rows and its active selection.
            existing = [dict(row) for row in result.get('devices', []) if isinstance(row, dict)]
            existing_ids = {str(row.get('id') or '') for row in existing if row.get('id')}
            rows = existing + [row for row in legacy_rows if str(row.get('id') or '') not in existing_ids]
            active = result.get('active_id') or old_active or ''
            # Do not remove old values until the registry acknowledges the
            # complete merged migration. A failed/conflicting save remains
            # recoverable from the original Calibre preferences.
            result = _broker.sync_devices(rows, active, revision=int(result.get('revision', 0)))
            if not isinstance(result, dict):
                raise RuntimeError('kindle-link returned an invalid migration response.')
            returned_rows = [row for row in result.get('devices', []) if isinstance(row, dict)]
            for host, port, username, password, alias in passwords:
                base_alias = _base_alias(alias)
                matches = [
                    row for row in returned_rows
                    if str(row.get('host') or '').strip() == host
                    and int(row.get('port', 22) or 22) == port
                    and str(row.get('username') or 'root') == username
                    and _base_alias(str(row.get('name') or row.get('alias') or '')) == base_alias
                ]
                if not matches:
                    matches = [
                        row for row in returned_rows
                        if str(row.get('host') or '').strip() == host
                        and int(row.get('port', 22) or 22) == port
                        and str(row.get('username') or 'root') == username
                    ]
                if len(matches) == 1 and matches[0].get('id'):
                    m_id = str(matches[0]['id'])
                    _session_passwords[m_id] = password
                    base_id = str(matches[0].get('device_id') or m_id.split(':')[0])
                    if base_id:
                        _session_passwords[base_id] = password
            _cache_result(result)
            with prefs:
                prefs['registry_migrated'] = True
                prefs['generation'] = str(result.get('revision', ''))
                _discard_legacy_values()
            return _registry_cache
        with prefs:
            prefs['registry_migrated'] = True
            _discard_legacy_values()
        return result


def _public_devices(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    devices = []
    for row in result.get('devices', []):
        item = dict(row)
        endpoint_id = str(item.get('id') or '')
        base_id = str(item.get('device_id') or endpoint_id.split(':')[0])
        item['password'] = _session_passwords.get(endpoint_id) or _session_passwords.get(base_id, '')
        item['remember_password'] = bool(item['password'])
        item.setdefault('documents', '/mnt/us/documents')
        item.setdefault('user_store', '')
        if item.get('endpoint') == 'tailcat' or is_tailcat_address(item.get('host', '')):
            alias = item.get('alias') or 'Kindle'
            item['alias'] = f"{_base_alias(str(alias))} (Tailscale)"
        devices.append(item)
    return devices


def get_devices() -> List[Dict[str, Any]]:
    """Return endpoint rows from the Rust registry."""
    with _lock:
        return _public_devices(_ensure_registry())


def get_active_device_id() -> str:
    with _lock:
        result = _ensure_registry()
        active_id = str(result.get('active_id') or '')
        if active_id and any(row.get('id') == active_id for row in result.get('devices', [])):
            return active_id
        rows = result.get('devices', [])
        return str(rows[0].get('id') or '') if rows else ''


def get_active_device() -> Dict[str, Any]:
    with _lock:
        devices = get_devices()
        active_id = get_active_device_id()
        for device in devices:
            if device.get('id') == active_id:
                return dict(device)
        return dict(devices[0]) if devices else {
            'id': '', 'device_id': '', 'endpoint': 'lan', 'alias': 'Kindle',
            'host': '', 'port': 22, 'username': 'root', 'identity_file': '',
            'known_hosts': '~/.ssh/known_hosts', 'timeout': 8,
            'documents': '/mnt/us/documents', 'user_store': '', 'password': '',
            'remember_password': False,
        }


def registry_revision() -> int:
    with _lock:
        return int(_ensure_registry().get('revision', 0))


def set_active_device_id(device_id: str) -> Dict[str, Any]:
    with _lock:
        result = _broker.select_device(str(device_id))
        if not isinstance(result, dict):
            raise RuntimeError('kindle-link returned an invalid selection response.')
        return _cache_result(result)


def _registry_row(device: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(device)
    row.pop('password', None)
    row.pop('remember_password', None)
    row.pop('_legacy_password', None)
    if is_tailcat_address(row.get('host', '')):
        row['endpoint'] = 'tailcat'
    else:
        row.setdefault('endpoint', 'lan')
    row.setdefault('device_id', str(row.get('id', '')).rsplit(':', 1)[0])
    row.setdefault('id', f"{row['device_id']}:{row['endpoint']}")
    row.setdefault('alias', 'Kindle')
    row.setdefault('port', 22)
    row.setdefault('username', 'root')
    row.setdefault('identity_file', '')
    row.setdefault('known_hosts', '~/.ssh/known_hosts')
    row.setdefault('timeout', 8)
    row.setdefault('documents', '/mnt/us/documents')
    row.setdefault('user_store', '')
    return row


def save_devices(devices: List[Dict[str, Any]], active_id: Optional[str] = None,
                 revision: Optional[int] = None) -> Dict[str, Any]:
    """Atomically replace registry endpoint rows, optionally using CAS revision."""
    with _lock:
        public = [dict(device) for device in devices]
        for device in public:
            validate_device(device)
        rows = [_registry_row(device) for device in public]
        if active_id is None:
            active_id = get_active_device_id()
        result = _broker.sync_devices(rows, active_id or '', revision=revision)
        if not isinstance(result, dict):
            raise RuntimeError('kindle-link returned an invalid save response.')
        # Password changes become session state only after a successful save.
        new_ids = set()
        for device in public:
            endpoint_id = str(device.get('id') or '')
            base_id = str(device.get('device_id') or endpoint_id.split(':')[0])
            new_ids.add(endpoint_id)
            if base_id:
                new_ids.add(base_id)
            if device.get('password'):
                _session_passwords[endpoint_id] = str(device['password'])
                if base_id:
                    _session_passwords[base_id] = str(device['password'])
            elif 'password' in device and device.get('password') == '':
                _session_passwords.pop(endpoint_id, None)
                if base_id:
                    _session_passwords.pop(base_id, None)
        for endpoint_id in list(_session_passwords):
            if endpoint_id not in new_ids:
                _session_passwords.pop(endpoint_id, None)
        _cache_result(result)
        return result


def set_device_enabled(enabled: bool) -> None:
    with _lock:
        with prefs:
            prefs['enabled'] = bool(enabled)
            prefs['generation'] = str(uuid.uuid4().hex)


def connection_options() -> Dict[str, Any]:
    """Construct options from the active Rust endpoint and UI preferences."""
    with _lock:
        dev = get_active_device()
        dev_id = dev.get('id', '')
        base_id = dev.get('device_id', '') or dev_id.split(':')[0]
        password = _session_passwords.get(dev_id) or _session_passwords.get(base_id, '')
        result = {
            'enabled': prefs.get('enabled', True),
            'auto_connect': prefs.get('auto_connect', prefs.get('enabled', True)),
            'host': dev.get('host', ''),
            'port': dev.get('port', 22),
            'username': dev.get('username', 'root'),
            'documents': dev.get('documents', '/mnt/us/documents'),
            'user_store': dev.get('user_store', ''),
            'identity_file': os.path.expanduser(dev.get('identity_file') or ''),
            'known_hosts': os.path.expanduser(dev.get('known_hosts') or '~/.ssh/known_hosts'),
            'password': password,
            'remember_password': bool(password),
            'timeout': int(dev.get('timeout', 8) or 8),
            'read_metadata': prefs.get('read_metadata', True),
            'format_map': list(prefs.get('format_map', FORMATS)),
            'save_template': prefs.get('save_template', '{author_sort}/{title} - {authors}_{id}'),
            'generation': prefs.get('generation', ''),
            'device_id': dev.get('device_id') or dev_id,
            'endpoint_id': dev_id,
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
    """Validate a single endpoint configuration."""
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
    """Store UI preferences and update the active endpoint in the Rust registry."""
    if dev is None:
        dev = get_active_device()
    validate_options(options, dev=dev)
    with _lock:
        if dev.get('id'):
            updated = dict(dev)
            updated['known_hosts'] = options.get('known_hosts', '~/.ssh/known_hosts')
            updated['timeout'] = int(options.get('timeout', 8))
            save_devices([updated if row.get('id') == dev.get('id') else row
                          for row in get_devices()], get_active_device_id(),
                         revision=registry_revision())
        with prefs:
            prefs['enabled'] = options.get('enabled', True)
            prefs['auto_connect'] = options.get('auto_connect', True)
            prefs['read_metadata'] = options.get('read_metadata', True)
            prefs['format_map'] = options.get('format_map', list(FORMATS))
            prefs['save_template'] = options.get('save_template', '{author_sort}/{title} - {authors}_{id}')
            prefs['generation'] = str(registry_revision())

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
