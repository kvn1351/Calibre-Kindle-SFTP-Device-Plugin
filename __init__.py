"""A Calibre device interface for stock Kindles reached over SSH/SFTP."""

import json
import os
import posixpath
import socket
import sys
import threading
import time
import traceback
import uuid

# When Calibre reloads this top-level module upon updating the plugin ZIP in a running
# session, existing submodules in sys.modules may still point to old code. Evict them
# so new functions and symbols are freshly loaded.
_pkg_prefix = __name__ + '.'
for _mod_name in list(sys.modules):
    if _mod_name.startswith(_pkg_prefix):
        sys.modules.pop(_mod_name, None)

from calibre.customize import InterfaceActionBase
from calibre.devices.errors import FreeSpaceError
from calibre.devices.interface import DevicePlugin
from calibre.devices.mime import mime_type_ext
from calibre.devices.usbms.books import Book as USBBook, BookList
from calibre.ebooks.metadata.book.base import Metadata
from calibre.ebooks.metadata.book.json_codec import JsonCodec

from calibre_plugins.kindle_sftp.config import FORMATS, connection_options, device_settings
from calibre_plugins.kindle_sftp.transport import SSHTransport


class RemoteBook(USBBook):
    """Calibre's standard book behavior without local filesystem access."""

    def __init__(self, prefix, lpath, size=None, other=None):
        Metadata.__init__(self, '')
        self._new_book = False
        self.device_collections = []
        self.lpath = lpath
        self.path = posixpath.join(prefix, lpath)
        self.mime = mime_type_ext(posixpath.splitext(lpath)[1].lstrip('.'))
        self.size = size
        self.datetime = time.gmtime()
        if other is not None:
            self.smart_update(other)


