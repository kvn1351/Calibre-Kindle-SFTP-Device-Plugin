"""Toolbar action and connection management dialogs for Kindle SFTP."""

from functools import partial
import os
import re
import shlex
import uuid
from typing import Optional
from qt.core import (
    QAction, QActionGroup, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QIcon, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QRadioButton, QSpinBox, QTextBrowser,
    QThread, QTimer, QToolButton, QVBoxLayout, QWidget, pyqtSignal,
)

from calibre.gui2 import Dispatcher, error_dialog, info_dialog
from calibre.gui2.actions import InterfaceAction

from calibre_plugins.kindle_sftp.config import (
    format_device_label, get_active_device, get_active_device_id, get_devices,
    registry_revision, save_devices, set_active_device_id, validate_device,
)
from calibre_plugins.kindle_sftp.broker import executable, get_host_keys, setup_tailcat
from calibre_plugins.kindle_sftp.transport import SSHTransport, is_tailcat_address


class ManageConnectionsDialog(QDialog):
    """Modal dialog to manage multiple Kindle Wi-Fi (SFTP) device profiles."""

    def __init__(self, parent=None, initial_device_id=None):
        super().__init__(parent)
        self.setWindowTitle("Manage Kindle Connections")
        self.setWindowIcon(QIcon.ic("devices/kindle.png"))
        self.resize(760, 520)

        raw_devices = get_devices()
        # Deep-copy devices and retain the registry revision for optimistic CAS.
        self.devices = [dict(d) for d in raw_devices]
        self.active_id = initial_device_id or get_active_device_id()
        self.revision = registry_revision()
        self._updating_form = False

        if not self.devices:
            # A blank draft is not persisted until the user accepts the dialog.
            device_id = uuid.uuid4().hex
            first = {
                "id": f"{device_id}:lan",
                "device_id": device_id,
                "endpoint": "lan",
                "alias": "My Kindle",
                "host": "",
                "port": 22,
                "username": "root",
                "password": "",
                "remember_password": False,
                "identity_file": "",
                "known_hosts": "~/.ssh/known_hosts",
                "timeout": 8,
                "documents": "/mnt/us/documents",
                "user_store": "",
            }
            self.devices.append(first)
            self.active_id = first["id"]

        main_layout = QVBoxLayout(self)

        header = QLabel(
            "<b>Kindle Devices</b>: Configure SSH/SFTP connection details for each Kindle. "
            "Select an active device to link over Wi-Fi."
        )
        header.setWordWrap(True)
        main_layout.addWidget(header)

        body_layout = QHBoxLayout()
        main_layout.addLayout(body_layout)

        # Left pane: Device list & buttons
        left_layout = QVBoxLayout()
        body_layout.addLayout(left_layout, stretch=1)

        left_layout.addWidget(QLabel("Kindles:"))
        self.list_widget = QListWidget(self)
        self.list_widget.currentRowChanged.connect(self._on_device_selected)
        left_layout.addWidget(self.list_widget)

        btn_row = QHBoxLayout()
        self.btn_add = QPushButton("+ Add")
        self.btn_add.clicked.connect(self._add_device)
        self.btn_remove = QPushButton("- Remove")
        self.btn_remove.clicked.connect(self._remove_device)
        self.btn_set_active = QPushButton("Set Active")
        self.btn_set_active.clicked.connect(self._set_selected_active)
        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_remove)
        btn_row.addWidget(self.btn_set_active)
        left_layout.addLayout(btn_row)

        # Right pane: Device details form
        right_group = QGroupBox("Device Details", self)
        right_layout = QVBoxLayout(right_group)
        body_layout.addWidget(right_group, stretch=2)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        right_layout.addLayout(form)

        self.edit_alias = QLineEdit()
        self.edit_alias.setPlaceholderText("e.g. Paperwhite 5, Oasis")
        self.edit_alias.textEdited.connect(self._on_field_edited)
        form.addRow("Device Alias:", self.edit_alias)

        self.edit_host = QLineEdit()
        self.edit_host.setPlaceholderText("e.g. 192.168.1.50 or kindle.local")
        self.edit_host.setToolTip("Kindle local IP address (e.g. 192.168.1.50, 192.168.0.50), hostname, or Tailcat address")
        self.edit_host.textEdited.connect(self._on_field_edited)
        form.addRow("Host / IP:", self.edit_host)

        self.edit_port = QSpinBox()
        self.edit_port.setRange(1, 65535)
        self.edit_port.setValue(22)
        self.edit_port.valueChanged.connect(self._on_field_edited)
        form.addRow("Port (Dropbear/Jobman: 22; KUAL: 2222):", self.edit_port)

        self.edit_username = QLineEdit()
        self.edit_username.setPlaceholderText("root")
        self.edit_username.textEdited.connect(self._on_field_edited)
        form.addRow("SSH Username:", self.edit_username)

        self.edit_password = QLineEdit()
        self.edit_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_password.setPlaceholderText("Empty: SSH agent / key authentication")
        self.edit_password.textEdited.connect(self._on_field_edited)
        form.addRow("SSH Password:", self.edit_password)

        self.chk_remember_password = QCheckBox(
            "Keep password in memory for this Calibre session (never save to disk)"
        )
        self.chk_remember_password.toggled.connect(self._on_field_edited)
        form.addRow(self.chk_remember_password)

        self.edit_identity = QLineEdit()
        self.edit_identity.setPlaceholderText("Empty: ~/.ssh default keys / agent")
        self.edit_identity.textEdited.connect(self._on_field_edited)
        browse_row = QHBoxLayout()
        browse_row.addWidget(self.edit_identity)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_identity)
        browse_row.addWidget(browse_btn)
        form.addRow("Private Key File:", browse_row)

        # Action buttons for current device
        actions_row = QHBoxLayout()
        self.btn_trust = QPushButton("Verify / trust host key…")
        self.btn_trust.clicked.connect(self._trust_host)
        actions_row.addWidget(self.btn_trust)
        self.btn_test = QPushButton("Test connection")
        self.btn_test.clicked.connect(self._test_connection)
        actions_row.addWidget(self.btn_test)
        self.btn_tailcat = QPushButton("Setup Tailcat Remote Access…")
        self.btn_tailcat.clicked.connect(self._open_tailcat_wizard)
        actions_row.addWidget(self.btn_tailcat)
        self.btn_tailcat_help = QPushButton("?")
        self.btn_tailcat_help.setToolTip("About Tailcat remote access, files installed, and uninstallation")
        self.btn_tailcat_help.setStyleSheet("font-weight: bold; font-size: 13px; min-width: 28px; max-width: 28px;")
        self.btn_tailcat_help.clicked.connect(self._show_tailcat_info)
        actions_row.addWidget(self.btn_tailcat_help)
        right_layout.addLayout(actions_row)
        right_layout.addStretch(1)

        # Dialog buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        button_box.accepted.connect(self._save_and_accept)
        button_box.rejected.connect(self.reject)
        main_layout.addWidget(button_box)

        # Populate device list
        self._refresh_list_widget()
        # Select active device or first item
        sel_idx = 0
        for i, dev in enumerate(self.devices):
            if dev.get("id") == self.active_id:
                sel_idx = i
                break
        self.list_widget.setCurrentRow(sel_idx)

    def _refresh_list_widget(self):
        curr = self.list_widget.currentRow()
        self.list_widget.clear()
        for dev in self.devices:
            dev_id = dev.get("id", "")
            active_marker = "★ " if dev_id == self.active_id else "   "
            label = f"{active_marker}{format_device_label(dev)}"
            item = QListWidgetItem(label)
            item.setData(1000, dev_id)
            self.list_widget.addItem(item)
        if 0 <= curr < self.list_widget.count():
            self.list_widget.setCurrentRow(curr)

    def _current_device(self):
        row = self.list_widget.currentRow()
        if 0 <= row < len(self.devices):
            return self.devices[row]
        return None

    def _on_device_selected(self, row):
        if row < 0 or row >= len(self.devices):
            return
        self._updating_form = True
        try:
            dev = self.devices[row]
            self.edit_alias.setText(dev.get("alias", ""))
            self.edit_host.setText(dev.get("host", ""))
            self.edit_port.setValue(int(dev.get("port", 22)))
            self.edit_username.setText(dev.get("username", "root"))
            self.edit_password.setText(dev.get("password", ""))
            self.chk_remember_password.setChecked(bool(dev.get("remember_password", False)))
            self.edit_identity.setText(dev.get("identity_file", ""))
            host = dev.get("host", "").strip()
            is_tc = dev.get("endpoint") == "tailcat" or is_tailcat_address(host)
            self.btn_tailcat.setVisible(not is_tc)
            self.btn_tailcat_help.setVisible(not is_tc)
        finally:
            self._updating_form = False

    def _on_field_edited(self):
        if self._updating_form:
            return
        dev = self._current_device()
        if dev is None:
            return
        dev["alias"] = self.edit_alias.text().strip()
        dev["host"] = self.edit_host.text().strip()
        dev["port"] = self.edit_port.value()
        dev["username"] = self.edit_username.text().strip()
        dev["password"] = self.edit_password.text()
        dev["remember_password"] = self.chk_remember_password.isChecked()
        dev["identity_file"] = self.edit_identity.text().strip()
        dev["documents"] = dev.get("documents") or "/mnt/us/documents"
        dev.setdefault("known_hosts", "~/.ssh/known_hosts")
        dev.setdefault("timeout", 8)
        if is_tailcat_address(dev["host"]):
            dev["endpoint"] = "tailcat"
        elif not dev.get("endpoint") or dev.get("endpoint") == "tailcat":
            dev["endpoint"] = "lan"
        is_tc = dev.get("endpoint") == "tailcat" or is_tailcat_address(dev["host"])
        self.btn_tailcat.setVisible(not is_tc)
        self.btn_tailcat_help.setVisible(not is_tc)

        row = self.list_widget.currentRow()
        item = self.list_widget.item(row)
        if item is not None:
            active_marker = "★ " if dev.get("id") == self.active_id else "   "
            item.setText(f"{active_marker}{format_device_label(dev)}")

    def _add_device(self):
        device_id = uuid.uuid4().hex
        new_dev = {
            "id": f"{device_id}:lan",
            "device_id": device_id,
            "endpoint": "lan",
            "alias": f"Kindle {len(self.devices) + 1}",
            "host": "",
            "port": 22,
            "username": "root",
            "password": "",
            "remember_password": False,
            "identity_file": "",
            "known_hosts": "~/.ssh/known_hosts",
            "timeout": 8,
            "documents": "/mnt/us/documents",
            "user_store": "",
        }
        self.devices.append(new_dev)
        if len(self.devices) == 1:
            self.active_id = new_dev["id"]
        self._refresh_list_widget()
        self.list_widget.setCurrentRow(len(self.devices) - 1)
        self.edit_host.setFocus()

    def _remove_device(self):
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self.devices):
            return
        target = self.devices[row]
        alias = target.get("alias") or "Kindle"
        if len(self.devices) == 1:
            error_dialog(self, "Cannot Remove Device", "At least one Kindle connection must be maintained.", show=True)
            return
        answer = QMessageBox.question(
            self, "Remove Kindle", f"Remove '{alias}' from your configured connections?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed_id = target.get("id")
        self.devices.pop(row)
        if self.active_id == removed_id:
            self.active_id = self.devices[0].get("id", "")
        self._refresh_list_widget()
        new_row = min(row, len(self.devices) - 1)
        self.list_widget.setCurrentRow(new_row)

    def _set_selected_active(self):
        dev = self._current_device()
        if dev:
            self.active_id = dev.get("id", "")
            self._refresh_list_widget()

    def _browse_identity(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Choose SSH Private Key", os.path.expanduser("~/.ssh"))
        if filename:
            self.edit_identity.setText(filename)
            self._on_field_edited()

    def _test_connection(self):
        from calibre_plugins.kindle_sftp.transport import SSHTransport
        dev = self._current_device()
        if not dev:
            return
        try:
            validate_device(dev)
            if not dev.get("host"):
                raise ValueError("Enter the Kindle hostname or IP address first.")
            opts = {
                "host": dev.get("host", "").strip(),
                "port": int(dev.get("port", 22) or 22),
                "username": dev.get("username", "root") or "root",
                "password": dev.get("password", ""),
                "identity_file": dev.get("identity_file", ""),
                "documents": dev.get("documents") or "/mnt/us/documents",
                "user_store": dev.get("user_store", ""),
                "known_hosts": os.path.expanduser(dev.get("known_hosts") or "~/.ssh/known_hosts"),
                "timeout": int(dev.get("timeout", 8) or 8),
            }
            transport = SSHTransport(opts)
            try:
                transport.connect()
                total, free = transport.space()
                disp = format_device_label(dev)
                info_dialog(
                    self, "Kindle SFTP Connection Successful",
                    f"Connected to {disp}!\n\n"
                    f"SSH authentication, SFTP subsystem, and documents directory verified.\n"
                    f"Storage: {free / 1024**3:.2f} GiB free of {total / 1024**3:.2f} GiB.",
                    show=True,
                )
            finally:
                transport.close()
        except Exception as error:
            error_dialog(self, "Kindle SFTP Connection Failed", str(error), show=True)

    def _trust_host(self):
        import hashlib, base64
        dev = self._current_device()
        if not dev:
            return
        try:
            validate_device(dev)
            host = dev.get("host", "").strip()
            port = int(dev.get("port", 22))
            if not host:
                raise ValueError("Enter the Kindle hostname or IP address first (e.g. 192.168.1.50).")
            timeout = int(dev.get("timeout", 8) or 8)
            keys = get_host_keys({**dev, 'timeout': timeout})
            fingerprints = []
            for line in keys:
                parts = line.split()
                if len(parts) != 3:
                    raise ValueError("kindle-link returned a malformed SSH host key.")
                digest = hashlib.sha256(base64.b64decode(parts[2], validate=True)).digest()
                fingerprints.append(parts[1] + " SHA256:" + base64.b64encode(digest).decode().rstrip("="))
            if not keys:
                raise ValueError("No SSH host keys were returned. Check the hostname, port, and server.")
            known_hosts_path = os.path.abspath(os.path.expanduser(dev.get("known_hosts") or "~/.ssh/known_hosts"))
            message = (
                f"The server at {host}:{port} supplied:\n\n"
                + "\n".join(fingerprints)
                + f"\n\nAppend these host keys to {known_hosts_path}?"
            )
            answer = QMessageBox.warning(
                self, "Verify SSH Host Identity", message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            os.makedirs(os.path.dirname(known_hosts_path), mode=0o700, exist_ok=True)
            with open(known_hosts_path, "a", encoding="utf-8") as stream:
                stream.write("\n" + "\n".join(keys) + "\n")
            info_dialog(self, "Host Key Trusted", "Host keys saved. You can now test the connection.", show=True)
        except Exception as error:
            error_dialog(self, "Could not verify SSH host", str(error), show=True)

    def _open_tailcat_wizard(self):
        dev = self._current_device()
        if not dev:
            return
        try:
            validate_device(dev)
        except Exception as error:
            error_dialog(self, "Invalid Device Configuration", f"Please fill and fix device settings before setting up Tailcat:\n{error}", show=True)
            return
        if is_tailcat_address(dev.get("host", "")):
            error_dialog(self, "Tailcat Already Configured", "This profile already has a Tailcat address configured.", show=True)
            return

        wizard = TailcatSetupWizardDialog(self, dev)
        wizard.exec()

    def _show_tailcat_info(self):
        curr_dev = self._current_device()
        host = curr_dev.get("host", "") if curr_dev else ""
        device_id = curr_dev.get("device_id", "") if curr_dev else ""
        dlg = TailcatInfoDialog(self, host=host, device_id=device_id)
        dlg.exec()

    def _save_and_accept(self):
        try:
            for dev in self.devices:
                validate_device(dev)
            if not self.active_id and self.devices:
                self.active_id = self.devices[0].get("id", "")
            save_devices(self.devices, self.active_id, revision=self.revision)
            self.accept()
        except Exception as error:
            title = "Connection Save Conflict" if "revision" in str(error).lower() or "conflict" in str(error).lower() else "Invalid Device Configuration"
            error_dialog(self, title, str(error), show=True)


class DeployTailcatWorker(QThread):
    log_msg = pyqtSignal(str)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, dev_opts: dict, bin_path: Optional[str], arch: str, force_key: bool):
        super().__init__()
        self.dev_opts = dev_opts
        self.bin_path = bin_path
        self.arch = arch
        self.force_key = force_key

    def run(self):
        try:
            tc_addr = setup_tailcat(
                self.dev_opts,
                binary_path=self.bin_path,
                arch=self.arch,
                force_new_key=self.force_key,
                logger=self.log_msg.emit,
            )
            self.finished_ok.emit(tc_addr)
        except Exception as exc:
            self.failed.emit(str(exc))


class TailcatInfoDialog(QDialog):
    """Modal dialog explaining how Tailcat works, files installed on Kindle, and uninstallation."""

    def __init__(self, parent=None, host="", device_id=""):
        super().__init__(parent)
        self.setWindowTitle("About Tailcat Remote Access")
        self.setWindowIcon(QIcon.ic("devices/kindle.png"))
        self.resize(680, 540)

        layout = QVBoxLayout(self)

        browser = QTextBrowser(self)
        browser.setOpenExternalLinks(True)

        stable_id = device_id
        if not stable_id and host:
            try:
                matches = [row for row in get_devices()
                           if row.get("endpoint") == "tailcat" and row.get("host") == host]
            except Exception:
                matches = []
            if len(matches) == 1:
                stable_id = matches[0].get("device_id", "")
        try:
            binary = executable()
        except Exception:
            binary = "kindle-link"
        self.uninstall_cmd = shlex.join([binary, "uninstall", "--device", stable_id or "DEVICE_ID"])

        html = f"""
        <h2>Tailcat Remote Access</h2>
        <p><b>Tailcat</b> (<code>tailscale/tailcat</code>) provides secure, encrypted point-to-point connections over Tailscale's global WireGuard data plane and NAT-traversal relays (DERP), <b>without requiring a Tailscale account, central coordination server, or open firewall ports</b>.</p>
        <p>The kindle-link broker owns provisioning and records the resulting endpoint in its device registry. It obtains the official static Linux release on demand, keeping this plugin small and transparent.</p>

        <h3>How it Works</h3>
        <ul>
            <li>Your Kindle generates a permanent, private WireGuard Curve25519 node key stored exclusively on the device (<code>kindle.private.json</code>).</li>
            <li>From this key, Tailcat derives a unique, permanent address (e.g. <code>tcpGFw...</code>).</li>
            <li>When you activate Tailcat on the Kindle, it listens and forwards encrypted traffic to the local Dropbear SSH server (port 22).</li>
            <li>Calibre connects to that address through OpenSSH's <code>ProxyCommand</code>, providing seamless wireless book sync from anywhere outside your home network.</li>
            <li><b>On-Demand Only:</b> Tailcat is NOT an on-boot daemon and does not run continuously in deep sleep. It runs only when you trigger its scriptlet on the Kindle.</li>
        </ul>

        <h3>What is Installed on the Kindle</h3>
        <p><b>All files are strictly isolated to user storage (<code>/mnt/us/</code>)</b>. No system partitions (<code>/app</code>, <code>/etc</code>, <code>/usr</code>) or root files are ever modified:</p>
        <ul>
            <li><code>/mnt/us/tailcat/bin/tailcat</code>: Static Linux executable (ARMv7 or ARM64).</li>
            <li><code>/mnt/us/tailcat/LICENSE</code>: Official Tailscale BSD 3-Clause license.</li>
            <li><code>/mnt/us/tailcat/kindle.private.json</code>: Permanent server node key.</li>
            <li><code>/mnt/us/tailcat/tailcat.addr</code>: Text file storing your unique Tailcat address.</li>
            <li><code>/mnt/us/tailcat/start-tailcat.sh</code> &amp; <code>stop-tailcat.sh</code>: Background runner scriptlets.</li>
            <li><code>/mnt/us/documents/Calibre Start Tailcat.sh</code>: Home library launcher.</li>
            <li><code>/mnt/us/documents/Calibre Stop Tailcat.sh</code>: Home library launcher.</li>
            <li><code>/mnt/us/extensions/tailcat/</code>: Optional KUAL menu extension (if KUAL is present).</li>
        </ul>

        <h3>Full Uninstallation</h3>
        <p>The kindle-link CLI owns Tailcat removal and removes the registry endpoint after a successful device operation.</p>
        <p><b>Method 1 — Run the broker command:</b></p>
        <p>Copy and run this command on the computer where Calibre and kindle-link are installed:</p>
        <pre style="background: #2b2b2b; color: #f0f0f0; padding: 10px; border-radius: 4px; font-family: monospace; white-space: pre-wrap; word-break: break-all;">{self.uninstall_cmd}</pre>
        <p>The command contains the stable registry device ID; it does not guess a LAN address or execute an SSH shell pipeline.</p>
        <p>Do not delete individual files while Tailcat is running. The broker command performs the device cleanup and registry update as one operation.</p>
        """
        browser.setHtml(html)
        layout.addWidget(browser)

        btn_box = QHBoxLayout()
        btn_copy = QPushButton("Copy Uninstall Command")
        btn_copy.setToolTip("Copy the kindle-link CLI uninstallation command to clipboard")
        btn_copy.clicked.connect(self._copy_uninstall)
        btn_box.addWidget(btn_copy)
        btn_box.addStretch(1)

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        btn_box.addWidget(btn_close)
        layout.addLayout(btn_box)

    def _copy_uninstall(self):
        QApplication.clipboard().setText(self.uninstall_cmd)
        info_dialog(self, "Copied to Clipboard", "The uninstallation command has been copied to your clipboard.", show=True)


class TailcatSetupWizardDialog(QDialog):
    """Wizard to deploy Tailcat to Kindle user storage and configure remote access."""

    def __init__(self, parent: ManageConnectionsDialog, device: dict):
        super().__init__(parent)
        self.device = device
        self.worker = None
        self.setWindowTitle("Setup Tailcat Remote Access for Kindle")
        self.setWindowIcon(QIcon.ic("devices/kindle.png"))
        self.resize(680, 500)

        layout = QVBoxLayout(self)

        header = QLabel(
            "<h3>Tailcat Remote Access Setup</h3>"
            "<p>The kindle-link broker provisions Tailcat to your Kindle user storage "
            "(<code>/mnt/us/tailcat/</code>) and records the remote endpoint in its registry.<br>"
            "<b>No root filesystem modifications or boot daemons are installed.</b> Tailcat runs "
            "on-demand via scriptlets when you activate it on the Kindle.</p>"
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        info_group = QGroupBox("Target Kindle Profile", self)
        info_layout = QFormLayout(info_group)
        alias_str = self.device.get("alias") or "Kindle"
        host_val = self.device.get('host') or "192.168.1.50"
        host_str = f"{host_val}:{self.device.get('port', 22)}"
        user_str = self.device.get("username", "root")
        info_layout.addRow("Device Alias:", QLabel(f"<b>{alias_str}</b>"))
        info_layout.addRow("LAN Address:", QLabel(f"<code>{host_str}</code> (user: {user_str})"))
        layout.addWidget(info_group)

        cfg_group = QGroupBox("Deployment Configuration", self)
        cfg_layout = QFormLayout(cfg_group)

        self.radio_download = QRadioButton("Download official Tailcat release automatically from GitHub (Recommended)")
        self.radio_download.setChecked(True)
        self.radio_download.toggled.connect(self._on_source_toggled)
        cfg_layout.addRow(self.radio_download)

        arch_row = QHBoxLayout()
        self.combo_arch = QComboBox()
        self.combo_arch.addItem("Auto-detect Kindle architecture (ARMv7 / ARM64)", "auto")
        self.combo_arch.addItem("Linux ARMv7 (Paperwhite, Oasis, Voyage, Basic)", "armv7")
        self.combo_arch.addItem("Linux ARM64 (Kindle Scribe)", "arm64")
        arch_row.addWidget(QLabel("Target Architecture:"))
        arch_row.addWidget(self.combo_arch, stretch=1)
        cfg_layout.addRow("", arch_row)

        self.radio_local = QRadioButton("Use local binary file…")
        self.radio_local.toggled.connect(self._on_source_toggled)
        cfg_layout.addRow(self.radio_local)

        bin_row = QHBoxLayout()
        self.edit_bin = QLineEdit()
        self.edit_bin.setPlaceholderText("Select local static Linux ARM tailcat binary…")
        self.edit_bin.setEnabled(False)
        bin_row.addWidget(self.edit_bin)
        self.btn_browse = QPushButton("Browse…")
        self.btn_browse.setEnabled(False)
        self.btn_browse.clicked.connect(self._browse_binary)
        bin_row.addWidget(self.btn_browse)
        cfg_layout.addRow("", bin_row)

        self.chk_force_key = QCheckBox("Generate new key even if one already exists on Kindle")
        self.chk_force_key.setToolTip("Unchecked: reuses existing permanent address if /mnt/us/tailcat/kindle.private.json exists.")
        cfg_layout.addRow(self.chk_force_key)
        layout.addWidget(cfg_group)

        layout.addWidget(QLabel("Deployment Log:"))
        self.log_edit = QPlainTextEdit(self)
        self.log_edit.setReadOnly(True)
        self.log_edit.setStyleSheet("font-family: monospace; font-size: 11px;")
        layout.addWidget(self.log_edit, stretch=1)

        btn_box = QHBoxLayout()
        self.btn_deploy = QPushButton("Deploy Tailcat to Kindle")
        self.btn_deploy.setStyleSheet("font-weight: bold; padding: 6px 14px;")
        self.btn_deploy.clicked.connect(self._start_deployment)
        btn_box.addWidget(self.btn_deploy)

        self.btn_help = QPushButton("?")
        self.btn_help.setToolTip("About Tailcat remote access, files installed, and uninstallation")
        self.btn_help.setStyleSheet("font-weight: bold; font-size: 13px; min-width: 28px; max-width: 28px; padding: 6px 0;")
        self.btn_help.clicked.connect(self._show_tailcat_info)
        btn_box.addWidget(self.btn_help)

        btn_box.addStretch(1)
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.accept)
        btn_box.addWidget(self.btn_close)
        layout.addLayout(btn_box)

    def _on_source_toggled(self):
        is_dl = self.radio_download.isChecked()
        self.combo_arch.setEnabled(is_dl)
        self.edit_bin.setEnabled(not is_dl)
        self.btn_browse.setEnabled(not is_dl)

    def _show_tailcat_info(self):
        host = self.device.get("host", "") if getattr(self, "device", None) else ""
        device_id = self.device.get("device_id", "") if getattr(self, "device", None) else ""
        dlg = TailcatInfoDialog(self, host=host, device_id=device_id)
        dlg.exec()

    def _browse_binary(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "Select Tailcat ARM Binary", os.path.dirname(self.edit_bin.text()) or os.path.expanduser("~")
        )
        if filename:
            self.edit_bin.setText(filename)

    def _append_log(self, text: str):
        self.log_edit.appendPlainText(text)
        self.log_edit.verticalScrollBar().setValue(self.log_edit.verticalScrollBar().maximum())

    def _start_deployment(self):
        use_download = self.radio_download.isChecked()
        bin_path = None
        arch = "auto"

        if not use_download:
            bin_path = self.edit_bin.text().strip()
            if not bin_path or not os.path.isfile(bin_path):
                error_dialog(self, "Binary Not Found", "Please select a valid local Tailcat ARM binary file first.", show=True)
                return
        else:
            arch = self.combo_arch.currentData() or "auto"

        self.btn_deploy.setEnabled(False)
        self.btn_close.setEnabled(False)
        self.btn_browse.setEnabled(False)
        self.edit_bin.setEnabled(False)
        self.combo_arch.setEnabled(False)
        self.radio_download.setEnabled(False)
        self.radio_local.setEnabled(False)
        self.chk_force_key.setEnabled(False)
        self.log_edit.clear()
        self._append_log("Starting deployment...")

        force_key = self.chk_force_key.isChecked()
        dev_opts = dict(self.device)
        dev_opts["known_hosts"] = os.path.expanduser(dev_opts.get("known_hosts") or "~/.ssh/known_hosts")
        dev_opts["timeout"] = int(dev_opts.get("timeout", 10) or 10)

        self.worker = DeployTailcatWorker(dev_opts, bin_path, arch, force_key)
        self.worker.log_msg.connect(self._append_log)
        self.worker.finished_ok.connect(self._on_success)
        self.worker.failed.connect(self._on_failure)
        self.worker.start()

    def _on_success(self, tc_addr: str):
        self._append_log("\n✓ Deployment completed successfully!")
        self.btn_close.setEnabled(True)

        parent = self.parent()
        orig_alias = self.device.get("alias") or "Kindle"
        base_alias = re.sub(r"\s*\((?:Tailcat|Tailscale)\)\s*$", "", orig_alias, flags=re.IGNORECASE).strip() or "Kindle"
        new_alias = f"{base_alias} (Tailscale)"
        # setup_tailcat persists the endpoint in the Rust registry. Reload that
        # row instead of inventing a second ID in the dialog draft.
        registry_devices = get_devices()
        remote = next(
            (row for row in registry_devices
             if row.get("endpoint") == "tailcat"
             and (row.get("host") == tc_addr or row.get("device_id") == self.device.get("device_id"))),
            None,
        )
        if remote is None:
            error_dialog(
                self, "Tailcat Setup Incomplete",
                "Tailcat reported success, but kindle-link did not return the saved remote endpoint.",
                show=True,
            )
            return
        remote["alias"] = new_alias
        existing_idx = next(
            (idx for idx, row in enumerate(parent.devices)
             if row.get("id") == remote.get("id")
             or (row.get("device_id") == remote.get("device_id") and row.get("endpoint") == "tailcat")),
            None,
        )
        if existing_idx is None:
            parent.devices.append(dict(remote))
            existing_idx = len(parent.devices) - 1
        else:
            # Keep unsaved edits to every other draft row intact.
            parent.devices[existing_idx] = dict(remote)
        parent.active_id = remote.get("id", "")
        parent.revision = registry_revision()
        parent._refresh_list_widget()
        parent.list_widget.setCurrentRow(existing_idx)

        msg = (
            f"Tailcat has been successfully configured on your Kindle!\n\n"
            f"Permanent Address:\n{tc_addr}\n\n"
            f"Profile:\n{new_alias}\n\n"
            f"To connect remotely:\n"
            f"1. Start Tailcat on your Kindle (choose whichever is easiest):\n"
            f"   • Tap 'Calibre Start Tailcat' in your Kindle Home Library, OR\n"
            f"   • In KUAL under 'Calibre Tailcat' -> 'Start Calibre Tailcat', OR\n"
            f"   • In the Kindle search bar: ;sh /mnt/us/tailcat/start-tailcat.sh\n\n"
            f"2. In Calibre, switch to '{new_alias}' from the toolbar dropdown and click 'Link Kindle Wi-Fi'."
        )
        QMessageBox.information(self, "Tailcat Setup Complete", msg)

    def _on_failure(self, error_msg: str):
        self._append_log(f"\n✗ Error: {error_msg}")
        self.btn_deploy.setEnabled(True)
        self.btn_close.setEnabled(True)
        self.radio_download.setEnabled(True)
        self.radio_local.setEnabled(True)
        self._on_source_toggled()
        self.chk_force_key.setEnabled(True)
        error_dialog(self, "Tailcat Setup Failed", error_msg, show=True)


class KindleSFTPAction(InterfaceAction):
    """Calibre toolbar action button and dropdown menu for Kindle Wi-Fi connection."""

    name = "Kindle SFTP Device"
    action_spec = ("Link Kindle Wi-Fi", "devices/kindle.png", "Connect to Kindle over Wi-Fi (SFTP)", None)
    action_type = "global"
    popup_type = QToolButton.ToolButtonPopupMode.MenuButtonPopup
    action_add_menu = True

    def genesis(self):
        if not hasattr(self, 'qaction') or self.qaction is None:
            self.create_action()
        self._refresh_pending = False
        self.qaction.setIcon(QIcon.ic("devices/kindle.png"))
        self.qaction.triggered.connect(self.toggle_connection)
        self.menu = self.qaction.menu()
        self.menu.aboutToShow.connect(self.rebuild_menu)
        self.rebuild_menu()

        # Timer to monitor connection state and keep button label in sync
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(1000)
        self._poll_timer.timeout.connect(self.update_button_state)
        self._poll_timer.start()
        self.update_button_state()
        self._ensure_toolbar_presence()

    def _ensure_toolbar_presence(self):
        try:
            from calibre.gui2 import gprefs
            modified = False
            for key in ("action-layout-toolbar", "action-layout-toolbar-device"):
                actions = list(gprefs.get(key, []))
                if actions and self.name not in actions:
                    actions.append(self.name)
                    gprefs[key] = tuple(actions)
                    modified = True
            if modified and hasattr(self.gui, "bars_manager"):
                self.gui.bars_manager.init_bars()
        except Exception:
            pass

    def initialization_complete(self):
        self._ensure_toolbar_presence()

    def is_kindle_connected(self) -> bool:
        dm = getattr(self.gui, "device_manager", None)
        return bool(
            dm and dm.is_device_connected and getattr(dm.device, "name", "") == "Kindle SFTP Device"
        )

    def update_button_state(self):
        connected = self.is_kindle_connected()
        active = get_active_device()
        info = format_device_label(active)

        if connected:
            self.qaction.setText("Disconnect Kindle Wi-Fi")
            self.qaction.setToolTip(f"Connected to {info}. Click to disconnect.")
        else:
            self.qaction.setText("Link Kindle Wi-Fi")
            if active.get("host"):
                self.qaction.setToolTip(f"Connect to {info} over Wi-Fi")
            else:
                self.qaction.setToolTip("Configure and link Kindle over Wi-Fi")
        self.refresh_action.setEnabled(connected and not self._refresh_pending)

    def toggle_connection(self):
        if self.is_kindle_connected():
            self.disconnect_kindle()
        else:
            self.connect_kindle()

    def show_status_message(self, message: str, timeout: int = 3000):
        sb = getattr(self.gui, "status_bar", None)
        if sb and hasattr(sb, "show_message"):
            sb.show_message(message, timeout)

    def disconnect_kindle(self):
        dm = getattr(self.gui, "device_manager", None)
        if dm:
            for dev in getattr(dm, "devices", []):
                if getattr(dev, "name", "") == "Kindle SFTP Device":
                    with getattr(dev, "_lock", None) or dev:
                        dev._manual_connect_requested = False
                        dev._ejected = True
            if dm.is_device_connected:
                dm.umount_device()
        self.show_status_message("Kindle Wi-Fi disconnected.", 3000)
        self.update_button_state()

    def connect_kindle(self):
        active = get_active_device()
        if not active.get("host"):
            self.open_manage_connections()
            return
        dm = getattr(self.gui, "device_manager", None)
        if dm:
            for dev in getattr(dm, "devices", []):
                if getattr(dev, "name", "") == "Kindle SFTP Device":
                    with getattr(dev, "_lock", None) or dev:
                        dev._ejected = False
                        dev._next_probe = 0.0
                        dev._must_disconnect = False
                        dev._manual_connect_requested = True
            if hasattr(dm, "detect_device"):
                dm.detect_device()
        alias = active.get("alias") or "Kindle"
        self.show_status_message(f"Connecting to {alias} over Wi-Fi…", 3000)
        self.update_button_state()

    def select_device(self, dev_id):
        set_active_device_id(dev_id)
        active = get_active_device()
        dev_label = format_device_label(active)
        dm = getattr(self.gui, "device_manager", None)

        if self.is_kindle_connected():
            # If already connected, disconnect old device and connect to newly selected device
            dm.umount_device()
            self.show_status_message(f"Switched active Kindle to {dev_label}. Reconnecting…", 3000)
            self.connect_kindle()
        else:
            self.show_status_message(f"Selected Kindle: {dev_label}", 3000)
        self.update_button_state()

    def rebuild_menu(self):
        self.menu.clear()
        devices = get_devices()
        active_id = get_active_device_id()

        # 1. "Selected Kindle" submenu
        sel_menu = self.menu.addMenu(QIcon.ic("devices/kindle.png"), "Selected Kindle")
        group = QActionGroup(self)
        group.setExclusive(True)

        if not devices:
            none_act = sel_menu.addAction("No Kindles configured")
            none_act.setEnabled(False)
        else:
            for dev in devices:
                dev_id = dev.get("id", "")
                label = format_device_label(dev)
                act = QAction(label, sel_menu)
                act.setCheckable(True)
                act.setChecked(dev_id == active_id)
                act.triggered.connect(partial(self.select_device, dev_id))
                group.addAction(act)
                sel_menu.addAction(act)

        sel_menu.addSeparator()
        sub_manage = sel_menu.addAction(QIcon.ic("config.png"), "Manage Connections…")
        sub_manage.triggered.connect(self.open_manage_connections)

        # 2. Main menu separator and entries
        self.menu.addSeparator()
        self.refresh_action = self.menu.addAction(
            QIcon.ic("view-refresh.png"), "Full refresh Kindle library",
        )
        self.refresh_action.setEnabled(self.is_kindle_connected() and not self._refresh_pending)
        self.refresh_action.triggered.connect(self.request_full_refresh)
        pref_act = self.menu.addAction(QIcon.ic("config.png"), "Preferences…")
        pref_act.triggered.connect(self.open_preferences)

    def request_full_refresh(self):
        if not self.is_kindle_connected() or self._refresh_pending:
            return
        dm = self.gui.device_manager
        self._refresh_pending = True
        self.update_button_state()
        try:
            dm.create_job(
                partial(self._run_full_refresh, dm.device),
                Dispatcher(self._full_refresh_done),
                "Full refresh Kindle library",
            )
        except Exception:
            self._refresh_pending = False
            self.update_button_state()
            raise
        self.show_status_message("Requesting a full native Kindle library refresh…", 5000)

    def _run_full_refresh(self, device):
        if not self.is_kindle_connected() or self.gui.device_manager.device is not device:
            raise RuntimeError("The Kindle connection changed before the refresh could start.")
        return device.refresh_library()

    def _full_refresh_done(self, job):
        self._refresh_pending = False
        self.update_button_state()
        if job.failed:
            error_dialog(
                self.gui, "Kindle library refresh failed",
                "Return to the native Kindle library and check that its stock scanner is running. "
                "No book files were changed by this refresh request.",
                det_msg=job.details, show=True,
            )
        elif not job.result:
            info_dialog(
                self.gui, "Kindle library refresh skipped",
                "KOReader is active. Return to the native Kindle library, then choose "
                "Full refresh Kindle library again.",
                show=True,
            )
        else:
            self.show_status_message(
                "Full native Kindle library refresh requested; indexing runs asynchronously.", 8000,
            )

    def open_manage_connections(self):
        dlg = ManageConnectionsDialog(self.gui)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.update_button_state()
            self.rebuild_menu()

    def open_preferences(self):
        self.interface_action_base_plugin.do_user_config(self.gui)
