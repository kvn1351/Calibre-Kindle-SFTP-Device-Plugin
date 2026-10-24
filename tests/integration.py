#!/usr/bin/env python3
"""Run isolated, real SSH/SFTP and Calibre integration checks. Never uses a Kindle."""

import argparse
import getpass
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid


STATE_ENV = 'KINDLE_SFTP_TEST_STATE'


def client_tests():
    from calibre.customize.ui import available_output_formats, device_plugins
    from calibre.db.legacy import LibraryDatabase
    from calibre.ebooks.metadata.book.base import Metadata
    from calibre.ebooks.metadata.meta import get_metadata
    from calibre.gui2 import Application
    from calibre.gui2.library.models import BooksModel
    from calibre_plugins.kindle_sftp.config import prefs
    from calibre_plugins.kindle_sftp.transport import (
        SSHTransport, TransportCommandError, TransportError, TransportSecurityError,
        deploy_tailcat, download_tailcat_binary, enroll_host_key_for_tailcat,
        find_host_tailcat, get_kindle_architecture, is_tailcat_address,
        probe_host,
    )

    app = Application([])
    if not hasattr(app, 'addAction'):
        app.addAction = lambda *a: None
    state = json.loads(Path(os.environ[STATE_ENV]).read_text())
    fixture = Path(state['fixture'])
    template = next(p for p in device_plugins(include_disabled=True) if p.name == 'Kindle SFTP Device')

    class Integration(unittest.TestCase):
        def setUp(self):
            self.root = fixture / 'documents' / self._testMethodName
            self.root.mkdir(parents=True)
            self.user_store = fixture / 'store' / self._testMethodName
            self.user_store.mkdir(parents=True)
            self.options = {**state['options'], 'documents': str(self.root), 'user_store': str(self.user_store)}
            with prefs:
                for key, value in self.options.items():
                    prefs[key] = value
                prefs['enabled'] = True
                prefs['auto_connect'] = True
                prefs['read_metadata'] = True
                prefs['generation'] = uuid.uuid4().hex
            self.plugin = type(template)(template.plugin_path)
            self.plugin.startup()

        def tearDown(self):
            self.plugin.shutdown()
            for marker in ('fail-scanner', 'fail-commit'):
                (fixture / marker).unlink(missing_ok=True)
            shutil.rmtree(self.root, ignore_errors=True)
            shutil.rmtree(self.user_store, ignore_errors=True)

        def connect(self):
            self.assertIsNotNone(self.plugin.detect_managed_devices(set(), force_refresh=True))
            self.plugin.open(None, 'isolated-fixture-library')
            return self.plugin.books()

        def scans(self):
            path = fixture / 'scans.jsonl'
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

        def scanner_calls(self):
            path = fixture / 'scanner_calls.jsonl'
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

        def metadata(self, title, number):
            info = Metadata(title, ['Synthetic Author'])
            info.id = number
            info.uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, title))
            return info

        def test_round_trip_metadata_eject_and_native_refresh(self):
            (self.root / 'existing.txt').write_bytes(b'Existing synthetic ebook.\n')
            books = self.connect()
            self.assertIn('existing.txt', [book.lpath for book in books])
            source = fixture / 'source.txt'
            metadata = [self.metadata('First Book', 1), self.metadata('Second Book', 2)]
            before = len(self.scans())
            before_calls = len(self.scanner_calls())
            locations = self.plugin.upload_books([str(source)] * 2, ['first.txt', 'second.txt'], metadata=metadata)
            self.assertEqual(len(self.scans()), before + 2)
            calls = self.scanner_calls()[before_calls:]
            self.assertEqual([c['action'] for c in calls], ['reScanFile', 'reScanFile'])
            snapshot = self.scans()[-1]
            for path, _ in locations:
                self.assertIn(str(Path(path).relative_to(fixture / 'documents')), snapshot)
                self.assertEqual(Path(path).read_bytes(), source.read_bytes())
            self.assertFalse(any(name.endswith('.part') for name in snapshot))
            self.plugin.add_books_to_metadata(locations, metadata, (books, None, None))
            self.plugin.sync_booklists((books, None, None))
            back = io.BytesIO()
            self.plugin.get_file(locations[0][0], back)
            self.assertEqual(back.getvalue(), source.read_bytes())
            local = self.plugin.prepare_addable_books([locations[0][0]])[0]
            self.assertIsInstance(local, str)
            self.assertEqual(Path(local).read_bytes(), source.read_bytes())
            Path(local).unlink()
            self.assertGreater(self.plugin.free_space()[0], 0)
            self.plugin.eject()
            self.assertIsNone(self.plugin.detect_managed_devices(set(), force_refresh=True))
            self.plugin.post_yank_cleanup()
            with prefs:
                prefs['generation'] = uuid.uuid4().hex
            reconnected = self.connect()
            cached = next(book for book in reconnected if book.path == locations[0][0])
            self.assertEqual((cached.title, cached.authors, cached.uuid),
                             (metadata[0].title, metadata[0].authors, metadata[0].uuid))
            before = len(self.scans())
            before_calls = len(self.scanner_calls())
            paths = [path for path, _ in locations]
            self.plugin.delete_books(paths)
            self.plugin.remove_books_from_metadata(paths, (reconnected, None, None))
            self.plugin.sync_booklists((reconnected, None, None))
            self.assertEqual(len(self.scans()), before + 1)
            self.assertEqual(self.scanner_calls()[before_calls]['action'], 'doFullScan')
            self.assertTrue(all(not Path(path).exists() for path in paths))
            self.assertEqual([book.lpath for book in self.plugin.books()], ['existing.txt'])

        @unittest.skipUnless(state['ipv6'], 'IPv6 loopback unavailable')
        def test_bracketed_ipv6_detection_and_sftp(self):
            with prefs:
                prefs['host'] = '[::1]'
                prefs['generation'] = uuid.uuid4().hex
            self.connect()
            target = str(self.root / 'ipv6.txt')
            transport = self.plugin._connection()
            transport.write_bytes_atomic(target, b'IPv6 ebook')
            self.assertEqual(transport.read_bytes(target), b'IPv6 ebook')
            transport.remove(target)
            self.assertFalse(Path(target).exists())

        def test_host_identity_path_confinement_and_literal_globs(self):
            empty = fixture / 'empty-known-hosts'
            empty.write_text('')
            unknown = SSHTransport({**self.options, 'known_hosts': str(empty)})
            try:
                with self.assertRaises(TransportSecurityError):
                    unknown.connect()
            finally:
                unknown.close()
            wrong = fixture / 'wrong-known-hosts'
            wrong.write_text('[127.0.0.1]:' + str(self.options['port']) + ' '
                             + (fixture / 'client_key.pub').read_text())
            changed = SSHTransport({**self.options, 'known_hosts': str(wrong)})
            try:
                with self.assertRaises(TransportSecurityError):
                    changed.connect()
            finally:
                changed.close()
            self.connect()
            transport = self.plugin._connection()
            paths = [self.root / name for name in ('Report[12].txt', 'Report1.txt', 'Report2.txt')]
            for index, path in enumerate(paths):
                transport.write_bytes_atomic(str(path), f'sentinel {index}'.encode())
            self.assertEqual(transport.read_bytes(str(paths[0])), b'sentinel 0')
            transport.remove(str(paths[0]))
            self.assertFalse(paths[0].exists())
            self.assertEqual(paths[1].read_bytes(), b'sentinel 1')
            self.assertEqual(paths[2].read_bytes(), b'sentinel 2')
            outside = fixture / 'outside-sentinel.txt'
            outside.write_bytes(b'outside sentinel')
            (self.root / 'escape').symlink_to(fixture, target_is_directory=True)
            escaped = str(self.root / 'escape' / outside.name)
            for operation in (
                lambda: transport.read_bytes(escaped),
                lambda: transport.write_bytes_atomic(escaped, b'overwrite'),
                lambda: transport.remove(escaped),
                lambda: transport.remove(str(self.root / '..' / 'outside.txt')),
            ):
                with self.assertRaises(TransportSecurityError):
                    operation()
            self.assertEqual(outside.read_bytes(), b'outside sentinel')
            with self.assertRaises(ValueError):
                self.plugin.delete_books([str(outside)])

        def test_failed_commit_preserves_previous_book_and_cleans_stage(self):
            self.connect()
            transport = self.plugin._connection()
            target = str(self.root / 'replace.txt')
            transport.write_bytes_atomic(target, b'original complete book')
            (fixture / 'fail-commit').touch()
            try:
                with self.assertRaises(TransportCommandError):
                    transport.write_bytes_atomic(target, b'new complete book')
            finally:
                (fixture / 'fail-commit').unlink()
            self.assertEqual(Path(target).read_bytes(), b'original complete book')
            self.assertEqual(list(self.root.glob('.calibre-sftp-*.part')), [])

        def test_refresh_failure_reports_committed_files(self):
            self.connect()
            metadata = self.metadata('Scanner Unavailable', 3)
            source = fixture / 'source.txt'
            before = len(self.scans())
            (fixture / 'fail-scanner').touch()
            with self.assertRaises(RuntimeError):
                self.plugin.upload_books([str(source)], ['source.txt'], metadata=[metadata])
            committed = list(self.root.rglob('*.txt'))
            self.assertEqual(len(committed), 1)
            self.assertEqual(committed[0].read_bytes(), source.read_bytes())
            self.assertEqual(len(self.scans()), before)
            (fixture / 'fail-scanner').unlink()
            self.plugin.delete_books([str(committed[0])])
            self.assertEqual(len(self.scans()), before + 1)

        def test_partial_upload_refreshes_successfully_committed_books(self):
            self.connect()
            second = fixture / 'temporary-second-book.txt'
            second.write_bytes(b'Second book disappears during the batch')
            first = fixture / 'source.txt'
            metadata = [self.metadata('Completed Book', 5), self.metadata('Interrupted Book', 6)]
            before = len(self.scans())

            def remove_second_after_first(fraction, message):
                if fraction == 0.5:
                    second.unlink(missing_ok=True)

            self.plugin.set_progress_reporter(remove_second_after_first)
            with self.assertRaises(TransportError):
                self.plugin.upload_books([str(first), str(second)], ['first.txt', 'second.txt'], metadata=metadata)
            committed = list(self.root.rglob('*.txt'))
            self.assertEqual(len(committed), 1)
            self.assertEqual(committed[0].read_bytes(), first.read_bytes())
            self.assertEqual(len(self.scans()), before + 1)
            self.assertIn(str(committed[0].relative_to(fixture / 'documents')), self.scans()[-1])
            self.assertFalse(any(name.endswith('.part') for name in self.scans()[-1]))

        def test_standard_calibre_auto_conversion_then_sftp_delivery(self):
            html = fixture / 'conversion.html'
            html.write_text('<html><head><title>Conversion Fixture</title></head><body><h1>Conversion Fixture</h1><p>Synthetic integration book.</p></body></html>')
            epub = fixture / 'conversion.epub'
            convert = state['ebook_convert']
            subprocess.run([convert, str(html), str(epub), '--title', 'Conversion Fixture', '--authors', 'Synthetic Author'],
                           check=True, capture_output=True, timeout=120)
            db = LibraryDatabase(str(fixture / 'isolated-library'))
            try:
                info = self.metadata('Conversion Fixture', 4)
                book_id = db.create_book_entry(info)
                with epub.open('rb') as stream:
                    db.add_format(book_id, 'EPUB', stream, index_is_id=True)
                # Execute the real selector used by Calibre's Send-to-device flow.
                class ModelContext:
                    pass
                context = ModelContext()
                context.db = db
                formats = self.plugin.settings().format_map
                direct, automatic = BooksModel.get_preferred_formats_from_ids(context, [book_id], formats, exclude_auto=True)
                self.assertEqual(direct, [])
                self.assertEqual(automatic, [book_id])
                output_format = next(fmt for fmt in formats if fmt in available_output_formats())
                converted = fixture / ('converted.' + output_format)
                subprocess.run([convert, str(epub), str(converted)], check=True, capture_output=True, timeout=120)
                with converted.open('rb') as stream:
                    db.add_format(book_id, output_format.upper(), stream, index_is_id=True)
                direct, automatic = BooksModel.get_preferred_formats_from_ids(context, [book_id], formats, exclude_auto=True)
                self.assertEqual(automatic, [])
                self.assertEqual(len(direct), 1)
                self.assertEqual(Path(direct[0]).suffix, '.' + output_format)
                self.connect()
                with open(direct[0], 'rb') as stream:
                    metadata = get_metadata(stream, output_format)
                locations = self.plugin.upload_books(direct, ['converted.' + output_format], metadata=[metadata])
                result = io.BytesIO()
                self.plugin.get_file(locations[0][0], result)
                self.assertEqual(result.getvalue(), Path(direct[0]).read_bytes())
                result.seek(0)
                self.assertEqual(get_metadata(result, output_format).title, 'Conversion Fixture')
                for path in direct:
                    Path(path).unlink()
            finally:
                db.close()

        def test_thumbnail_generation_and_upload(self):
            self.connect()
            transport = self.plugin._connection()

            # Test transport.upload_thumbnail validation
            with self.assertRaises(ValueError):
                transport.upload_thumbnail('../traversal.jpg', b'data')
            with self.assertRaises(ValueError):
                transport.upload_thumbnail('cover.png', b'data')
            with self.assertRaises(ValueError):
                transport.upload_thumbnail('thumbnail_uuid_EBOK_portrait.jpg', b'')

            # Test transport.upload_thumbnail successful placement
            sample_thumb = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00'
            thumb_name = 'thumbnail_testuuid123_EBOK_portrait.jpg'
            transport.upload_thumbnail(thumb_name, sample_thumb)

            sys_thumb = self.user_store / 'system' / 'thumbnails' / thumb_name
            bug_thumb = self.user_store / 'amazon-cover-bug' / thumb_name
            self.assertTrue(sys_thumb.exists())
            self.assertTrue(bug_thumb.exists())
            self.assertEqual(sys_thumb.read_bytes(), sample_thumb)
            self.assertEqual(bug_thumb.read_bytes(), sample_thumb)

            # Test plugin._upload_thumbnail integration with converted book
            info = self.metadata('Thumbnail Book', 10)
            info.thumbnail = (300, 400, sample_thumb)

            html = fixture / 'thumb_book.html'
            html.write_text('<html><body><h1>Thumbnail Test</h1></body></html>')
            azw3 = fixture / 'thumb_book.azw3'
            convert = state['ebook_convert']
            subprocess.run([convert, str(html), str(azw3), '--title', 'Thumbnail Book', '--authors', 'Synthetic Author'],
                           check=True, capture_output=True, timeout=120)

            # Upload book and verify companion thumbnail was extracted from azw3 EXTH header and uploaded
            from calibre.devices.kindle.driver import thumbnail_filename
            with open(azw3, 'rb') as f:
                expected_tfname = thumbnail_filename(f)
            self.assertTrue(expected_tfname.startswith('thumbnail_'))
            self.assertTrue(expected_tfname.endswith('_portrait.jpg'))

            locations = self.plugin.upload_books([str(azw3)], ['thumb_book.azw3'], metadata=[info])
            self.assertEqual(len(locations), 1)

            uploaded_sys = self.user_store / 'system' / 'thumbnails' / expected_tfname
            uploaded_bug = self.user_store / 'amazon-cover-bug' / expected_tfname
            self.assertTrue(uploaded_sys.exists())
            self.assertTrue(uploaded_bug.exists())
            self.assertEqual(uploaded_sys.read_bytes(), sample_thumb)
            self.assertEqual(uploaded_bug.read_bytes(), sample_thumb)

        def test_toolbar_action_and_multiple_kindle_profiles(self):
            from calibre_plugins.kindle_sftp.config import (
                get_devices, save_devices, get_active_device, get_active_device_id,
                set_active_device_id, connection_options,
            )
            from calibre_plugins.kindle_sftp.ui import KindleSFTPAction

            # 1. Test Multiple Kindle device profiles configuration
            dev1 = {
                'id': 'pw5-test',
                'alias': 'Paperwhite 5',
                'host': '192.168.1.101',
                'port': 22,
                'username': 'root',
                'password': '',
                'remember_password': False,
                'identity_file': '',
                'documents': '/mnt/us/documents',
                'user_store': '',
            }
            dev2 = {
                'id': 'oasis-test',
                'alias': 'Kindle Oasis',
                'host': '192.168.1.102',
                'port': 2222,
                'username': 'root',
                'password': 'secretpassword',
                'remember_password': True,
                'identity_file': '',
                'documents': '/mnt/us/documents',
                'user_store': '',
            }
            save_devices([dev1, dev2], active_id='pw5-test')
            self.assertEqual(len(get_devices()), 2)
            self.assertEqual(get_active_device_id(), 'pw5-test')
            self.assertEqual(get_active_device()['alias'], 'Paperwhite 5')
            opts1 = connection_options()
            self.assertEqual(opts1['host'], '192.168.1.101')
            self.assertEqual(opts1['port'], 22)

            # Switch active device
            set_active_device_id('oasis-test')
            self.assertEqual(get_active_device_id(), 'oasis-test')
            opts2 = connection_options()
            self.assertEqual(opts2['host'], '192.168.1.102')
            self.assertEqual(opts2['port'], 2222)
            self.assertEqual(opts2['password'], 'secretpassword')

            # 2. Test Toolbar Action instantiation & UI elements
            action = KindleSFTPAction(app, '')
            action.interface_action_base_plugin = self.plugin
            action.do_genesis()

            # Main button text initially
            self.assertEqual(action.qaction.text(), 'Link Kindle Wi-Fi')

            # Test Menu structure
            action.rebuild_menu()
            menu_actions = action.menu.actions()
            self.assertGreaterEqual(len(menu_actions), 3)
            self.assertFalse(any('Manage Connections' in a.text() for a in menu_actions if not a.isSeparator()))

            # Check Submenu "Selected Kindle"
            submenus = [a.menu() for a in menu_actions if a.menu() and a.menu().title() == 'Selected Kindle']
            self.assertEqual(len(submenus), 1)
            sub_actions = submenus[0].actions()
            labels = [a.text() for a in sub_actions]
            self.assertTrue(any('Paperwhite 5' in lbl for lbl in labels))
            self.assertTrue(any('Kindle Oasis' in lbl for lbl in labels))
            self.assertTrue(any('Manage Connections' in lbl for lbl in labels))

            # Trigger device selection from menu
            oasis_action = next(a for a in sub_actions if 'Kindle Oasis' in a.text())
            self.assertTrue(oasis_action.isChecked())

            pw5_action = next(a for a in sub_actions if 'Paperwhite 5' in a.text())
            pw5_action.trigger()
            self.assertEqual(get_active_device_id(), 'pw5-test')

            # 3. Simulate connection state changes
            class MockConnectedDevice:
                name = 'Kindle SFTP Device'

            class MockDeviceManager:
                is_device_connected = True
                device = MockConnectedDevice()
                def umount_device(self):
                    self.is_device_connected = False

            app.device_manager = MockDeviceManager()
            action.update_button_state()
            self.assertEqual(action.qaction.text(), 'Disconnect Kindle Wi-Fi')

            # Toggle disconnect
            action.toggle_connection()
            self.assertFalse(app.device_manager.is_device_connected)
            self.assertEqual(action.qaction.text(), 'Link Kindle Wi-Fi')

            # 3b. Verify ManageConnectionsDialog Tailcat button visibility & no documents setting
            from calibre_plugins.kindle_sftp.ui import ManageConnectionsDialog, TailcatInfoDialog
            dlg = ManageConnectionsDialog(None, initial_device_id='pw5-test')
            self.assertFalse(hasattr(dlg, 'edit_documents'))
            self.assertFalse(dlg.btn_tailcat.isHidden())
            self.assertFalse(dlg.btn_tailcat_help.isHidden())
            # Use synthetic dummy Tailcat address for UI visibility testing
            dummy_tc = "tcpGFwMOCK000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
            dlg.edit_host.setText(dummy_tc)
            dlg._on_field_edited()
            self.assertTrue(dlg.btn_tailcat.isHidden())
            self.assertTrue(dlg.btn_tailcat_help.isHidden())
            dlg.close()

            # 3c. Verify TailcatInfoDialog contents and uninstallation commands
            info_dlg = TailcatInfoDialog(None, host='192.168.1.100')
            self.assertIn('killall tailcat', info_dlg.UNINSTALL_CMD)
            self.assertIn('Calibre Start Tailcat.sh', info_dlg.UNINSTALL_CMD)
            self.assertIn('ssh root@192.168.1.100', info_dlg.ssh_cmd)
            info_dlg.close()

            # Default example IP without host provided, platform-agnostic, and clean branding
            default_dlg = TailcatInfoDialog(None)
            self.assertIn('ssh root@192.168.1.50', default_dlg.ssh_cmd)
            default_dlg.close()

            # 4. Test Auto-connect vs Manual-connect detection
            live_dev = {
                'id': 'live-test',
                'alias': 'Live Test Kindle',
                'host': self.options['host'],
                'port': self.options['port'],
                'username': self.options['username'],
                'password': '',
                'remember_password': False,
                'identity_file': self.options['identity_file'],
                'documents': str(self.root),
                'user_store': str(self.user_store),
            }
            save_devices([live_dev], active_id='live-test')

            # When auto_connect is False, background polling does NOT detect:
            with prefs:
                prefs['auto_connect'] = False
                prefs['generation'] = uuid.uuid4().hex
            self.plugin._update_options()
            self.plugin._manual_connect_requested = False
            self.assertIsNone(self.plugin.detect_managed_devices(set(), force_refresh=False))

            # When manual connect is requested:
            self.plugin._manual_connect_requested = True
            dev_detected = self.plugin.detect_managed_devices(set(), force_refresh=False)
            self.assertIsNotNone(dev_detected)
            self.assertFalse(self.plugin._manual_connect_requested)

            # When auto_connect is True, background polling detects automatically:
            with prefs:
                prefs['auto_connect'] = True
                prefs['generation'] = uuid.uuid4().hex
            self.plugin._update_options()
            self.plugin._next_probe = 0.0
            self.assertIsNotNone(self.plugin.detect_managed_devices(set(), force_refresh=False))

        def test_tailcat_helpers_and_deployment(self):
            # 1. Address detection
            valid_tc = "tcpGFwWCARC9nyT4M9SVE46qLzGwVVfusQ5M6XLt9vyLj2ps5zSWFrWC"
            self.assertTrue(is_tailcat_address(valid_tc))
            self.assertFalse(is_tailcat_address("192.168.1.150"))
            self.assertFalse(is_tailcat_address("kindle.local"))
            self.assertFalse(is_tailcat_address(""))
            self.assertFalse(is_tailcat_address("tc.short"))

            # 2. Host key enrollment
            kh_path = self.user_store / "test_known_hosts"
            kh_path.write_text("127.0.0.1 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESTKEY123456789\n")
            self.assertTrue(enroll_host_key_for_tailcat("127.0.0.1", 22, valid_tc, str(kh_path)))
            kh_content = kh_path.read_text()
            self.assertIn(f"{valid_tc.lower()},{valid_tc}", kh_content)
            self.assertIn("AAAAC3NzaC1lZDI1NTE5AAAAITESTKEY123456789", kh_content)

            # 3. Download on demand & architecture detection helpers
            arch = get_kindle_architecture(self.options)
            self.assertIn(arch, ("armv7", "arm64"))

            import io
            import tarfile
            from unittest.mock import MagicMock, patch

            mock_tar_buf = io.BytesIO()
            with tarfile.open(fileobj=mock_tar_buf, mode="w:gz") as tar:
                bin_data = b"#!/bin/sh\necho test\n"
                ti_bin = tarfile.TarInfo(name="tailcat")
                ti_bin.size = len(bin_data)
                tar.addfile(ti_bin, io.BytesIO(bin_data))

                lic_data = b"BSD 3-Clause License\n"
                ti_lic = tarfile.TarInfo(name="LICENSE")
                ti_lic.size = len(lic_data)
                tar.addfile(ti_lic, io.BytesIO(lic_data))

            mock_tar_bytes = mock_tar_buf.getvalue()
            mock_resp = MagicMock()
            mock_resp.read = MagicMock(side_effect=[mock_tar_bytes, b""])
            mock_resp.headers = {"Content-Length": str(len(mock_tar_bytes))}
            mock_resp.__enter__.return_value = mock_resp

            with patch("urllib.request.urlopen", return_value=mock_resp):
                cached_bin, cached_lic = download_tailcat_binary(arch="testarch", version="v9.9.9")
                self.assertTrue(os.path.isfile(cached_bin))
                self.assertTrue(os.path.isfile(cached_lic))
                self.assertTrue(os.access(cached_bin, os.X_OK))

            # 4. Deployment to user store
            mock_bin = self.user_store / "mock_tailcat.sh"
            mock_bin.write_text(f"#!/bin/sh\ncase \"$*\" in *genkey*) echo '# wrote file'; echo '{valid_tc}';; *) echo 'tailcat mock';; esac\n")
            mock_bin.chmod(0o755)

            mock_lic = self.user_store / "mock_LICENSE"
            mock_lic.write_text("BSD 3-Clause License\n")

            logs = []
            tc_addr = deploy_tailcat(
                self.options,
                str(mock_bin),
                license_path=str(mock_lic),
                force_new_key=True,
                logger=logs.append,
            )
            self.assertEqual(tc_addr, valid_tc)
            self.assertTrue(any("Uploading Tailcat" in line for line in logs))
            self.assertTrue(any("Generating permanent" in line for line in logs))

            # Verify deployed files in user_store
            remote_bin = self.user_store / "tailcat" / "bin" / "tailcat"
            self.assertTrue(remote_bin.is_file())
            self.assertEqual(oct(remote_bin.stat().st_mode & 0o777), oct(0o755))

            remote_lic = self.user_store / "tailcat" / "LICENSE"
            self.assertTrue(remote_lic.is_file())
            self.assertEqual(remote_lic.read_text(), "BSD 3-Clause License\n")

            remote_start = self.user_store / "tailcat" / "start-tailcat.sh"
            self.assertTrue(remote_start.is_file())
            self.assertEqual(oct(remote_start.stat().st_mode & 0o777), oct(0o755))
            self.assertIn("nohup", remote_start.read_text())

            remote_stop = self.user_store / "tailcat" / "stop-tailcat.sh"
            self.assertTrue(remote_stop.is_file())
            self.assertEqual(oct(remote_stop.stat().st_mode & 0o777), oct(0o755))

            remote_addr = self.user_store / "tailcat" / "tailcat.addr"
            self.assertTrue(remote_addr.is_file())
            self.assertIn(valid_tc, remote_addr.read_text())

            doc_start = self.user_store / "documents" / "Calibre Start Tailcat.sh"
            self.assertTrue(doc_start.is_file())
            self.assertEqual(oct(doc_start.stat().st_mode & 0o777), oct(0o755))
            doc_content = doc_start.read_text()
            self.assertIn("Author: @kvn1351", doc_content)
            self.assertIn("Name: Calibre - Start Tailcat", doc_content)

            # 5. Transport ProxyCommand configuration
            tailcat_opts = dict(self.options)
            tailcat_opts["host"] = valid_tc
            t = SSHTransport(tailcat_opts)
            t._runtime_dir = str(self.user_store / "t_runtime")
            t._control_path = str(self.user_store / "t_runtime" / "sock")
            args = t._common_args(control=False)
            self.assertTrue(any("ProxyCommand=" in arg for arg in args))
            self.assertTrue(any(valid_tc in arg for arg in args))

    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Integration))
    del app
    if not result.wasSuccessful():
        raise SystemExit(1)