class KindleSFTP(DevicePlugin, InterfaceActionBase):
    name = 'Kindle SFTP Device'
    gui_name = 'Kindle (SFTP)'
    description = 'Manage books on a jailbroken Kindle over SSH/SFTP without USB storage or KOReader.'
    author = 'Kevin Johansson (@kvn1351)'
    version = (0, 10, 0)
    minimum_calibre_version = (9, 15, 0)
    supported_platforms = ['osx', 'linux']
    actual_plugin = 'calibre_plugins.kindle_sftp.ui:KindleSFTPAction'
    MANAGES_DEVICE_PRESENCE = True
    CAN_SET_METADATA = []
    BACKLOADING_ERROR_MESSAGE = None
    FORMATS = list(FORMATS)
    path_sep = '/'
    icon = 'kindle.png'
    SUPPORTS_SUB_DIRS = True
    METADATA_CACHE = '.calibre-kindle-sftp.json'
    OPEN_FEEDBACK_MESSAGE = 'Connecting to Kindle over SSH/SFTP…'
    THUMBNAIL_HEIGHT = 500

    def __init__(self, plugin_path):
        super().__init__(plugin_path)
        self._lock = threading.RLock()
        self._transport = None
        self._options = None
        self._ejected = False
        self._next_probe = 0.0
        self._open = False
        self._must_disconnect = False
        self._stopped = False
        self._manual_connect_requested = False
        self._root = '/mnt/us/documents'
        self._files = {}
        self._booklist = BookList(None, self._root, self.settings)
        self._driveinfo = {}
        self._uid = ''
        self.last_error = ''
        self.report_progress = lambda fraction, message: None

    @classmethod
    def settings(cls):
        return device_settings()

    def get_gui_name(self):
        alias = self._options.get('device_alias') if self._options else None
        return f"{alias} (SFTP)" if alias else self.gui_name

    def is_customizable(self):
        return True

    def config_widget(self):
        from calibre_plugins.kindle_sftp.config import ConfigWidget
        return ConfigWidget()

    def save_settings(self, config_widget):
        config_widget.save_settings()
        with self._lock:
            self._close_transport()
            self._must_disconnect = self._open
            self._open = False
            self._ejected = False
            self._next_probe = 0
            self._options = None

    def startup(self):
        with self._lock:
            self._stopped = False

    def reset(self, key='-1', log_packets=False, report_progress=None, detected_device=None):
        if report_progress is not None:
            self.set_progress_reporter(report_progress)

    def set_progress_reporter(self, report_progress):
        self.report_progress = report_progress or (lambda fraction, message: None)

    def _close_transport(self):
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()

    def _update_options(self):
        options = connection_options()
        if self._options != options:
            was_open = self._open
            self._close_transport()
            self._options = options
            self._root = posixpath.normpath(options.get('documents') or '/mnt/us/documents')
            self._ejected = False
            self._open = False
            self._must_disconnect = self._must_disconnect or was_open
            self._next_probe = 0
        return options

    def detect_managed_devices(self, devices_on_system, force_refresh=False):
        with self._lock:
            options = self._update_options()
            if self._must_disconnect:
                self._must_disconnect = False
                return None
            if self._stopped or not options.get('enabled', True) or not options.get('host') or self._ejected:
                return None
            if self._open:
                if self._transport is not None and self._transport.is_alive():
                    return (options['host'], options['port'])
                self._close_transport()
                self._open = False
                return None

            auto_connect = options.get('auto_connect', True)
            if not auto_connect and not self._manual_connect_requested and not force_refresh:
                return None

            now = time.monotonic()
            if not force_refresh and not self._manual_connect_requested and now < self._next_probe:
                return None
            self._next_probe = now + 10
            if self._transport is None:
                transport = SSHTransport(options)
                try:
                    transport.connect()
                    self._transport = transport
                except Exception as error:
                    transport.close()
                    self.last_error = str(error)
                    self._next_probe = time.monotonic() + 30
                    return None
            self._manual_connect_requested = False
            return (options['host'], options['port'])

    def open(self, connected_device, library_uuid):
        with self._lock:
            options = self._update_options()
            if self._stopped or not options.get('enabled', True) or self._ejected:
                raise RuntimeError('Kindle SFTP connection is disabled or ejected. Save plugin settings to reconnect.')
            self._manual_connect_requested = False
            transport = self._transport or SSHTransport(options)
            try:
                transport.connect()
                self._transport = transport
                self.current_library_uuid = library_uuid
                self._uid = str(uuid.uuid5(uuid.NAMESPACE_URL,
                    f'ssh://{options["username"]}@{options["host"]}:{options["port"]}{self._root}'))
                self._driveinfo = {'main': {
                    'device_store_uuid': self._uid,
                    'device_name': self.gui_name,
                    'location_code': 'main',
                    'last_library_uuid': library_uuid,
                    'prefix': self._root + '/',
                }}
                self._files = {}
                self._booklist = BookList(None, self._root, self.settings)
                self._open = True
                self.last_error = ''
            except Exception as error:
                transport.close()
                self._transport = None
                self._open = False
                self._next_probe = time.monotonic() + 30
                self.last_error = str(error)
                raise

    def debug_managed_device_detection(self, devices_on_system, output):
        options = connection_options()
        print(f'Kindle SFTP: enabled={options.get("enabled", True)}; host={options.get("host", "")!r}; '
              f'port={options.get("port", 22)}; documents={options.get("documents", "/mnt/us/documents")!r}', file=output)
        if not options['enabled'] or not options['host']:
            print('Configure and enable Kindle SFTP in Preferences → Plugins → Device interface.', file=output)
            return False
        transport = None
        try:
            transport = SSHTransport(options)
            transport.connect()
            total, free = transport.space()
            print(f'SSH/SFTP verified; {free} bytes free of {total}. No books were modified.', file=output)
            return True
        except Exception as error:
            print(f'Connection failed: {error}', file=output)
            return False
        finally:
            if transport is not None:
                transport.close()

    def _connection(self):
        if not self._open or self._transport is None:
            raise RuntimeError('The Kindle SFTP device is not connected.')
        return self._transport

    def eject(self):
        with self._lock:
            self._ejected = True
            self._open = False
            self._close_transport()

    def post_yank_cleanup(self):
        with self._lock:
            self._open = False
            self._close_transport()
            self._files = {}

    def shutdown(self):
        with self._lock:
            self._stopped = True
            self._open = False
            self._close_transport()

    def card_prefix(self, end_session=True):
        return (None, None)

    def get_device_uid(self):
        return self._uid

    def get_driveinfo(self):
        return self._driveinfo

    def get_device_information(self, end_session=True):
        return (self.gui_name, '', 'SSH/SFTP', 'application/octet-stream', self._driveinfo)

    def total_space(self, end_session=True):
        with self._lock:
            total, _ = self._connection().space()
            return (total, 0, 0)

    def free_space(self, end_session=True):
        with self._lock:
            _, free = self._connection().space()
            return (free, -1, -1)

    def put_file(self, infile, path, replace_file=False, end_session=True):
        import shutil, tempfile
        with self._lock:
            close = False
            if hasattr(infile, 'name') and os.path.isfile(str(infile.name)):
                source = infile.name
            elif hasattr(infile, 'read'):
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.bin')
                shutil.copyfileobj(infile, tmp)
                tmp.close()
                source = tmp.name
                close = True
            elif isinstance(infile, str) and os.path.isfile(infile):
                source = infile
            else:
                raise ValueError('infile must be a valid file or path')

            try:
                clean = path.strip('/')
                if clean.startswith('documents/'):
                    clean = clean[len('documents/'):]
                elif clean == 'documents' or not clean:
                    clean = os.path.basename(source)
                dest = posixpath.normpath(posixpath.join(self._root, clean))
                if not dest.startswith(self._root + '/'):
                    dest = posixpath.join(self._root, os.path.basename(source))

                self._connection().upload(source, dest)
                self._refresh_library(paths=[dest])
            finally:
                if close:
                    try:
                        os.unlink(source)
                    except OSError:
                        pass

    def get_file(self, path, outfile, end_session=True):
        with self._lock:
            clean = path.strip('/')
            if clean.startswith('documents/'):
                clean = clean[len('documents/'):]
            target = posixpath.normpath(posixpath.join(self._root, clean))
            data = self._connection().read_bytes(target)
            outfile.write(data)

    def list(self, path, recurse=False):
        with self._lock:
            import time
            from types import SimpleNamespace
            files = []
            now = time.time()
            for item in self._connection().list_books():
                fname = posixpath.basename(item['path'])
                files.append(SimpleNamespace(
                    name=fname,
                    path=item['path'],
                    is_dir=False,
                    is_readonly=False,
                    size=item.get('size', 0),
                    ctime=now,
                    wtime=now,
                ))
            return [(path, files)]

    def _relative_path(self, path):
        if not isinstance(path, str) or not path.startswith(self._root + '/'):
            raise ValueError('Book path is outside the configured Kindle documents directory.')
        relative = path[len(self._root) + 1:]
        if (not relative or any(part in ('', '.', '..') for part in relative.split('/'))
                or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in relative)):
            raise ValueError('Unsupported or unsafe Kindle book path.')
        return relative

    def _book_path(self, path):
        self._relative_path(path)
        if posixpath.splitext(path)[1].lstrip('.').lower() not in self.FORMATS:
            raise ValueError('This path is not a supported Kindle book format.')
        return path

    def _inventory(self):
        records = {}
        for item in self._connection().list_books():
            path = item['path']
            relative = self._relative_path(path)
            parts = relative.split('/')
            if any(part.startswith('.') or part.endswith('.sdr') for part in parts):
                continue
            if posixpath.splitext(relative)[1].lstrip('.').lower() not in self.FORMATS:
                continue
            records[path] = item
        return records

    def _read_cache(self):
        try:
            raw = self._connection().read_bytes(posixpath.join(self._root, self.METADATA_CACHE))
        except FileNotFoundError:
            return {}, {}
        try:
            cache = json.loads(raw)
            if cache.get('version') != 1 or not isinstance(cache.get('books'), list) or not isinstance(cache.get('stats'), dict):
                raise ValueError('unsupported cache structure')
            books = {}
            for item in cache['books']:
                relative = item.get('lpath')
                if isinstance(relative, str):
                    self._book_path(posixpath.join(self._root, relative))
                    books[relative] = item
            return books, cache['stats']
        except (ValueError, TypeError, AttributeError) as error:
            print(f'Kindle SFTP: rebuilding invalid metadata cache: {error}')
            return {}, {}

    def books(self, oncard=None, end_session=True):
        with self._lock:
            result = BookList(oncard, self._root, self.settings)
            if oncard is not None:
                return result
            self._files = self._inventory()
            cached, stamps = self._read_cache()
            codec = JsonCodec()
            records = sorted(self._files.items())
            for index, (path, record) in enumerate(records):
                relative = self._relative_path(path)
                stamp = [record['size'], record['mtime']]
                book = None
                if relative in cached and stamps.get(relative) == stamp:
                    book = codec.raw_to_book(cached[relative], RemoteBook, self._root)
                if book is None:
                    book = RemoteBook(self._root, relative, size=record['size'])
                    book.title = posixpath.splitext(posixpath.basename(relative))[0]
                    book.authors = ['Unknown']
                    if self._options['read_metadata']:
                        from calibre.ebooks.metadata.meta import get_metadata
                        from calibre.ptempfile import TemporaryFile
                        extension = posixpath.splitext(relative)[1].lstrip('.').lower()
                        with TemporaryFile(suffix='.' + extension) as filename:
                            with open(filename, 'wb') as output:
                                self._connection().download(path, output)
                            try:
                                with open(filename, 'rb') as source:
                                    metadata = get_metadata(source, extension)
                                book.smart_update(metadata)
                            except Exception as error:
                                print(f'Kindle SFTP: could not read embedded metadata for {relative!r}: {error}')
                book.lpath = relative
                book.path = path
                book.size = record['size']
                book.datetime = time.gmtime(record['mtime'])
                result.add_book(book, replace_metadata=False)
                self.report_progress((index + 1) / max(1, len(records)), 'Reading Kindle book metadata…')
            self._booklist = result
            self.sync_booklists((result, None, None))
            self.report_progress(1.0, 'Kindle books loaded')
            return result

    def _upload_path(self, filename, metadata):
        from calibre.devices.utils import create_upload_path
        from calibre.utils.filenames import ascii_filename
        path = create_upload_path(
            metadata, filename, self._options['save_template'], ascii_filename,
            prefix_path=self._root, path_type=posixpath, maxlen=240,
            use_subdirs=True, news_in_folder=True,
        )
        return self._book_path(path)

    def _thumbnail_filename(self, local_path):
        try:
            from calibre.devices.kindle.driver import thumbnail_filename
            with open(local_path, 'rb') as stream:
                name = thumbnail_filename(stream)
                return name if name else None
        except Exception:
            return None

    def _thumbnail_bytes(self, info):
        if info is None:
            return None
        thumb = getattr(info, 'thumbnail', None)
        if thumb:
            if isinstance(thumb, (tuple, list)) and len(thumb) >= 3 and thumb[2]:
                return bytes(thumb[2])
            if isinstance(thumb, (bytes, bytearray, memoryview)):
                return bytes(thumb)

        raw_cover = None
        cover_data = getattr(info, 'cover_data', None)
        if cover_data:
            if isinstance(cover_data, (tuple, list)) and len(cover_data) >= 2 and cover_data[1]:
                raw_cover = bytes(cover_data[1])
            elif isinstance(cover_data, (bytes, bytearray, memoryview)):
                raw_cover = bytes(cover_data)

        if not raw_cover:
            cover_path = getattr(info, 'cover', None)
            if isinstance(cover_path, str) and os.path.isfile(cover_path):
                try:
                    with open(cover_path, 'rb') as stream:
                        raw_cover = stream.read()
                except OSError:
                    pass

        if raw_cover:
            try:
                from calibre.utils.img import scale_image
                ht = getattr(self, 'THUMBNAIL_HEIGHT', 500)
                scaled = scale_image(raw_cover, height=ht, as_png=False)
                if scaled and len(scaled) >= 3 and scaled[2]:
                    return bytes(scaled[2])
            except Exception:
                return raw_cover

        return None

    def _upload_thumbnail(self, local_path, info):
        try:
            tfname = self._thumbnail_filename(local_path)
            if not tfname:
                return
            thumb_data = self._thumbnail_bytes(info)
            if not thumb_data:
                return
            self._connection().upload_thumbnail(tfname, thumb_data)
        except Exception as error:
            print(f'Kindle SFTP: thumbnail upload failed: {error}')

    def upload_books(self, files, names, on_card=None, end_session=True, metadata=None):
        with self._lock:
            if on_card is not None:
                raise ValueError('Kindle SFTP supports main memory only, not storage cards.')
            files, names = list(files), list(names)
            if len(files) != len(names) or (metadata is not None and len(metadata) != len(files)):
                raise ValueError('Files, names, and metadata must have matching lengths.')
            information = list(metadata) if metadata is not None else [None] * len(files)
            planned = []
            for source, name, info in zip(files, names, information):
                if info is None:
                    info = Metadata(posixpath.splitext(posixpath.basename(name))[0], ['Unknown'])
                path = self._upload_path(name, info)
                size = os.path.getsize(source)
                planned.append((source, path, size, info))
            if len({path for _, path, _, _ in planned}) != len(planned):
                raise ValueError('The filename template maps multiple books to the same path. Include {id} or {uuid}.')
            locations = []
            try:
                for index, (source, path, size, info) in enumerate(planned):
                    _, free = self._connection().space()
                    if size > free:
                        raise FreeSpaceError('Not enough free space in Kindle memory for an atomic book transfer.')
                    self.report_progress(index / max(1, len(planned)), 'Transferring books to Kindle…')
                    self._connection().upload(source, path)
                    self._upload_thumbnail(source, info)
                    locations.append((path, size))
                    self.report_progress((index + 1) / max(1, len(planned)), 'Transferring books to Kindle…')
            finally:
                if locations:
                    self._refresh_library(paths=[p for p, _ in locations])
            self._files = self._inventory()
            return locations

    def add_books_to_metadata(self, locations, metadata, booklists):
        # Calibre calls this outside the device thread. Update memory only.
        with self._lock:
            main = booklists[0]
            for (path, size), info in zip(locations, metadata):
                relative = self._relative_path(path)
                book = RemoteBook(self._root, relative, size=size, other=info)
                book.size = size
                added = main.add_book(book, replace_metadata=True)
                if added is not None:
                    added.size = size
                    added._new_book = True

    def _refresh_library(self, paths=None):
        self.report_progress(-1, 'Requesting native Kindle library refresh…')
        try:
            requested = self._connection().refresh_library(paths=paths)
        except Exception as error:
            raise RuntimeError(
                'Book files changed on the Kindle, but its native library refresh failed. '
                'Check that the stock framework/scanner is running and that the SSH user can set '
                f'com.lab126.scanner properties. Original error: {error}'
            ) from error
        if not requested:
            self.report_progress(
                -1, 'KOReader is active; native library refresh skipped. '
                'Return to the Kindle library and request a full refresh from the toolbar menu.',
            )

    def refresh_library(self):
        """Request a manual full scan on Calibre's device job thread."""
        with self._lock:
            return self._connection().refresh_library()

    def delete_books(self, paths, end_session=True):
        with self._lock:
            paths = [self._book_path(path) for path in paths]
            changed = False
            try:
                for index, path in enumerate(paths):
                    try:
                        self._connection().remove(path)
                        changed = True
                    except FileNotFoundError:
                        pass
                    self._files.pop(path, None)
                    self.report_progress((index + 1) / max(1, len(paths)), 'Deleting Kindle books…')
            finally:
                if changed:
                    self._refresh_library()

    @classmethod
    def remove_books_from_metadata(cls, paths, booklists):
        paths = set(paths)
        for booklist in booklists:
            if booklist is not None:
                for book in tuple(booklist):
                    if book.path in paths:
                        booklist.remove_book(book)

    def sync_booklists(self, booklists, end_session=True):
        with self._lock:
            if not booklists or booklists[0] is None:
                return
            codec = JsonCodec()
            stats = {
                self._relative_path(path): [record['size'], record['mtime']]
                for path, record in self._files.items()
            }
            payload = {
                'version': 1,
                'books': codec.encode_booklist_metadata(booklists[0]),
                'stats': stats,
            }
            self._connection().write_bytes_atomic(
                posixpath.join(self._root, self.METADATA_CACHE),
                json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            )
            for book in booklists[0]:
                book._new_book = False

    def get_file(self, path, outfile, end_session=True):
        with self._lock:
            self._connection().download(self._book_path(path), outfile)

    def prepare_addable_books(self, paths):
        from calibre.ptempfile import PersistentTemporaryFile
        result = []
        for path in paths:
            local = None
            try:
                local = PersistentTemporaryFile(suffix=posixpath.splitext(path)[1])
                self.get_file(path, local)
                local.close()
                result.append(local.name)
            except Exception as error:
                if local is not None:
                    local.close()
                    try:
                        os.unlink(local.name)
                    except OSError:
                        pass
                result.append((path, error, traceback.format_exc()))
        return result
