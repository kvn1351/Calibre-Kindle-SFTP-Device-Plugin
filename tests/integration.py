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
    from calibre_plugins.kindle_sftp.config import prefs, get_devices, save_devices, connection_options
    from calibre_plugins.kindle_sftp.broker import executable, get_host_keys, setup_tailcat, uninstall_tailcat
    from calibre_plugins.kindle_sftp.transport import (
        SSHTransport, TransportCommandError, TransportError, TransportSecurityError,
    )

    app = Application([])
    if not hasattr(app, 'addAction'):
        app.addAction = lambda *a: None
    state = json.loads(Path(os.environ[STATE_ENV]).read_text())
    fixture = Path(state['fixture'])
    template = next(p for p in device_plugins(include_disabled=True) if p.name == 'Kindle SFTP Device')

    class Integration(unittest.TestCase):
        def setUp(self):
            self.user_store = fixture / 'documents' / self._testMethodName
            self.root = self.user_store / 'documents'
            self.root.mkdir(parents=True)
            self.options = {**state['options'], 'documents': str(self.root), 'user_store': str(self.user_store)}
            with prefs:
                prefs['registry_migrated'] = True
                prefs['enabled'] = True
                prefs['auto_connect'] = True
                prefs['read_metadata'] = True
                prefs['generation'] = uuid.uuid4().hex
            row = {**self.options, 'id': 'fixture:lan', 'device_id': 'fixture',
                   'endpoint': 'lan', 'alias': 'FixtureKindle'}
            save_devices([row], active_id=row['id'])
            self.plugin = type(template)(template.plugin_path)
            self.plugin.startup()

        def tearDown(self):
            self.plugin.shutdown()
            for marker in ('fail-scanner', 'fail-commit', 'foreground.json', 'fail-appmgr'):
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
            rows = get_devices()
            rows[0]['host'] = '[::1]'
            save_devices(rows, active_id=rows[0]['id'])
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

        def test_koreader_transfer_delete_and_later_full_refresh(self):
            self.connect()
            foreground = fixture / 'foreground.json'
            foreground.write_text(json.dumps({
                'activeApp': 'tech.hackerdude.shell_integration.launcher',
                'activeContext': ':2:file:///mnt/us/documents/KOReader.sh',
            }))
            (fixture / 'fail-scanner').touch()
            before_calls = self.scanner_calls()
            source = fixture / 'source.txt'
            info = self.metadata('Reading in KOReader', 20)
            locations = self.plugin.upload_books([str(source)], ['source.txt'], metadata=[info])
            target = Path(locations[0][0])
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertIn(str(target), self.plugin._files)
            books = self.plugin.books()
            self.plugin.add_books_to_metadata(locations, [info], (books, None, None))
            self.plugin.sync_booklists((books, None, None))
            self.assertEqual(next(b for b in books if b.path == str(target)).uuid, info.uuid)
            self.assertFalse(self.plugin.refresh_library())
            self.plugin.delete_books([str(target)])
            self.assertFalse(target.exists())
            self.assertEqual(self.scanner_calls(), before_calls)
            (fixture / 'fail-scanner').unlink()
            foreground.unlink()
            self.assertTrue(self.plugin.refresh_library())
            self.assertEqual(self.scanner_calls()[len(before_calls):],
                             [{'action': 'doFullScan', 'target': None}])
            self.assertNotIn(str(target.relative_to(fixture / 'documents')), self.scans()[-1])

        def test_other_launcher_and_unknown_foreground_do_not_hide_scanner_errors(self):
            self.connect()
            foreground = fixture / 'foreground.json'
            foreground.write_text(json.dumps({
                'activeApp': 'tech.hackerdude.shell_integration.launcher',
                'activeContext': ':2:file:///mnt/us/documents/Other.sh',
            }))
            self.assertTrue(self.plugin.refresh_library())
            self.assertEqual(self.scanner_calls()[-1], {'action': 'doFullScan', 'target': None})
            foreground.write_text(json.dumps({
                'activeApp': 'tech.hackerdude.shell_integration.launcher',
                'activeContext': ':2:file:///mnt/us/documents/KOReader.sh',
            }))
            (fixture / 'fail-appmgr').touch()
            (fixture / 'fail-scanner').touch()
            with self.assertRaises(TransportCommandError):
                self.plugin.refresh_library()

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

        def test_manual_full_refresh_menu_queues_scan_on_connected_device(self):
            from queue import Queue
            from threading import Thread
            from types import SimpleNamespace
            from calibre.gui2.device import DeviceManager
            from calibre_plugins.kindle_sftp.ui import KindleSFTPAction

            self.connect()
            registered = []
            manager = DeviceManager(
                None, SimpleNamespace(add_job=registered.append, changed_queue=Queue()),
                None, None, None, None,
            )
            manager.connected_device = self.plugin
            previous_manager = getattr(app, 'device_manager', None)
            app.device_manager = manager
            action = KindleSFTPAction(app, '')
            try:
                action.interface_action_base_plugin = self.plugin
                action.do_genesis()
                action.menu.show()
                app.processEvents()
                before_calls = len(self.scanner_calls())
                action.refresh_action.trigger()
                self.assertEqual(self.scanner_calls()[before_calls:], [])
                self.assertFalse(action.refresh_action.isEnabled())
                action.rebuild_menu()
                self.assertFalse(action.refresh_action.isEnabled())
                job = manager.jobs.get_nowait()
                worker = Thread(target=job.run)
                worker.start()
                deadline = time.monotonic() + 15
                while action._refresh_pending and time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(0.01)
                worker.join(timeout=1)
                self.assertFalse(action._refresh_pending)
                self.assertFalse(job.failed, job.details)
                self.assertTrue(action.refresh_action.isEnabled())
                self.assertEqual(self.scanner_calls()[before_calls:],
                                 [{'action': 'doFullScan', 'target': None}])
                manager.connected_device = None
                action.update_button_state()
                self.assertFalse(action.refresh_action.isEnabled())
            finally:
                action._poll_timer.stop()
                action.menu.close()
                action.deleteLater()
                app.device_manager = previous_manager

        def test_registry_and_calibre_share_devices_selection_and_conflicts(self):
            from types import SimpleNamespace
            from calibre_plugins.kindle_sftp.broker import frontend, sync_devices
            from calibre_plugins.kindle_sftp.config import (
                get_active_device_id, registry_revision, set_active_device_id,
            )
            from calibre_plugins.kindle_sftp.ui import KindleSFTPAction, ManageConnectionsDialog
            rows = get_devices()
            other = {**rows[0], 'id': 'second:lan', 'device_id': 'second',
                     'alias': 'SecondKindle', 'host': '192.0.2.2', 'password': 'memory-only-secret'}
            rows.append(other)
            save_devices(rows, active_id='fixture:lan')
            devices = json.loads(subprocess.check_output([executable(), 'devices', '--json']))['devices']
            self.assertEqual({d['id'] for d in devices}, {'fixture', 'second'})
            registry_file = Path(os.environ['XDG_CONFIG_HOME']) / 'tailcat/devices.json'
            if not registry_file.exists():
                registry_file = Path(os.environ['XDG_CONFIG_HOME']) / 'tailcat/devices.json'
            if not registry_file.exists():
                registry_file = Path(os.environ['XDG_CONFIG_HOME']) / 'kindle-link/devices.json'
            self.assertNotIn(b'memory-only-secret', registry_file.read_bytes())
            subprocess.run([executable(), 'use', 'second'], check=True, capture_output=True)
            self.assertEqual(connection_options()['host'], '192.0.2.2')
            self.assertEqual(connection_options()['password'], 'memory-only-secret')
            before = frontend()
            dialog = ManageConnectionsDialog(None)
            try:
                dialog.edit_host.setText('192.0.2.99')
                dialog._on_field_edited()
                dialog.reject()
                self.assertEqual(frontend(), before, 'Cancel persisted draft edits')
                # A CLI update must not be overwritten by a dialog's stale snapshot.
                newer = frontend()
                newer['devices'][0]['alias'] = 'RenamedCLI'
                sync_devices(newer['devices'], newer['active_id'], newer['revision'])
                with self.assertRaises(RuntimeError):
                    save_devices(dialog.devices, dialog.active_id, revision=dialog.revision)
                self.assertEqual(get_devices()[0]['alias'], 'RenamedCLI')
            finally:
                dialog.close()
                dialog.deleteLater()
            previous_manager = getattr(app, 'device_manager', None)
            app.device_manager = SimpleNamespace(is_device_connected=False, device=None)
            action = KindleSFTPAction(app, '')
            try:
                action.interface_action_base_plugin = self.plugin
                action.do_genesis()
                selection = next(a.menu() for a in action.menu.actions() if a.menu())
                first = next(a for a in selection.actions() if 'RenamedCLI' in a.text())
                first.trigger()
                self.assertEqual(get_active_device_id(), 'fixture:lan')
                self.assertEqual(frontend()['active_id'], 'fixture:lan')
            finally:
                action._poll_timer.stop()
                action.deleteLater()
                app.device_manager = previous_manager

        def test_manual_detection_requires_request_and_auto_detection_reconnects(self):
            with prefs:
                prefs['auto_connect'] = False
            self.assertIsNone(self.plugin.detect_managed_devices(set()))
            self.plugin._manual_connect_requested = True
            self.assertIsNotNone(self.plugin.detect_managed_devices(set()))
            self.assertFalse(self.plugin._manual_connect_requested)
            self.plugin.open(None, 'manual-connection-fixture')
            self.plugin.eject()
            self.assertFalse(self.plugin._transport)
            self.assertIsNone(self.plugin.detect_managed_devices(set()))
            with prefs:
                prefs['auto_connect'] = True
                prefs['generation'] = uuid.uuid4().hex
            self.assertIsNotNone(self.plugin.detect_managed_devices(set(), force_refresh=True))

        def test_existing_calibre_profiles_migrate_without_losing_cli_devices(self):
            from calibre_plugins.kindle_sftp.config import get_active_device_id
            address = 'tcLegacy_Profile-Address0123456789'
            lan = {**self.options, 'id': 'old-local', 'alias': 'LegacyReader',
                   'password': 'legacy-secret', 'remember_password': True}
            remote = {**lan, 'id': 'old-remote', 'alias': 'LegacyReader-Tailscale',
                      'host': address, 'port': 22}
            with prefs:
                prefs['registry_migrated'] = False
                prefs['devices'] = [lan, remote]
                prefs['active_device_id'] = 'old-remote'
            rows = get_devices()
            self.assertIn('fixture:lan', [row['id'] for row in rows])
            migrated = [row for row in rows if row['alias'].startswith('LegacyReader')]
            self.assertEqual({row['endpoint'] for row in migrated}, {'lan', 'tailcat'})
            self.assertEqual(len({row['device_id'] for row in migrated}), 1)
            self.assertEqual({row['password'] for row in migrated}, {'legacy-secret'})
            self.assertEqual(get_active_device_id(), 'fixture:lan')
            self.assertIsNone(prefs.get('devices'))
            self.assertIsNone(prefs.get('password'))
            registry_file = Path(os.environ['XDG_CONFIG_HOME']) / 'tailcat/devices.json'
            if not registry_file.exists():
                registry_file = Path(os.environ['XDG_CONFIG_HOME']) / 'kindle-link/devices.json'
            self.assertNotIn(b'legacy-secret', registry_file.read_bytes())

        def test_cli_interactive_ssh_shell(self):
            import pty
            import select
            import signal
            config = self.user_store / 'shell-config.json'
            config.write_text(json.dumps(self.options))
            pid, terminal = pty.fork()
            if pid == 0:
                os.execv(executable(), [executable(), 'ssh', '--config', str(config)])
            output = bytearray()
            finished = False
            try:
                deadline = time.monotonic() + 20
                status = None
                sent = False
                while time.monotonic() < deadline:
                    if select.select([terminal], [], [], 0.1)[0]:
                        try:
                            chunk = os.read(terminal, 4096)
                        except OSError:
                            chunk = b''
                        output.extend(chunk)
                    if not sent and (b'$' in output or b'#' in output or b'>' in output or time.monotonic() > deadline - 15):
                        os.write(terminal, b"printf '\\n%s_%s\\n' KINDLE_LINK SHELL_OK; exit\n")
                        sent = True
                    exited, status = os.waitpid(pid, os.WNOHANG)
                    if exited:
                        while select.select([terminal], [], [], 0.1)[0]:
                            try:
                                chunk = os.read(terminal, 4096)
                                if not chunk:
                                    break
                                output.extend(chunk)
                            except OSError:
                                break
                        finished = True
                        break
                self.assertTrue(finished, output.decode('utf-8', 'replace'))
                self.assertEqual(os.waitstatus_to_exitcode(status), 0, output)
                self.assertIn(b'KINDLE_LINK_SHELL_OK', output)
            finally:
                if not finished:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                os.close(terminal)

        def test_broker_crash_terminates_its_ssh_and_allows_restart(self):
            import select
            config = self.user_store / 'broker-crash.json'
            config.write_text(json.dumps(self.options))
            client = subprocess.Popen(
                [executable(), 'lease', '--config', str(config)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                self.assertTrue(select.select([client.stdout], [], [], 15)[0])
                reply = json.loads(client.stdout.readline())
                self.assertTrue(reply['ok'], reply)
                status = json.loads(subprocess.check_output([executable(), 'status', '--json']))
                master = next(c for c in status['connections'] if c['control_path'] == reply['control_path'])
                broker_pid = int(subprocess.check_output(
                    ['ps', '-p', str(master['master_pid']), '-o', 'ppid='],
                ).strip())
                os.kill(broker_pid, 9)
                deadline = time.monotonic() + 10
                while Path(reply['control_path']).exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(Path(reply['control_path']).exists(), 'Broker crash left SSH alive')
                result = subprocess.run(
                    [executable(), 'cmd', '--config', str(config), 'printf', 'broker-restarted'],
                    capture_output=True, timeout=15,
                )
                self.assertEqual((result.returncode, result.stdout), (0, b'broker-restarted'), result.stderr)
            finally:
                client.stdin.close()
                try:
                    client.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    client.kill()
                    client.wait(timeout=3)
                client.stdout.close()
                client.stderr.close()

        def test_cli_provisioning_preserves_identity_and_trust(self):
            documents = self.user_store / 'documents'
            documents.mkdir(exist_ok=True)
            options = {**self.options, 'documents': str(documents)}
            address = 'tcSynthetic_CaseSensitive-Address0123456789'
            binary = self.user_store / 'synthetic-tailcat'
            binary.write_text(f'''#!/usr/bin/python3
import json, os, pathlib, sys, time
key = pathlib.Path(next(a.split('=', 1)[1] for a in sys.argv if a.startswith('--key=')))
if 'genkey' in sys.argv:
    if key.exists() and '--force' not in sys.argv:
        raise SystemExit(3)
    value = {address!r} if not key.exists() else {address!r} + 'New'
    key.write_text(json.dumps({{'address': value}}))
    print(value)
elif 'serve' in sys.argv:
    pathlib.Path(os.environ['TAILCAT_ADDR_FILE']).write_text(json.loads(key.read_text())['address'])
    time.sleep(30)
else:
    raise SystemExit(2)
''')
            binary.chmod(0o700)
            license_path = self.user_store / 'synthetic-license'
            license_path.write_bytes(b'Synthetic license fixture\\n')
            result = setup_tailcat(options, str(binary), str(license_path))
            self.assertEqual(result, address)
            remote = self.user_store / 'tailcat'
            self.assertEqual((remote / 'bin/tailcat').read_bytes(), binary.read_bytes())
            self.assertEqual((remote / 'LICENSE').read_bytes(), license_path.read_bytes())
            key_before = (remote / 'kindle.private.json').read_bytes()
            trust_before = Path(options['known_hosts']).read_bytes()
            self.assertEqual(setup_tailcat(options, str(binary), str(license_path)), address)
            self.assertEqual((remote / 'kindle.private.json').read_bytes(), key_before)
            self.assertEqual(Path(options['known_hosts']).read_bytes(), trust_before)
            self.assertEqual((remote / 'tailcat.addr').read_text().strip(), address)
            # Recovery must retain the saved key rather than silently replace it.
            (remote / 'tailcat.addr').unlink()
            self.assertEqual(setup_tailcat(options, str(binary), str(license_path)), address)
            self.assertEqual((remote / 'kindle.private.json').read_bytes(), key_before)
            self.assertEqual(
                setup_tailcat(options, str(binary), str(license_path), force_new_key=True),
                address + 'New',
            )
            self.assertNotEqual((remote / 'kindle.private.json').read_bytes(), key_before)
            self.assertTrue((documents / 'Calibre Start Tailcat.sh').is_file())
            self.assertTrue((documents / 'Calibre Stop Tailcat.sh').is_file())
            self.assertEqual(list(remote.rglob('*.tmp-*')), [])
            keys = get_host_keys(options)
            trusted_key = (fixture / 'host_key.pub').read_text().split()[:2]
            self.assertIn(trusted_key, [line.split()[1:3] for line in keys])
            profiles = json.loads(subprocess.check_output([executable(), 'devices', '--json']))['devices']
            device = next(d for d in profiles if d.get('tailcat', {}).get('host') == address + 'New')
            unrelated = documents / 'unrelated-book.txt'
            unrelated.write_bytes(b'Preserve this book during uninstall.')
            owner = SSHTransport(self.options)
            owner.connect()
            try:
                with self.assertRaises(RuntimeError):
                    uninstall_tailcat(self.options)
                self.assertTrue(remote.is_dir())
            finally:
                owner.close()
            unrelated_process = subprocess.Popen(['sleep', '60'])
            try:
                (remote / 'tailcat.pid').write_text(str(unrelated_process.pid))
                with self.assertRaises(RuntimeError):
                    uninstall_tailcat(self.options)
                self.assertIsNone(unrelated_process.poll(), 'Uninstall killed an unrelated process')
                self.assertTrue(remote.is_dir())
            finally:
                unrelated_process.terminate()
                unrelated_process.wait(timeout=5)
                (remote / 'tailcat.pid').unlink(missing_ok=True)
            removed = uninstall_tailcat(self.options)
            self.assertTrue(removed.get('ok'))
            self.assertFalse(remote.exists())
            self.assertFalse((documents / 'Calibre Start Tailcat.sh').exists())
            self.assertFalse((documents / 'Calibre Stop Tailcat.sh').exists())
            self.assertEqual(unrelated.read_bytes(), b'Preserve this book during uninstall.')
            remaining = json.loads(subprocess.check_output([executable(), 'devices', '--json']))['devices']
            selected = next(d for d in remaining if d['id'] == device['id'])
            self.assertIsNone(selected['tailcat'])
            self.assertEqual(selected['lan']['host'], self.options['host'])

        def test_shared_connection_survives_one_owner_and_closes_after_last_sigkill(self):
            import select
            binary = executable()
            config = self.user_store / 'connection.json'
            config.write_text(json.dumps(self.options))
            config.chmod(0o600)
            cli = subprocess.Popen(
                [binary, 'lease', '--config', str(config)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            calibre = None
            try:
                self.assertTrue(select.select([cli.stdout], [], [], 15)[0], 'CLI lease never became ready')
                first = json.loads(cli.stdout.readline())
                self.assertTrue(first['ok'], first)
                code = (
                    'import json, time; '
                    'from calibre.customize.ui import initialize_plugins; initialize_plugins(); '
                    'from calibre_plugins.kindle_sftp.transport import SSHTransport; '
                    f't = SSHTransport(json.load(open({str(config)!r}))); t.connect(); '
                    'print(json.dumps({\"control_path\": t._control_path}), flush=True); time.sleep(120)'
                )
                calibre = subprocess.Popen(
                    [state['calibre_debug'], '-c', code],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                self.assertTrue(select.select([calibre.stdout], [], [], 15)[0], 'Calibre lease never became ready')
                raw_line = calibre.stdout.readline()
                if not raw_line:
                    err = calibre.stderr.read().decode('utf-8', 'replace')
                    self.fail(f"calibre process sent empty line (exit code {calibre.poll()}). stderr: {err}")
                second = json.loads(raw_line)
                self.assertEqual(first['control_path'], second['control_path'])
                status = json.loads(subprocess.check_output([binary, 'status', '--json']))
                connection = next(c for c in status['connections'] if c['control_path'] == first['control_path'])
                self.assertEqual(connection['leases'], 2)
                master_pid = connection['master_pid']
                calibre.kill()
                calibre.wait(timeout=5)
                command = subprocess.run(
                    [binary, 'cmd', '--config', str(config), 'sh', '-c',
                     'printf \"%s\" \"$1\"; printf \"synthetic stderr\" >&2; exit 7',
                     'sh', 'spaces $ and \"quotes\"'],
                    capture_output=True, timeout=15,
                )
                self.assertEqual(command.returncode, 7, command.stderr)
                self.assertEqual(command.stdout, b'spaces $ and \"quotes\"')
                self.assertEqual(command.stderr, b'synthetic stderr')
                status = json.loads(subprocess.check_output([binary, 'status', '--json']))
                self.assertEqual(status['connections'][0]['master_pid'], master_pid)
                cli.kill()
                cli.wait(timeout=5)
                deadline = time.monotonic() + 10
                while Path(first['control_path']).exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(Path(first['control_path']).exists(), 'Last owner death left an SSH master')
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        os.kill(master_pid, 0)
                        time.sleep(0.05)
                    except ProcessLookupError:
                        break
                with self.assertRaises(ProcessLookupError):
                    os.kill(master_pid, 0)
            finally:
                for child in (cli, calibre):
                    if child is not None:
                        if child.poll() is None:
                            child.kill()
                            child.wait(timeout=5)
                        for stream in (child.stdin, child.stdout, child.stderr):
                            if stream is not None:
                                stream.close()

        def test_concurrent_clients_coalesce_and_reject_different_trust(self):
            from concurrent.futures import ThreadPoolExecutor
            clients = [SSHTransport(self.options) for _ in range(4)]
            try:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    list(pool.map(lambda client: client.connect(), clients))
                self.assertEqual(len({client._control_path for client in clients}), 1)
                clients[0].close()
                self.assertEqual(clients[1]._remote_command('printf still-connected').stdout, b'still-connected')
                unknown_hosts = self.user_store / 'different-known-hosts'
                unknown_hosts.write_text('')
                untrusted = SSHTransport({**self.options, 'known_hosts': str(unknown_hosts)})
                try:
                    with self.assertRaises(TransportError):
                        untrusted.connect()
                finally:
                    untrusted.close()
                self.assertEqual(clients[1]._remote_command('printf trusted-owner').stdout, b'trusted-owner')
            finally:
                for client in clients:
                    client.close()

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
        appmgr = root / 'bin/lipc-get-prop'
        appmgr.write_text(f'''#!{python_bin}
import json, pathlib, sys
root = pathlib.Path({str(root)!r})
if (root/'fail-appmgr').exists():
    print('Synthetic app manager unavailable', file=sys.stderr)
    raise SystemExit(3)
if len(sys.argv) != 3 or sys.argv[1] != 'com.lab126.appmgrd':
    raise SystemExit(2)
state = root/'foreground.json'
values = json.loads(state.read_text()) if state.exists() else {{
    'activeApp': 'com.lab126.booklet.home',
    'activeContext': 'native-library',
}}
print(values[sys.argv[2]])
''')
        appmgr.chmod(0o700)
        scanner = root / 'bin/lipc-set-prop'
        scanner.write_text(f'''#!{python_bin}
import json, pathlib, sys
root = pathlib.Path({str(root)!r})
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

with (root/'scanner_calls.jsonl').open('a') as output:
    output.write(json.dumps({{'action': action, 'target': target}})+'\\n')
if (root/'fail-scanner').exists():
    print('Synthetic scanner unavailable', file=sys.stderr)
    raise SystemExit(3)
files = sorted(str(p.relative_to(root/'documents')) for p in (root/'documents').rglob('*') if p.is_file())
with (root/'scans.jsonl').open('a') as output:
    output.write(json.dumps(files)+'\\n')
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
if [ -z "$SSH_ORIGINAL_COMMAND" ]; then exec /bin/sh -i; fi
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
        state.write_text(json.dumps({'fixture': str(root), 'calibre_debug': str(debug), 'ebook_convert': str(convert), 'ipv6': ipv6, 'options': {
            'host': '127.0.0.1', 'port': port, 'username': getpass.getuser(),
            'identity_file': str(root / 'client_key'), 'known_hosts': str(root / 'known_hosts'),
            'password': '', 'remember_password': False, 'timeout': 5,
        }}))
        env = {
            **os.environ, 'CALIBRE_CONFIG_DIRECTORY': str(root / 'calibre-config'),
            'QT_QPA_PLATFORM': 'offscreen', STATE_ENV: str(state),
            'TAILCATCTL_RUNTIME_DIR': str(root / 'broker'),
            'KINDLE_LINK_RUNTIME_DIR': str(root / 'broker'),
            'TAILCATCTL_CONFIG_DIR': str(root / 'config/tailcat'),
            'XDG_CONFIG_HOME': str(root / 'config'), 'XDG_CACHE_HOME': str(root / 'cache'),
        }
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