def run_fixture():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--calibre-debug', default=shutil.which('calibre-debug') or '/Applications/calibre.app/Contents/MacOS/calibre-debug')
    args = parser.parse_args()
    debug = Path(args.calibre_debug).resolve()
    customize, convert = debug.with_name('calibre-customize'), debug.with_name('ebook-convert')
    sshd = shutil.which('sshd') or '/usr/sbin/sshd'
    sftp_server = next((path for path in ('/usr/libexec/sftp-server', '/usr/lib/openssh/sftp-server', '/usr/lib/ssh/sftp-server') if Path(path).is_file()), None)
    if not debug.is_file() or not Path(sshd).is_file() or sftp_server is None:
        raise SystemExit('Install Calibre and an OpenSSH server/SFTP subsystem before running this optional integration suite.')
    project = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project))
    from build_plugin import build
    with tempfile.TemporaryDirectory(prefix='kindle-sftp-integration-', dir='/tmp') as temporary:
        root = Path(temporary).resolve()
        (root / 'documents').mkdir()
        (root / 'bin').mkdir()
        (root / 'calibre-config').mkdir()
        (root / 'source.txt').write_bytes(b'Synthetic book transfer payload.\n')
        for key in ('host_key', 'client_key'):
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(root / key)], check=True)
        (root / 'authorized_keys').write_text((root / 'client_key.pub').read_text())
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        ipv6 = False
        try:
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
                probe.bind(('::1', port))
            ipv6 = True
        except OSError:
            pass
        (root / 'known_hosts').write_text(f'[127.0.0.1]:{port} ' + (root / 'host_key.pub').read_text())
        if ipv6:
            with (root / 'known_hosts').open('a') as stream:
                stream.write(f'[::1]:{port} ' + (root / 'host_key.pub').read_text())
        python_bin = '/usr/bin/python3' if Path('/usr/bin/python3').is_file() else (shutil.which('python3') or sys.executable)
        if sys.platform == 'darwin':
            stat = root / 'bin/stat'
            stat.write_text(f'#!{python_bin}\nimport os, sys\ns = os.stat(sys.argv[3])\nprint({{"%s": s.st_size, "%Y": int(s.st_mtime)}}[sys.argv[2]])\n')
            stat.chmod(0o700)
        scanner = root / 'bin/lipc-set-prop'
        scanner.write_text(f'''#!{python_bin}
import json, pathlib, sys
root = pathlib.Path({str(root)!r})
if (root/'fail-scanner').exists():
    print('Synthetic scanner unavailable', file=sys.stderr)
    raise SystemExit(1)
args = sys.argv[1:]
if args == ['-i', 'com.lab126.scanner', 'doFullScan', '1']:
    action = 'doFullScan'
    target = None
elif len(args) == 4 and args[:3] == ['-i', 'com.lab126.scanner', 'reScanFile']:
    action = 'reScanFile'
    target = args[3]
else:
    print('Synthetic scanner unexpected invocation:', args, file=sys.stderr)
    raise SystemExit(2)

files = sorted(str(p.relative_to(root/'documents')) for p in (root/'documents').rglob('*') if p.is_file())
with (root/'scans.jsonl').open('a') as output:
    output.write(json.dumps(files)+'\\n')
with (root/'scanner_calls.jsonl').open('a') as output:
    output.write(json.dumps({{'action': action, 'target': target}})+'\\n')
''')
        scanner.chmod(0o700)
        wrapper = root / 'remote-shell'
        wrapper.write_text(f'''#!/bin/sh
if [ "$SSH_ORIGINAL_COMMAND" = "{sftp_server}" ]; then exec {sftp_server}; fi
if [ -f {root}/fail-commit ]; then
    case "$SSH_ORIGINAL_COMMAND" in *'mv -f '*) echo 'Synthetic commit failure' >&2; exit 73;; esac
fi
PATH={root}/bin:/usr/bin:/bin:/usr/sbin:/sbin
export PATH
exec /bin/sh -c "$SSH_ORIGINAL_COMMAND"
''')
        wrapper.chmod(0o700)
        config = root / 'sshd_config'
        config.write_text(f'''Port {port}
ListenAddress 127.0.0.1
HostKey {root}/host_key
PidFile {root}/sshd.pid
AuthorizedKeysFile {root}/authorized_keys
StrictModes no
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
AllowUsers {getpass.getuser()}
Subsystem sftp {sftp_server}
ForceCommand {wrapper}
LogLevel ERROR
''')
        if ipv6:
            with config.open('a') as stream:
                stream.write('ListenAddress ::1\n')
        state = root / 'state.json'
        state.write_text(json.dumps({'fixture': str(root), 'ebook_convert': str(convert), 'ipv6': ipv6, 'options': {
            'host': '127.0.0.1', 'port': port, 'username': getpass.getuser(),
            'identity_file': str(root / 'client_key'), 'known_hosts': str(root / 'known_hosts'),
            'password': '', 'remember_password': False, 'timeout': 5,
        }}))
        env = {**os.environ, 'CALIBRE_CONFIG_DIRECTORY': str(root / 'calibre-config'), 'QT_QPA_PLATFORM': 'offscreen', STATE_ENV: str(state)}
        package = build(root / 'KindleSFTP.zip')
        subprocess.run([str(customize), '-a', str(package)], env=env, check=True)
        with (root / 'sshd.log').open('wb') as log:
            server = subprocess.Popen([sshd, '-D', '-e', '-f', str(config)], stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 10
                while True:
                    if server.poll() is not None:
                        raise RuntimeError((root / 'sshd.log').read_text())
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=0.1):
                            break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError('Isolated SSH server did not become ready')
                        time.sleep(0.05)
                subprocess.run([str(debug), '-e', str(Path(__file__).resolve())], env=env, check=True, timeout=300)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


if __name__ == '__main__':
    if os.environ.get(STATE_ENV):
        client_tests()
    else:
        run_fixture()
