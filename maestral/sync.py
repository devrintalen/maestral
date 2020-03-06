# -*- coding: utf-8 -*-
"""
@author: Sam Schott  (ss2151@cam.ac.uk)

(c) Sam Schott; This work is licensed under a Creative Commons
Attribution-NonCommercial-NoDerivs 2.0 UK: England & Wales License.

"""
# system imports
import os
import os.path as osp
from stat import S_ISDIR
import shutil
import logging
import time
from threading import Thread, Timer, Event, Lock, RLock
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue, Empty
from collections import abc, OrderedDict
import functools
from enum import IntEnum

# external imports
import pathspec
import umsgpack
import dropbox
from dropbox.files import Metadata, DeletedMetadata, FileMetadata, FolderMetadata
from watchdog.events import FileSystemEventHandler
from watchdog.events import (EVENT_TYPE_CREATED, EVENT_TYPE_DELETED,
                             EVENT_TYPE_MODIFIED, EVENT_TYPE_MOVED)
from watchdog.events import (DirModifiedEvent, FileModifiedEvent, DirCreatedEvent,
                             FileCreatedEvent, DirDeletedEvent, FileDeletedEvent,
                             DirMovedEvent, FileMovedEvent)
from watchdog.utils.dirsnapshot import DirectorySnapshot
from atomicwrites import atomic_write

# local imports
from maestral.config import MaestralConfig, MaestralState
from maestral.watchdog import Observer
from maestral.constants import (IDLE, SYNCING, PAUSED, STOPPED, DISCONNECTED,
                                EXCLUDED_FILE_NAMES, MIGNORE_FILE, IS_FS_CASE_SENSITIVE)
from maestral.errors import (MaestralApiError, RevFileError, DropboxDeletedError,
                             DropboxAuthError, SyncError, ExcludedItemError,
                             PathError, InotifyError, NotFoundError)
from maestral.utils.content_hasher import DropboxContentHasher
from maestral.utils.notify import MaestralDesktopNotifier, FILECHANGE
from maestral.utils.path import is_child, path_exists_case_insensitive, delete
from maestral.utils.appdirs import get_data_path

logger = logging.getLogger(__name__)


# TODO:
#  * periodic resync: 
#  * fix excluding local file events: check if delay of 1 sec really is required, consider
#    lower memory usage alternatives (parallel downloads of binned deletions and folders?)
#


# ========================================================================================
# Syncing functionality
# ========================================================================================

class Conflict(IntEnum):
    RemoteNewer = 0
    Conflict = 1
    Identical = 2
    LocalNewerOrIdentical = 2


class TimedQueue(Queue):
    """
    A queue that remembers the time of the last put.

    :ivar update_time: Time of the last put.
    """

    __slots__ = ('update_time',)

    def __init__(self):
        super(self.__class__, self).__init__()
        self.update_time = 0.0

    # Put a new item in the queue, remember time
    def _put(self, item):
        self.queue.append(item)
        self.update_time = time.time()


class FileEventHandler(FileSystemEventHandler):
    """
    Handles captured file events and adds them to :ivar:`local_q` to be processed
    by :class:`upload_worker`. This acts as a translation layer between
    `watchdog.Observer` and :class:`upload_worker`.

    :param Event syncing: Set when syncing is running.
    :param Event startup: Set when startup is running.
    :param Queue local_file_event_queue: Queue with unprocessed local file events.
    :param Queue queue_downloading: Queue with files to be ignored. This is primarily used
         to exclude files and folders from monitoring if they are currently being
         downloaded. All entries in :ivar:`queue_downloading` should be temporary only.
    """

    def __init__(self, syncing, startup, local_file_event_queue, queue_downloading):

        self.syncing = syncing
        self.startup = startup
        self.local_file_event_queue = local_file_event_queue
        self.queue_downloading = queue_downloading

        self._renamed_items_cache = []

    def is_being_downloaded(self, local_path):
        """
        Checks if a file is currently being downloaded and should therefore not trigger
        any file events.

        :param str local_path: Local path to file.
        :returns: ``True`` if the file is currently being downloaded, ``False`` otherwise.
        :rtype: bool
        """

        with self.queue_downloading.mutex:
            local_path = local_path.lower()
            return any(local_path == p.lower() or is_child(local_path, p.lower())
                       for p in self.queue_downloading.queue)

    # TODO: The logic for ignoring moved events of children will no longer work when
    #   renaming the parent's moved event. This will throw sync errors when trying to
    #   apply those events, but they are only temporary and therefore tolerable for now.
    def rename_on_case_conflict(self, event):
        """
        Checks for other items in the same directory with same name but a different case.
        Only needed for case sensitive file systems.

        :param event: Created or moved event.
        :returns: Modified event if conflict detected and file has been renamed, original
            event otherwise.
        """

        if not (event.event_type is EVENT_TYPE_CREATED or event.event_type is
                EVENT_TYPE_MOVED):
            return event

        # get the created items path (src_path or dest_path)
        created_path = get_dest_path(event)

        # get all other items in the same directory
        try:
            parent_dir = osp.dirname(created_path)
            other_items = [osp.join(parent_dir, file) for file in os.listdir(parent_dir)]
            other_items.remove(created_path)
        except (FileNotFoundError, ValueError):
            # ValueError is raised when created_path is no longer in directory
            # FileNotFoundError is raised when directory no longer exists
            return event

        # check if we have any conflicting names with different cases
        if any(p.lower() == created_path.lower() for p in other_items):
            # try to find a unique new name of the form '(case conflict)'
            # or '(case conflict 1)'
            base, ext = osp.splitext(created_path)
            new_path = f'{base} (case conflict){ext}'
            i = 1
            while any(p.lower() == new_path.lower() for p in other_items):
                new_path = f'{base} (case conflict {i}){ext}'
                i += 1
            # rename newly created item
            self._renamed_items_cache.append(created_path)  # ignore temporarily
            shutil.move(created_path, new_path)  # this will be picked up by watchdog
            logger.debug('Case conflict: renamed "%s" to "%s"', created_path, new_path)

            if isinstance(event, DirCreatedEvent):
                return DirCreatedEvent(src_path=new_path)
            elif isinstance(event, FileCreatedEvent):
                return FileCreatedEvent(src_path=new_path)
            elif isinstance(event, DirMovedEvent):
                return DirMovedEvent(src_path=event.src_path, dest_path=new_path)
            elif isinstance(event, FileMovedEvent):
                return FileMovedEvent(src_path=event.src_path, dest_path=new_path)

        return event

    def on_any_event(self, event):
        """
        Checks if the system file event should be ignored for any reason. If not, adds it
        to the queue for events to upload.

        :param event: Watchdog file event.
        """

        if not (self.syncing.is_set() or self.startup.is_set()):
            # ignore events if we are not during startup or sync
            return

        # ignore files currently being downloaded
        if self.is_being_downloaded(event.src_path):
            return

        # rename target on case conflict
        if IS_FS_CASE_SENSITIVE:
            event = self.rename_on_case_conflict(event)

        # ignore files which have just been renamed
        if event.src_path in self._renamed_items_cache:
            self._renamed_items_cache.remove(event.src_path)
            return

        self.local_file_event_queue.put(event)


class InQueue:
    """
    A context manager that puts `name` into `custom_queue` when entering the context and
    removes it when exiting, after an optional delay.
    """
    def __init__(self, *names, queue=Queue(), delay=0):
        """
        :param str names: Items to put in queue.
        :param queue: Instance of :class:`Queue`.
        :param float delay: Delay before removing item from queue. Defaults to 0.
        """
        self.names = names
        self.queue = queue
        self._delay = delay

    def __enter__(self):
        for name in self.names:
            self.queue.put(name)

    def __exit__(self, err_type, err_value, err_traceback):
        self._timer = Timer(
            self._delay,
            remove_from_queue,
            args=(self.queue, *self.names)
        )
        self._timer.start()


class SyncStateSet(abc.MutableSet):
    """
    A wrapper for a list in the saved state that implements a MutableSet interface. All
    given paths are stored in lower-case, reflecting Dropbox's insensitive file system.

    :param str config_name: Name of config.
    :param str section: Section name in state.
    :param str option: Option name in state.
    """

    _lock = RLock()

    def __init__(self, config_name, section, option):
        super().__init__()
        self.config_name = config_name
        self.section = section
        self.option = option
        self._state = MaestralState(config_name)

    def __iter__(self):
        with self._lock:
            return iter(self._state.get(self.section, self.option))

    def __contains__(self, dbx_path):
        with self._lock:
            return dbx_path in self._state.get(self.section, self.option)

    def __len__(self):
        with self._lock:
            return len(self._state.get(self.section, self.option))

    def discard(self, dbx_path):
        dbx_path = dbx_path.lower().rstrip(osp.sep)
        with self._lock:
            state_list = self._state.get(self.section, self.option)
            state_list = set(state_list)
            state_list.discard(dbx_path)
            self._state.set(self.section, self.option, list(state_list))

    def add(self, dbx_path):
        dbx_path = dbx_path.lower().rstrip(osp.sep)
        with self._lock:
            state_list = self._state.get(self.section, self.option)
            state_list = set(state_list)
            state_list.add(dbx_path)
            self._state.set(self.section, self.option, list(state_list))

    def clear(self):
        with self._lock:
            self._state.set(self.section, self.option, [])

    def __repr__(self):
        return f'<{self.__class__.__name__}(section=\'{self.section}\',' \
               f'option=\'{self.option}\', entries={list(self)})>'


def catch_sync_issues(func):
    """
    Decorator that catches all SyncErrors and logs them.
    """

    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            res = func(self, *args, **kwargs)
            if res is None:
                res = True
        except SyncError as exc:
            file_name = os.path.basename(exc.dbx_path)
            logger.warning('Could not sync %s', file_name, exc_info=True)
            if exc.dbx_path is not None:
                if exc.local_path is None:
                    exc.local_path = self.to_local_path(exc.dbx_path)
                self.sync_errors.put(exc)
                if any(isinstance(a, Metadata) for a in args):
                    self.download_errors.add(exc.dbx_path)

            res = False

        return res

    return wrapper


class UpDownSync:
    """
    Class that contains methods to sync local file events with Dropbox and vice versa.

    :param client: MaestralApiClient client instance.
    :param local_file_event_queue: Queue with local file-changed events.
    :param queue_uploading: Queue with files currently being uploaded.
    :param queue_downloading: Queue with files currently being downloaded.
    """

    lock = Lock()

    _rev_lock = RLock()
    _last_sync_lock = RLock()

    _max_history = 30

    def __init__(self, client, local_file_event_queue, queue_uploading, queue_downloading):

        self.client = client
        self.config_name = self.client.config_name
        self.rev_file_path = get_data_path('maestral', f'{self.config_name}.index')

        self._conf = MaestralConfig(self.config_name)
        self._state = MaestralState(self.config_name)
        self.notifier = MaestralDesktopNotifier.for_config(self.config_name)

        self.sync_errors = Queue()

        self.local_file_event_queue = local_file_event_queue
        self.queued_newly_included_downloads = Queue()

        self.download_errors = SyncStateSet(self.config_name, 'sync', 'download_errors')
        self.pending_downloads = SyncStateSet(self.config_name, 'sync', 'pending_downloads')

        self.queued_for_download = Queue()
        self.queued_for_upload = Queue()
        self.queue_uploading = queue_uploading
        self.queue_downloading = queue_downloading

        # load cached properties
        self._dropbox_path = self._conf.get('main', 'path')
        self._mignore_path = osp.join(self._dropbox_path, MIGNORE_FILE)
        self._excluded_items = self._conf.get('main', 'excluded_items')
        self._rev_dict_cache = self._load_rev_dict_from_file()
        self._mignore_rules = self._load_mignore_rules_form_file()
        self._last_sync_for_path = dict()

    # ==== settings ======================================================================

    @property
    def dropbox_path(self):
        """Path to local Dropbox folder, as loaded from the config file. Before changing
        :ivar`dropbox_path`, make sure that all syncing is paused. Make sure to move
        the local Dropbox directory before resuming the sync. Changes are saved to the
        config file."""
        return self._dropbox_path

    @dropbox_path.setter
    def dropbox_path(self, path):
        """Setter: dropbox_path"""
        self._dropbox_path = path
        self._mignore_path = osp.join(self._dropbox_path, MIGNORE_FILE)
        self._conf.set('main', 'path', path)

    @property
    def excluded_items(self):
        """List containing all folders excluded from sync. Changes are saved to the
        config file. If a parent folder is excluded, its children will automatically be
        removed from the list. If only children are given but not the parent folder,
        any new items added to the parent will be synced."""
        return self._excluded_items

    @excluded_items.setter
    def excluded_items(self, folder_list):
        """Setter: excluded_items"""
        clean_list = self.clean_excluded_items_list(folder_list)
        self._excluded_items = clean_list
        self._conf.set('main', 'excluded_items', clean_list)

    # ==== sync state ====================================================================

    @property
    def last_cursor(self):
        """Cursor from last sync with remote Dropbox. The value is updated and saved to
        the config file on every download of remote changes."""
        return self._state.get('sync', 'cursor')

    @last_cursor.setter
    def last_cursor(self, cursor):
        """Setter: last_cursor"""
        self._state.set('sync', 'cursor', cursor)
        logger.debug('Remote cursor saved: %s', cursor)

    @property
    def last_sync(self):
        """Time stamp from last sync with remote Dropbox. The value is updated and
        saved to the config file on every successful upload of local changes."""
        return self._state.get('sync', 'lastsync')

    @last_sync.setter
    def last_sync(self, last_sync):
        """Setter: last_cursor"""
        logger.debug('Local cursor saved: %s', last_sync)
        self._state.set('sync', 'lastsync', last_sync)

    def get_last_sync_for_path(self, dbx_path):
        with self._last_sync_lock:
            dbx_path = dbx_path.lower()
            return max(self._last_sync_for_path.get(dbx_path, 0.0), self.last_sync)

    def set_last_sync_for_path(self, dbx_path, last_sync):
        with self._last_sync_lock:
            dbx_path = dbx_path.lower()
            if last_sync == 0.0:
                try:
                    del self._last_sync_for_path[dbx_path]
                except KeyError:
                    pass
            else:
                self._last_sync_for_path[dbx_path] = last_sync

    # ==== rev file management ===========================================================

    def _load_rev_dict_from_file(self, raise_exception=False):
        """
        Loads Maestral's rev index from `rev_file_path` using u-msgpack.

        :param bool raise_exception: If ``True``, raises an exception when loading fails.
            If ``False``, an error message is logged instead.
        :raises: RevFileError
        """
        rev_dict_cache = dict()
        new_exc = None

        with self._rev_lock:
            try:
                with open(self.rev_file_path, 'rb') as f:
                    rev_dict_cache = umsgpack.unpack(f)
                assert isinstance(rev_dict_cache, dict)
                assert all(isinstance(key, str) for key in rev_dict_cache.keys())
                assert all(isinstance(val, str) for val in rev_dict_cache.values())
            except (FileNotFoundError, IsADirectoryError):
                logger.info('Maestral index could not be found')
            except (AssertionError, umsgpack.InsufficientDataException) as exc:
                title = 'Corrupted index'
                msg = 'Maestral index has become corrupted. Please rebuild.'
                new_exc = RevFileError(title, msg).with_traceback(exc.__traceback__)
            except PermissionError as exc:
                title = 'Could not load index'
                msg = ('Insufficient permissions for Dropbox folder. Please '
                       'make sure that you have read and write permissions.')
                new_exc = RevFileError(title, msg).with_traceback(exc.__traceback__)
            except OSError as exc:
                title = 'Could not load index'
                msg = 'Please resync your Dropbox to rebuild the index.'
                new_exc = RevFileError(title, msg).with_traceback(exc.__traceback__)

            if new_exc and raise_exception:
                raise new_exc
            elif new_exc:
                exc_info = (type(new_exc), new_exc, new_exc.__traceback__)
                logger.error(title, exc_info=exc_info)

            return rev_dict_cache

    def _save_rev_dict_to_file(self, raise_exception=False):
        """
        Save Maestral's rev index to `rev_file_path` using u-msgpack.

        :param bool raise_exception: If ``True``, raises an exception when saving fails.
            If ``False``, an error message is logged instead.
        :raises: RevFileError
        """
        new_exc = None

        with self._rev_lock:
            try:
                with atomic_write(self.rev_file_path, mode='wb', overwrite=True) as f:
                    umsgpack.pack(self._rev_dict_cache, f)
            except PermissionError as exc:
                title = 'Could not save index'
                msg = ('Insufficient permissions for Dropbox folder. Please '
                       'make sure that you have read and write permissions.')
                new_exc = RevFileError(title, msg).with_traceback(exc.__traceback__)
            except OSError as exc:
                title = 'Could not save index'
                msg = 'Please check the logs for more information'
                new_exc = RevFileError(title, msg).with_traceback(exc.__traceback__)

            if new_exc and raise_exception:
                raise new_exc
            elif new_exc:
                exc_info = (type(new_exc), new_exc, new_exc.__traceback__)
                logger.error(title, exc_info=exc_info)

    def get_rev_index(self):
        """
        Returns a copy of the revision index containing the revision
        numbers for all synced files and folders.

        :returns: Copy of revision index.
        :rtype: dict
        """
        with self._rev_lock:
            return dict(self._rev_dict_cache)

    def get_local_rev(self, dbx_path):
        """
        Gets revision number of local file.

        :param str dbx_path: Dropbox file path.
        :returns: Revision number as str or `None` if no local revision number
            has been saved.
        :rtype: str
        """
        with self._rev_lock:
            dbx_path = dbx_path.lower()
            rev = self._rev_dict_cache.get(dbx_path, None)

            return rev

    def set_local_rev(self, dbx_path, rev):
        """
        Saves revision number `rev` for local file. If `rev` is `None`, the
        entry for the file is removed.

        :param str dbx_path: Relative Dropbox file path.
        :param rev: Revision number as string or `None`.
        """
        with self._rev_lock:
            dbx_path = dbx_path.lower()

            if rev == self._rev_dict_cache.get(dbx_path, None):
                # rev is already set, nothing to do
                return

            if rev is None:
                # remove entry and all its children revs
                for path in dict(self._rev_dict_cache):
                    if path.startswith(dbx_path):
                        self._rev_dict_cache.pop(path, None)
            else:
                # add entry
                self._rev_dict_cache[dbx_path] = rev
                # set all parent revs to 'folder'
                dirname = osp.dirname(dbx_path)
                while dirname != '/':
                    self._rev_dict_cache[dirname] = 'folder'
                    dirname = osp.dirname(dirname)

            # save changes to file
            self._save_rev_dict_to_file()

    def clear_rev_index(self):
        with self._rev_lock:
            self._rev_dict_cache.clear()
            self._save_rev_dict_to_file()

    # ==== mignore management ============================================================

    @property
    def mignore_path(self):
        return self._mignore_path

    @property
    def mignore_rules(self):
        if get_ctime(self.mignore_path) != self._mignore_ctime_loaded:
            self._mignore_rules = self._load_mignore_rules_form_file()
        return self._mignore_rules

    def _load_mignore_rules_form_file(self):
        self._mignore_ctime_loaded = get_ctime(self.mignore_path)
        try:
            with open(self.mignore_path, 'r') as f:
                spec = f.read()
        except FileNotFoundError:
            spec = ''
        spec = spec.lower()  # convert all patterns to lower case
        return pathspec.PathSpec.from_lines('gitwildmatch', spec.splitlines())

    # ==== helper functions ==============================================================

    @staticmethod
    def clean_excluded_items_list(folder_list):
        """Removes all duplicates from the excluded folder list."""

        # remove duplicate entries by creating set, strip trailing '/'
        folder_list = set(f.lower().rstrip(osp.sep) for f in folder_list)

        # remove all children of excluded folders
        clean_list = list(folder_list)
        for folder in folder_list:
            clean_list = [f for f in clean_list if not is_child(f, folder)]

        return clean_list

    def ensure_dropbox_folder_present(self):
        """
        Checks if the Dropbox folder still exists where we expect it to be.

        :raises: DropboxDeletedError
        """

        if not osp.isdir(self.dropbox_path):
            title = 'Dropbox folder has been moved or deleted'
            msg = ('Please move the Dropbox folder back to its original location '
                   'or restart Maestral to set up a new folder.')
            raise DropboxDeletedError(title, msg)

    def to_dbx_path(self, local_path):
        """
        Converts a local path to a path relative to the Dropbox folder. Casing of the
        given ``local_path`` will be preserved.

        :param str local_path: Full path to file in local Dropbox folder.
        :returns: Relative path with respect to Dropbox folder.
        :rtype: str
        :raises: ValueError if no path is specified or path is outside of local
            Dropbox folder.
        """

        if not local_path:
            raise ValueError('No path specified')

        dbx_root_list = osp.normpath(self.dropbox_path).split(osp.sep)
        path_list = osp.normpath(local_path).split(osp.sep)

        # Work out how much of the file path is shared by dropbox_path and path.
        # noinspection PyTypeChecker
        i = len(osp.commonprefix([dbx_root_list, path_list]))

        if i == len(path_list):  # path corresponds to dropbox_path
            return '/'
        elif i != len(dbx_root_list):  # path is outside of to dropbox_path
            raise ValueError(f'Specified path "{local_path}" is outside of Dropbox '
                             f'directory "{self.dropbox_path}"')

        return '/{}'.format('/'.join(path_list[i:]))

    def to_local_path(self, dbx_path):
        """
        Converts a Dropbox path to the corresponding local path.

        The ``path_display`` attribute returned by the Dropbox API only guarantees correct
        casing of the basename (file name or folder name) and not of the full path. This
        is because Dropbox itself is not case sensitive and stores all paths in lowercase
        internally. To the extend where parent directories of ``dbx_path`` are already
        present on the local drive, their casing is used. Otherwise, the casing from
        ``dbx_path`` is used. This aims to preserve the correct casing of file and folder
        names and prevents the creation of duplicate folders with different casing on the
        local drive.

        :param str dbx_path: Path to file relative to Dropbox folder.
        :returns: Corresponding local path on drive.
        :rtype: str
        :raises: ValueError if no path is specified.
        """

        dbx_path = dbx_path.replace('/', osp.sep)
        dbx_path_parent, dbx_path_basename = osp.split(dbx_path)

        local_parent = path_exists_case_insensitive(dbx_path_parent, self.dropbox_path)

        if local_parent == '':
            return osp.join(self.dropbox_path, dbx_path.lstrip(osp.sep))
        else:
            return osp.join(local_parent, dbx_path_basename)

    def has_sync_errors(self):
        """Returns ``True`` in case of sync errors, ``False`` otherwise."""
        return self.sync_errors.qsize() > 0

    def clear_sync_error(self, local_path=None, dbx_path=None):
        """
        Clears all sync errors related to the item defined by :param:`local_path`
        or :param:`local_path.

        :param str local_path: Path to local file.
        :param str dbx_path: Path to file on Dropbox.
        """
        assert local_path or dbx_path
        if not dbx_path:
            dbx_path = self.to_dbx_path(local_path)

        if self.has_sync_errors():
            for error in list(self.sync_errors.queue):
                if error.dbx_path.lower() == dbx_path.lower():
                    remove_from_queue(self.sync_errors, error)

        self.download_errors.discard(dbx_path)

    def clear_all_sync_errors(self):
        """Clears all sync errors."""
        with self.sync_errors.mutex:
            self.sync_errors.queue.clear()
        self.download_errors.clear()

    @staticmethod
    def is_excluded(dbx_path):
        """
        Check if file is excluded from sync.

        :param str dbx_path: Path of folder on Dropbox.
        :returns: ``True`` or `False`.
        :rtype: bool
        """
        dbx_path = dbx_path.lower()

        # is root folder?
        if dbx_path in ['/', '']:
            return True

        # information about excluded files:
        # https://help.dropbox.com/installs-integrations/sync-uploads/files-not-syncing

        basename = osp.basename(dbx_path)

        # in excluded files?
        test0 = basename in EXCLUDED_FILE_NAMES

        # is temporary file?
        # 1) office temporary files
        test1 = basename.startswith('~$')
        test2 = basename.startswith('.~')
        # 2) other temporary files
        test3 = basename.startswith('~') and basename.endswith('.tmp')

        return any((test0, test1, test2, test3))

    def is_excluded_by_user(self, dbx_path):
        """
        Check if file has been excluded from sync by the user.

        :param str dbx_path: Path of folder on Dropbox.
        :returns: ``True`` or `False`.
        :rtype: bool
        """
        dbx_path = dbx_path.lower()

        return any(dbx_path == f or is_child(dbx_path, f) for f in self.excluded_items)

    def is_mignore(self, event):
        """
        Check if local file change has been excluded by an mignore pattern.

        :param event: Local file event.
        :returns: ``True`` or ``False``.
        :rtype: bool
        :raises: ValueError if given a FileMovedEvent or DirMovedEvent
        """
        if len(self.mignore_rules.patterns) == 0:
            return False

        if event.event_type == EVENT_TYPE_MOVED:
            raise ValueError('Cannot check moved events,'
                             'split into created and deleted events first')

        dbx_path = self.to_dbx_path(event.src_path)

        return (self._is_mignore_path(dbx_path, is_dir=event.is_directory)
                and not self.get_local_rev(dbx_path))

    def _should_split_excluded(self, event):

        if event.event_type != EVENT_TYPE_MOVED:
            raise ValueError('Can only split moved events')

        if (self.is_excluded(event.src_path)
                or self.is_excluded(event.dest_path)
                or self.is_excluded_by_user(event.src_path)
                or self.is_excluded_by_user(event.dest_path)):
            return True
        else:
            return self._should_split_mignore(event)

    def _should_split_mignore(self, event):
        if len(self.mignore_rules.patterns) == 0:
            return False

        dbx_src_path = self.to_dbx_path(event.src_path)
        dbx_dest_path = self.to_dbx_path(event.dest_path)

        return (self._is_mignore_path(dbx_src_path, event.is_directory)
                or self._is_mignore_path(dbx_dest_path, event.is_directory))

    def _is_mignore_path(self, dbx_path, is_dir=False):

        relative_path = dbx_path.lstrip('/')

        if is_dir:
            relative_path += '/'

        return self.mignore_rules.match_file(relative_path)

    # ==== Upload sync ===================================================================

    def upload_local_changes_while_inactive(self):
        """
        Collects changes while sync has not been running and uploads them to Dropbox.
        Call this method when resuming sync.
        """

        logger.info('Indexing local changes...')

        try:
            events, local_cursor = self._get_local_changes_while_inactive()
            logger.debug('Retrieved local changes:\n%s', iter_to_str(events))
        except FileNotFoundError:
            self.ensure_dropbox_folder_present()
            return

        if len(events) > 0:
            self.apply_local_changes(events, local_cursor)
            logger.debug('Uploaded local changes while inactive')
        else:
            self.last_sync = local_cursor
            logger.debug('No local changes while inactive')

    def _get_local_changes_while_inactive(self):

        changes = []
        now = time.time()
        snapshot = DirectorySnapshot(self.dropbox_path)

        # remove root entry from snapshot
        del snapshot._inode_to_path[snapshot.inode(self.dropbox_path)]
        del snapshot._stat_info[self.dropbox_path]
        # get lowercase paths
        lowercase_snapshot_paths = {x.lower() for x in snapshot.paths}

        # get modified or added items
        for path in snapshot.paths:
            stats = snapshot.stat_info(path)
            # check if item was created or modified since last sync
            # but before we started the FileEventHandler (~now)
            dbx_path = self.to_dbx_path(path).lower()
            ctime_check = now > stats.st_ctime > self.get_last_sync_for_path(dbx_path)

            # always upload untracked items, check ctime of tracked items
            rev = self.get_local_rev(dbx_path)
            is_new = not rev
            is_modified = rev and ctime_check

            if is_new:
                if snapshot.isdir(path):
                    event = DirCreatedEvent(path)
                else:
                    event = FileCreatedEvent(path)
                changes.append(event)
            elif is_modified:
                if snapshot.isdir(path) and rev == 'folder':
                    event = DirModifiedEvent(path)
                    changes.append(event)
                elif not snapshot.isdir(path) and rev != 'folder':
                    event = FileModifiedEvent(path)
                    changes.append(event)
                elif snapshot.isdir(path):
                    event0 = FileDeletedEvent(path)
                    event1 = DirCreatedEvent(path)
                    changes += [event0, event1]
                elif not snapshot.isdir(path):
                    event0 = DirDeletedEvent(path)
                    event1 = FileCreatedEvent(path)
                    changes += [event0, event1]

        # get deleted items
        rev_dict_copy = self.get_rev_index()
        for p in rev_dict_copy:
            # warning: local_path may not be correctly cased
            local_path = self.to_local_path(p)
            if local_path.lower() not in lowercase_snapshot_paths:
                if rev_dict_copy[p] == 'folder':
                    event = DirDeletedEvent(local_path)
                else:
                    event = FileDeletedEvent(local_path)
                changes.append(event)

        del snapshot
        del lowercase_snapshot_paths

        return changes, now

    def wait_for_local_changes(self, timeout=2, delay=0.5):
        """
        Waits for local file changes. Returns a list of local changes, filtered to
        avoid duplicates.

        :param float timeout: If no changes are detected within timeout (sec), an empty
            list is returned.
        :param float delay: Delay in sec to wait for subsequent changes that may be
            duplicates.
        :returns: (list of file events, time_stamp)
        :rtype: (list, float)
        """
        self.ensure_dropbox_folder_present()
        try:
            events = [self.local_file_event_queue.get(timeout=timeout)]
        except Empty:
            return [], time.time()

        # keep collecting events until no more changes happen for at least `delay` sec
        t0 = time.time()
        has_more = True
        while has_more:
            try:
                events.append(self.local_file_event_queue.get(timeout=delay))
            except Empty:
                has_more = False
                t0 = time.time()

        # save timestamp
        local_cursor = t0

        logger.debug('Retrieved local file events:\n%s', iter_to_str(events))

        # clean up events to provide only one event per path
        events = self._clean_local_events(events)

        # REMOVE DIR_MODIFIED_EVENTS
        events = [e for e in events if not isinstance(e, DirModifiedEvent)]

        # COMBINE 'MOVED' EVENTS OF FOLDERS AND THEIR CHILDREN INTO ONE EVENT
        dir_moved_events = [e for e in events if self._is_dir_moved(e)]

        if len(dir_moved_events) > 0:
            child_move_events = []

            for parent_event in dir_moved_events:
                children = [x for x in events if self._is_moved_child(x, parent_event)]
                child_move_events += children

            events = self._list_diff(events, child_move_events)

        # COMBINE 'DELETED' EVENTS OF FOLDERS AND THEIR CHILDREN INTO ONE EVENT
        dir_deleted_events = [x for x in events if self._is_dir_deleted(x)]

        if len(dir_deleted_events) > 0:
            child_deleted_events = []

            for parent_event in dir_deleted_events:
                children = [x for x in events if self._is_deleted_child(x, parent_event)]
                child_deleted_events += children

            events = self._list_diff(events, child_deleted_events)

        logger.debug('Cleaned up local file events:\n%s', iter_to_str(events))

        return events, local_cursor

    def _filter_excluded_changes_local(self, events):
        """
        Checks for and removes file events referring to items which are excluded from
        syncing.

        :param events: List of file events.
        :returns: (``events_filtered``, ``events_excluded``)
        """

        logger.debug('Filtering excluded items from local file events')

        events_filtered = []
        events_excluded = []

        for event in events:

            local_path = get_dest_path(event)
            dbx_path = self.to_dbx_path(local_path)

            if self.is_excluded(dbx_path):  # is excluded?
                events_excluded.append(event)
            elif self.is_excluded_by_user(dbx_path):  # is excluded by selective sync?
                if event.event_type is EVENT_TYPE_DELETED:
                    self.clear_sync_error(local_path, dbx_path)
                else:
                    title = 'Could not upload'
                    message = ('An item with the same path already exists on '
                               'Dropbox but is excluded from syncing.')
                    exc = ExcludedItemError(title, message, dbx_path=dbx_path,
                                            local_path=local_path)
                    basename = osp.basename(dbx_path)
                    exc_info = (type(exc), exc, None)
                    logger.warning('Could not upload ', basename, exc_info=exc_info)
                    self.sync_errors.put(exc)
                events_excluded.append(event)
            elif self.is_mignore(event):  # is excluded by mignore?
                events_excluded.append(event)
            else:
                events_filtered.append(event)

        logger.debug('Events to keep:\n%s', iter_to_str(events_filtered))

        return events_filtered, events_excluded

    def _clean_local_events(self, events):
        """
        Takes local file events within the monitored period and cleans them up so that
        there is only a single event per path.

        :param events: Iterable of :class:`watchdog.FileSystemEvents`.
        :returns: List of :class:`watchdog.FileSystemEvents`.
        :rtype: list
        """

        all_src_paths = [e.src_path for e in events]
        all_dest_paths = [e.dest_path for e in events if e.event_type == EVENT_TYPE_MOVED]

        all_paths = all_src_paths + all_dest_paths

        # Move events are difficult to combine with other event types
        # -> split up moved events into deleted and created events if at least one
        # of the paths has other events associated with it or is excluded from sync
        new_events = []

        for e in events:
            if e.event_type == EVENT_TYPE_MOVED:
                related = tuple(p for p in all_paths if p in (e.src_path, e.dest_path))
                if len(related) > 2 or self._should_split_excluded(e):

                    if e.is_directory:
                        CreatedEvent = DirCreatedEvent
                        DeletedEvent = DirDeletedEvent
                    else:
                        CreatedEvent = FileCreatedEvent
                        DeletedEvent = FileDeletedEvent

                    new_events.append(DeletedEvent(e.src_path))
                    new_events.append(CreatedEvent(e.dest_path))
                else:
                    new_events.append(e)
            else:
                new_events.append(e)

        events = new_events.copy()

        unique_paths = set(e.src_path for e in events)
        histories = [[e for e in events if e.src_path == path] for path in unique_paths]

        new_events.clear()

        for h in histories:
            if len(h) == 1:
                new_events += h
            else:
                path = h[0].src_path

                n_created = len([e for e in h if e.event_type == EVENT_TYPE_CREATED])
                n_deleted = len([e for e in h if e.event_type == EVENT_TYPE_DELETED])

                if n_created > n_deleted:  # item was created
                    if h[-1].is_directory:
                        new_events.append(DirCreatedEvent(path))
                    else:
                        new_events.append(FileCreatedEvent(path))
                if n_created < n_deleted:  # item was deleted
                    if h[0].is_directory:
                        new_events.append(DirDeletedEvent(path))
                    else:
                        new_events.append(FileDeletedEvent(path))
                else:

                    first_created_idx = next(iter(i for i, e in enumerate(h) if e.event_type == EVENT_TYPE_CREATED), -1)
                    first_deleted_idx = next(iter(i for i, e in enumerate(h) if e.event_type == EVENT_TYPE_DELETED), -1)

                    if n_created == 0 or first_deleted_idx < first_created_idx:
                        # item was modified
                        if h[0].is_directory and h[-1].is_directory:
                            new_events.append(DirModifiedEvent(path))
                        elif not h[0].is_directory and not h[-1].is_directory:
                            new_events.append(FileModifiedEvent(path))
                        elif h[0].is_directory:
                            new_events.append(DirDeletedEvent(path))
                            new_events.append(FileCreatedEvent(path))
                        elif h[1].is_directory:
                            new_events.append(FileDeletedEvent(path))
                            new_events.append(DirCreatedEvent(path))
                    else:
                        # item was only temporary
                        pass

        return new_events

    @staticmethod
    def _separate_local_event_types(events):
        """
        Sorts local events into folder, files, deleted.

        :returns: Tuple of (folders, files, deleted)
        :rtype: tuple
        """

        folders = [e for e in events if e.is_directory
                   and e.event_type != EVENT_TYPE_DELETED]
        files = [e for e in events if not e.is_directory
                 and e.event_type != EVENT_TYPE_DELETED]
        deleted = [e for e in events if e.event_type == EVENT_TYPE_DELETED]

        return folders, files, deleted

    def apply_local_changes(self, events, local_cursor):
        """
        Applies locally detected events to remote Dropbox.

        :param list events: List of local file changes.
        :param float local_cursor: Time stamp of last event in `events`.
        """

        logger.debug('Beginning upload of local changes')

        events, _ = self._filter_excluded_changes_local(events)
        dir_events, file_events, deleted_events = self._separate_local_event_types(events)

        # sort events (might not be necessary)
        dir_events.sort(key=lambda x: x.src_path.count('/'))
        deleted_events.sort(key=lambda x: x.src_path.count('/'), reverse=True)

        # update queues
        for e in events:
            self.queued_for_upload.put(get_dest_path(e))

        # apply deleted events first, folder created events second
        # neither event type requires an actual upload
        for event in deleted_events:
            self._apply_event(event)

        for event in dir_events:
            self._apply_event(event)

        # apply file events in parallel
        num_threads = os.cpu_count() * 2
        success = []
        last_emit = time.time()
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            fs = (executor.submit(self._apply_event, e) for e in file_events)
            n_files = len(file_events)
            for f, n in zip(as_completed(fs), range(1, n_files + 1)):
                if time.time() - last_emit > 1 or n in (1, n_files):
                    # emit message at maximum every second
                    logger.info(f'Uploading {n}/{n_files}...')
                    last_emit = time.time()
                success.append(f.result())

        if all(success):
            self.last_sync = local_cursor  # save local cursor

    @staticmethod
    def _list_diff(list1, list2):
        """
        Subtracts elements of `list2` from `list1` while preserving the order of `list1`.

        :param list list1: List to subtract from.
        :param list list2: List of elements to subtract.
        :returns: Subtracted list.
        :rtype: list
        """
        return [item for item in list1 if item not in set(list2)]

    @staticmethod
    def _is_dir_moved(x):
        """Check for moved folders"""
        is_moved_event = (x.event_type is EVENT_TYPE_MOVED)
        return is_moved_event and x.is_directory

    @staticmethod
    def _is_moved_child(x, parent):
        """Check for children of moved folders"""
        is_moved_event = (x.event_type is EVENT_TYPE_MOVED)
        return (is_moved_event
                and is_child(x.src_path, parent.src_path)
                and is_child(x.dest_path, parent.dest_path))

    @staticmethod
    def _is_dir_deleted(x):
        """Check for deleted folders"""
        is_deleted_event = (x.event_type is EVENT_TYPE_DELETED)
        return is_deleted_event and x.is_directory

    @staticmethod
    def _is_deleted_child(x, parent):
        """Check for children of deleted folders"""
        is_deleted_event = (x.event_type is EVENT_TYPE_DELETED)
        return is_deleted_event and is_child(x.src_path, parent.src_path)

    @catch_sync_issues
    def _apply_event(self, event):
        """Apply a local file event `event` to the remote Dropbox. Clear any related
        sync errors with the file. Any new MaestralApiErrors will be caught by the
        decorator."""

        local_path_from = event.src_path
        local_path_to = get_dest_path(event)

        # book keeping
        remove_from_queue(self.queued_for_upload, local_path_to)
        self.clear_sync_error(local_path=local_path_to)
        if local_path_to != local_path_from:
            remove_from_queue(self.queued_for_upload, local_path_from)
            self.clear_sync_error(local_path=local_path_from)

        with InQueue(local_path_to, queue=self.queue_uploading):
            if event.event_type is EVENT_TYPE_CREATED:
                self._on_created(event)
            elif event.event_type is EVENT_TYPE_MOVED:
                with InQueue(local_path_from, queue=self.queue_uploading):
                    self._on_moved(event)
            elif event.event_type is EVENT_TYPE_MODIFIED:
                self._on_modified(event)
            elif event.event_type is EVENT_TYPE_DELETED:
                self._on_deleted(event)

    @staticmethod
    def _wait_for_creation(path):
        """
        Wait for a file at a path to be created or modified.
        :param str path: absolute path to file
        """
        try:
            while True:
                size1 = osp.getsize(path)
                time.sleep(0.5)
                size2 = osp.getsize(path)
                if size1 == size2:
                    return
        except OSError:
            return

    def _on_moved(self, event):
        """
        Call when local item is moved.

        Keep in mind that we may be moving a whole tree of items. But its better deal
        with the complexity than to delete and re-uploading everything. Thankfully, in
        case of directories, we always process the top-level first. Trying to move the
        children will then be delegated to `on_create` (because the old item no longer
        lives on Dropbox) and that won't upload anything because file contents have
        remained the same.

        :param event: Watchdog file event.
        :raises: MaestralApiError on failure.
        """

        local_path_from = event.src_path
        local_path_to = event.dest_path

        dbx_path_from = self.to_dbx_path(local_path_from)
        dbx_path_to = self.to_dbx_path(local_path_to)

        self.set_local_rev(dbx_path_from, None)

        md_from_old = self.client.get_metadata(dbx_path_from)

        # If not on Dropbox, e.g., because its old name was invalid,
        # create it instead of moving it.
        if not md_from_old:
            if isinstance(event, DirMovedEvent):
                new_event = DirCreatedEvent(local_path_to)
            else:
                new_event = FileCreatedEvent(local_path_to)
            return self._on_created(new_event)

        md_to_new = self.client.move(dbx_path_from, dbx_path_to, autorename=True)

        # handle conflicts
        if md_to_new.path_lower != dbx_path_to.lower():
            # created conflict => move local item and fetch original
            local_path_to_cc = self.to_local_path(md_to_new.path_display)
            with InQueue(local_path_to, local_path_to_cc,
                         queue=self.queue_downloading, delay=1.0):
                delete(local_path_to_cc)
                try:
                    shutil.move(local_path_to, local_path_to_cc)
                    self.set_local_rev(dbx_path_to, None)
                    self._set_local_rev_recursive(md_to_new)
                except FileNotFoundError:
                    self.set_local_rev(dbx_path_to, None)

            self.get_remote_item(dbx_path_to)

            logger.debug('Upload conflict "%s" handled by Dropbox, created "%s"',
                         dbx_path_to, md_to_new.path_display)

        else:
            self._set_local_rev_recursive(md_to_new)
            logger.debug('Moved "%s" to "%s" on Dropbox', dbx_path_from, dbx_path_to)

    def _set_local_rev_recursive(self, md):

        if isinstance(md, FileMetadata):
            self.set_local_rev(md.path_lower, md.rev)
        elif isinstance(md, FolderMetadata):
            self.set_local_rev(md.path_lower, 'folder')
            result = self.client.list_folder(md.path_lower, recursive=True)
            for md in result.entries:
                if isinstance(md, FileMetadata):
                    self.set_local_rev(md.path_lower, md.rev)
                elif isinstance(md, FolderMetadata):
                    self.set_local_rev(md.path_lower, 'folder')

    def _on_created(self, event):
        """
        Call when local item is created.

        :param class event: Watchdog file event.
        :raises: MaestralApiError on failure.
        """

        local_path = event.src_path
        dbx_path = self.to_dbx_path(local_path)

        md_old = self.client.get_metadata(dbx_path)
        self._wait_for_creation(local_path)

        if event.is_directory:
            if isinstance(md_old, FolderMetadata):
                self.set_local_rev(dbx_path, 'folder')
                return
            else:
                md_new = self.client.make_dir(dbx_path, autorename=True)

        else:
            # check if file already exists with identical content
            if isinstance(md_old, FileMetadata):
                local_hash = get_local_hash(local_path)
                if local_hash == md_old.content_hash:
                    # file hashes are identical, do not upload
                    self.set_local_rev(md_old.path_lower, md_old.rev)
                    return

            rev = self.get_local_rev(dbx_path)
            if not rev:  # truly a new file
                mode = dropbox.files.WriteMode('add')
            elif rev == 'folder':  # folder replaced by file
                mode = dropbox.files.WriteMode('overwrite')
            else:  # modified file
                mode = dropbox.files.WriteMode('update', rev)
            try:
                md_new = self.client.upload(local_path, dbx_path,
                                            autorename=True, mode=mode)
            except NotFoundError:
                logger.debug('Could not upload "%s": the item does not exist',
                             event.src_path)
                return

        if md_new.path_lower != dbx_path.lower() and md_old:
            # created conflict => move local item and fetch original
            local_path_cc = self.to_local_path(md_new.path_display)
            with InQueue(local_path, local_path_cc,
                         queue=self.queue_downloading, delay=1.0):
                delete(local_path_cc)
                try:
                    shutil.move(local_path, local_path_cc)
                    self.set_local_rev(md_old.path_lower, None)
                    self._set_local_rev_recursive(md_new)
                except FileNotFoundError:
                    self.set_local_rev(md_old.path_lower, None)

            self._create_local_entry(md_old)

            logger.debug('Upload conflict "%s" handled by Dropbox, created "%s"',
                         dbx_path, md_new.path_lower)
        else:
            rev = getattr(md_new, 'rev', 'folder')
            self.set_local_rev(md_new.path_lower, rev)
            logger.debug('Created "%s" on Dropbox', dbx_path)

    def _on_modified(self, event):
        """
        Call when local item is modified.

        :param class event: Watchdog file event.
        :raises: MaestralApiError on failure.
        """

        if not event.is_directory:  # ignore directory modified events

            local_path = event.src_path
            dbx_path = self.to_dbx_path(local_path)

            self._wait_for_creation(local_path)

            # check if item already exists with identical content
            md_old = self.client.get_metadata(dbx_path)
            if isinstance(md_old, FileMetadata):
                local_hash = get_local_hash(local_path)
                if local_hash == md_old.content_hash:
                    # file hashes are identical, do not upload
                    self.set_local_rev(md_old.path_lower, md_old.rev)
                    logger.debug('Modification of "%s" detected but file content is '
                                 'the same as on Dropbox', dbx_path)
                    return

            rev = self.get_local_rev(dbx_path)
            if rev == 'folder':
                mode = dropbox.files.WriteMode('overwrite')
            elif not rev:
                logger.debug('"%s" appears to have been modified but cannot '
                             'find old revision', dbx_path)
                mode = dropbox.files.WriteMode('add')
            else:
                mode = dropbox.files.WriteMode('update', rev)

            try:
                md_new = self.client.upload(local_path, dbx_path,
                                            autorename=True, mode=mode)
            except NotFoundError:
                logger.debug('Could not upload "%s": the item does not exist', dbx_path)
                return

            if md_new.path_lower != dbx_path.lower() and md_old:
                # created conflict => move local file and fetch original
                local_path_cc = self.to_local_path(md_new.path_display)
                with InQueue(local_path, local_path_cc,
                             queue=self.queue_downloading, delay=1.0):
                    delete(local_path_cc)
                    try:
                        shutil.move(local_path, local_path_cc)
                        self.set_local_rev(md_old.path_lower, None)
                        self.set_local_rev(md_new.path_lower, md_new.rev)
                    except FileNotFoundError:
                        self.set_local_rev(md_old.path_lower, None)

                self._create_local_entry(md_old)

                logger.debug('Upload conflict "%s" renamed to "%s" by Dropbox',
                             md_old.path_lower, md_new.path_lower)

            else:
                self.set_local_rev(md_new.path_lower, md_new.rev)
                logger.debug('Uploaded modified "%s" to Dropbox', md_new.path_lower)

    def _on_deleted(self, event):
        """
        Call when local item is deleted. We try not to delete remote items which have been
        modified since the last sync.

        :param class event: Watchdog file event.
        :raises: MaestralApiError on failure.
        """

        path = event.src_path
        dbx_path = self.to_dbx_path(path)
        local_rev = self.get_local_rev(dbx_path)
        is_file = local_rev != 'folder'
        is_folder = local_rev == 'folder'

        md = self.client.get_metadata(dbx_path, include_deleted=True)

        if is_folder and isinstance(md, FileMetadata):
            logger.debug('Expected folder at "%s" but found a file instead, checking '
                         'which one is newer', md.path_display)
            # don't delete a remote file if it was modified since last sync
            if md.server_modified.timestamp() >= self.get_last_sync_for_path(dbx_path):
                logger.debug('Skipping deletion: remote item "%s" has been modified '
                             'since last sync', md.path_display)
                self.set_local_rev(dbx_path, None)
                return

        if is_file and isinstance(md, FolderMetadata):
            # don't delete a remote folder if we were expecting a file
            # TODO: Delete the folder if its children did not change since last sync.
            #   Is there a way of achieving this without listing the folder or listing
            #   all changes and checking when they occurred?
            logger.debug('Skipping deletion: expected file at "%s" but found a '
                         'folder instead', md.path_display)
            self.set_local_rev(dbx_path, None)
            return

        try:
            # will only perform delete if Dropbox rev matches local_rev
            self.client.remove(dbx_path, parent_rev=local_rev if is_file else None)
        except NotFoundError:
            logger.debug('Could not delete "%s": the item no longer exists on Dropbox',
                         dbx_path)
        except PathError:
            logger.debug('Could not delete "%s": the item has been changed '
                         'since last sync', dbx_path)

        # remove revision metadata
        self.set_local_rev(dbx_path, None)

    # ==== Download sync =================================================================

    @catch_sync_issues
    def get_remote_folder(self, dbx_path='/', ignore_excluded=True):
        """
        Gets all files/folders from Dropbox and writes them to the local folder
        :ivar:`dropbox_path`. Call this method on first run of the Maestral. Indexing
        and downloading may take several minutes, depending on the size of the user's
        Dropbox folder.

        :param str dbx_path: Path to Dropbox folder. Defaults to root ('').
        :param bool ignore_excluded: If ``True``, do not index excluded folders.
        :returns: ``True`` on success, ``False`` otherwise.
        :rtype: bool
        """

        is_dbx_root = dbx_path in ('/', '')
        success = []

        if is_dbx_root:
            logger.info('Downloading your Dropbox')
        else:
            logger.info('Downloading %s', dbx_path)

        if not any(folder.startswith(dbx_path) for folder in self.excluded_items):
            # if there are no excluded subfolders, index and download all at once
            ignore_excluded = False

        # get a cursor for the folder
        cursor = self.client.get_latest_cursor(dbx_path)

        root_result = self.client.list_folder(dbx_path, recursive=(not ignore_excluded),
                                              include_deleted=False, limit=500)

        # download top-level folders / files first
        logger.info(SYNCING)
        _, s = self.apply_remote_changes(root_result, save_cursor=False)
        success.append(s)

        if ignore_excluded:
            # download sub-folders if not excluded
            for entry in root_result.entries:
                if isinstance(entry, FolderMetadata) and not self.is_excluded_by_user(
                        entry.path_display):
                    success.append(self.get_remote_folder(entry.path_display))

        if is_dbx_root:
            self.last_cursor = cursor

        return all(success)

    def get_remote_item(self, dbx_path):
        """
        Downloads a remote file or folder and updates its local rev and the revs of its
        children. If the remote item no longer exists, the corresponding local item will
        be deleted. Given paths will be added to the (persistent) pending_downloads list
        for the duration of the download so that they will be resumed in case Maestral
        is terminated during the download.

        :param str dbx_path: Dropbox path to file or folder.
        :return: ``True`` on success, ``False`` otherwise.
        :rtype: bool
        """
        self.pending_downloads.add(dbx_path)
        md = self.client.get_metadata(dbx_path, include_deleted=True)

        if isinstance(md, FolderMetadata):
            res = self.get_remote_folder(dbx_path)
        else:  # FileMetadata or DeletedMetadata
            res = self._create_local_entry(md)

        self.pending_downloads.discard(dbx_path)
        return res

    @catch_sync_issues
    def wait_for_remote_changes(self, last_cursor, timeout=40, delay=2):
        """
        Wraps MaestralApiClient.wait_for_remote_changes and catches sync errors.

        :param str last_cursor: Cursor form last sync.
        :param int timeout: Timeout in seconds before returning even if there are no
            changes. Dropbox adds random jitter of up to 30 sec to this value.
        :param float delay: Delay in sec to wait for subsequent changes that may be
            duplicates. This delay is typically only necessary folders are shared /
            un-shared with other Dropbox accounts.
        """
        logger.debug('Waiting for remote changes since cursor:\n%s', last_cursor)
        has_changes = self.client.wait_for_remote_changes(last_cursor, timeout=timeout)
        time.sleep(delay)
        logger.debug('Detected remote changes: %s', has_changes)
        return has_changes

    @catch_sync_issues
    def list_remote_changes(self, last_cursor):
        """Wraps ``MaestralApiClient.list_remove_changes`` and catches sync errors."""
        changes = self.client.list_remote_changes(last_cursor)
        logger.debug('Listed remote changes:\n%s', entries_to_str(changes.entries))
        clean_changes = self._clean_remote_changes(changes)
        logger.debug('Cleaned remote changes:\n%s', entries_to_str(clean_changes.entries))
        return clean_changes

    def filter_excluded_changes_remote(self, changes):
        """Removes all excluded items from the given list of changes.

        :param changes: :class:`dropbox.files.ListFolderResult` instance.
        :returns: (changes_filtered, changes_discarded)
        :rtype: tuple[:class:`dropbox.files.ListFolderResult`]
        """
        # filter changes from non-excluded folders
        entries_filtered = [e for e in changes.entries if not self.is_excluded_by_user(
            e.path_lower) or self.is_excluded(e.path_lower)]
        entries_discarded = list(set(changes.entries) - set(entries_filtered))

        changes_filtered = dropbox.files.ListFolderResult(
            entries=entries_filtered, cursor=changes.cursor, has_more=False)
        changes_discarded = dropbox.files.ListFolderResult(
            entries=entries_discarded, cursor=changes.cursor, has_more=False)

        return changes_filtered, changes_discarded

    def apply_remote_changes(self, changes, save_cursor=True):
        """
        Applies remote changes to local folder. Call this on the result of
        :method:`list_remote_changes`. The saved cursor is updated after a set
        of changes has been successfully applied.

        :param changes: :class:`dropbox.files.ListFolderResult` instance
            or ``False`` if requests failed.
        :param bool save_cursor: If True, :ivar:`last_cursor` will be updated
            from the last applied changes. Take care to only save a 'global' and
            'recursive' cursor which represents the state of the entire Dropbox
        :returns: List of changes that were made to local files, bool indicating if all
            download syncs were successful.
        :rtype: list, bool
        """

        if not changes:
            return False

        # filter out excluded changes
        changes_included, changes_excluded = self.filter_excluded_changes_remote(changes)

        # update queue
        for md in changes_included.entries:
            self.queued_for_download.put(self.to_local_path(md.path_display))

        # remove all deleted items from the excluded list
        _, _, deleted_excluded = self._separate_remote_entry_types(changes_excluded)
        for d in deleted_excluded:
            new_excluded = [f for f in self.excluded_items
                            if not f.startswith(d.path_lower)]
            self.excluded_items = new_excluded

        # sort changes into folders, files and deleted
        folders, files, deleted = self._separate_remote_entry_types(changes_included)

        # sort according to path hierarchy
        # do not create sub-folder / file before parent exists
        folders.sort(key=lambda x: x.path_display.count('/'))
        deleted.sort(key=lambda x: x.path_display.count('/'), reverse=True)

        downloaded = []  # local list of all changes

        # apply deleted items
        if deleted:
            logger.info('Applying deletions...')
        for item in deleted:
            downloaded.append(self._create_local_entry(item))

        # create local folders, start with top-level and work your way down
        if folders:
            logger.info('Creating folders...')
        for folder in folders:
            downloaded.append(self._create_local_entry(folder))

        # apply created files
        n_files = len(files)
        last_emit = time.time()
        with ThreadPoolExecutor(max_workers=6) as executor:
            fs = (executor.submit(self._create_local_entry, file) for file in files)
            for f, n in zip(as_completed(fs), range(1, n_files + 1)):
                if time.time() - last_emit > 1 or n in (1, n_files):
                    # emit messages at maximum every second
                    logger.info(f'Downloading {n}/{n_files}...')
                    last_emit = time.time()
                downloaded.append(f.result())

        success = all(downloaded)

        if save_cursor:
            self.last_cursor = changes.cursor

        return [entry for entry in downloaded if not isinstance(entry, bool)], success

    def check_download_conflict(self, md):
        """
        Check if local item is conflicting with remote item. The equivalent check when
        uploading and item will be carried out by Dropbox itself.

        Checks are carried out against our index, reflecting the latest sync state.

        :param Metadata md: Dropbox SDK metadata.
        :rtype: Conflict
        :raises: MaestralApiError if the Dropbox item does not exist.
        """

        # get metadata of remote item
        if isinstance(md, FileMetadata):
            remote_rev = md.rev
            remote_hash = md.content_hash
        elif isinstance(md, FolderMetadata):
            remote_rev = 'folder'
            remote_hash = 'folder'
        else:  # DeletedMetadata
            remote_rev = None
            remote_hash = None

        dbx_path = md.path_lower
        local_path = self.to_local_path(md.path_display)
        local_rev = self.get_local_rev(dbx_path)

        if remote_rev == local_rev:
            # Local change has the same rev. May be newer and
            # not yet synced or identical. Don't overwrite.
            logger.debug('Local item "%s" is the same or newer than on Dropbox', dbx_path)
            return Conflict.LocalNewerOrIdentical

        elif remote_rev != local_rev:
            # Dropbox server version has a different rev, likely is newer.
            # If the local version has been modified while sync was stopped,
            # those changes will be uploaded before any downloads can begin.
            # Conflict resolution will then be handled by Dropbox.
            # If the local version has been modified while sync was running
            # but changes were not uploaded before the remote version was
            # changed as well, the local ctime will be newer than last_sync:
            # (a) The upload of the changed file has already started. Upload thread
            #     will hold the lock and we won't be here checking for conflicts.
            # (b) The upload has not started yet. Manually check for conflict.

            if get_ctime(local_path) <= self.get_last_sync_for_path(dbx_path):
                logger.debug('No conflict: remote item "%s" is newer', dbx_path)
                return Conflict.RemoteNewer
            elif not remote_rev:
                logger.debug('Conflict: Local item "%s" has been modified since remote '
                             'deletion', dbx_path)
                return Conflict.LocalNewerOrIdentical
            else:
                local_hash = get_local_hash(local_path)
                if remote_hash == local_hash:
                    logger.debug('No conflict: contents are equal (%s)', dbx_path)
                    self.set_local_rev(dbx_path, remote_rev)  # update local rev
                    return Conflict.Identical
                else:
                    logger.debug('Conflict: local item "%s" was created since last upload',
                                 dbx_path)
                    return Conflict.Conflict

    def notify_user(self, changes):
        """
        Sends system notification for file changes.

        :param list changes: List of Dropbox metadata which has been applied locally.
        """

        # get number of remote changes
        n_changed = len(changes)

        if n_changed == 0:
            return

        user_name = None
        change_type = 'changed'

        # find out who changed the item(s), get the user name if its only a single user
        dbid_list = set(self._get_modified_by_dbid(md) for md in changes)
        if len(dbid_list) == 1:
            # all files have been modified by the same user
            dbid = dbid_list.pop()
            if dbid == self._conf.get('account', 'account_id'):
                user_name = 'You'
            else:
                account_info = self.client.get_account_info(dbid)
                user_name = account_info.name.display_name

        if n_changed == 1:
            # display user name, file name, and type of change
            md = changes[0]
            file_name = os.path.basename(md.path_display)

            if isinstance(md, DeletedMetadata):
                change_type = 'removed'
            elif isinstance(md, FileMetadata):
                revs = self.client.list_revisions(md.path_lower, limit=2)
                is_new_file = len(revs.entries) == 1
                change_type = 'added' if is_new_file else 'changed'
            elif isinstance(md, FolderMetadata):
                change_type = 'added'

        else:
            # display user name if unique, number of files, and type of change
            file_name = f'{n_changed} items'

            if all(isinstance(x, DeletedMetadata) for x in changes):
                change_type = 'removed'
            elif all(isinstance(x, FolderMetadata) for x in changes):
                change_type = 'added'
                file_name = f'{n_changed} folders'
            elif all(isinstance(x, FileMetadata) for x in changes):
                file_name = f'{n_changed} files'

        if user_name:
            msg = f'{user_name} {change_type} {file_name}'
        else:
            msg = f'{file_name} {change_type}'

        self.notifier.notify(msg, level=FILECHANGE)

    def _get_modified_by_dbid(self, md):
        """
        Returns the Dropbox ID of the user who modified a shared item or our own ID if the
        item was not shared.

        :param Metadata md: Dropbox file, folder or deleted metadata
        :return: Dropbox ID
        :rtype: str
        """

        try:
            return md.sharing_info.modified_by
        except AttributeError:
            return self._conf.get('account', 'account_id')

    @staticmethod
    def _separate_remote_entry_types(result):
        """
        Sorts entries in :class:`dropbox.files.ListFolderResult` into
        FolderMetadata, FileMetadata and DeletedMetadata.

        :returns: Tuple of (folders, files, deleted) containing instances of
            :class:`DeletedMetadata`, `:class:FolderMetadata`,
            and :class:`FileMetadata` respectively.
        :rtype: tuple
        """

        folders = [x for x in result.entries if isinstance(x, FolderMetadata)]
        files = [x for x in result.entries if isinstance(x, FileMetadata)]
        deleted = [x for x in result.entries if isinstance(x, DeletedMetadata)]

        return folders, files, deleted

    @staticmethod
    def _clean_remote_changes(changes):
        """
        Takes remote file events since last sync and cleans them up so that there is only
        a single event per path.

        Dropbox will sometimes report multiple changes per path. Once such instance is
        when sharing a folder: ``files/list_folder/continue`` will report the shared
        folder and its children as deleted and then created because the folder *is*
        actually deleted from the user's Dropbox and recreated as a shared folder which
        then gets mounted to the user's Dropbox. Ideally, we want to deal with this
        without re-downloading all its contents.

        :param changes: :class:`dropbox.files.ListFolderResult`
        :returns: Cleaned up changes with a single Metadata entry per path.
        :rtype: :class:`dropbox.files.ListFolderResult`
        """

        # Note: we won't have to deal with modified or moved events,
        # Dropbox only reports DeletedMetadata or FileMetadata / FolderMetadata

        all_paths = [e.path_lower for e in changes.entries]

        unique_paths = list(OrderedDict.fromkeys(all_paths))
        histories = [[e for e in changes.entries if e.path_lower == unique_path]
                     for unique_path in unique_paths]

        new_entries = []

        for h in histories:
            # Dropbox guarantees that applying events in the provided order
            # will reproduce the state in the cloud. We therefore combine:
            # deleted, ..., modified -> modified
            # modified, ..., deleted -> deleted
            # modified, ..., modified -> modified
            new_entries.append(h[-1])

        changes.entries = new_entries

        return changes

    @catch_sync_issues
    def _create_local_entry(self, entry):
        """
        Creates local file / folder for remote entry.

        :param Metadata class entry: Dropbox FileMetadata|FolderMetadata|DeletedMetadata.
        :returns: Copy of metadata if the change was downloaded, ``True`` if the change
            already existed locally and ``False`` if the download failed.
        :raises: MaestralApiError on failure.
        """

        local_path = self.to_local_path(entry.path_display)

        # book keeping
        self.clear_sync_error(dbx_path=entry.path_display)
        remove_from_queue(self.queued_for_download, local_path)

        conflict_check = self.check_download_conflict(entry)

        applied = None

        if conflict_check in (Conflict.Identical, Conflict.LocalNewerOrIdentical):
            return applied

        elif conflict_check == Conflict.Conflict:
            # rename local item and get remote
            base, ext = osp.splitext(local_path)
            new_local_file = ''.join((base, ' (conflicting copy)', ext))
            shutil.move(local_path, new_local_file)

        if isinstance(entry, FileMetadata):
            # Store the new entry at the given path in your local state.
            # If the required parent folders don’t exist yet, create them.
            # If there’s already something else at the given path,
            # replace it and remove all its children.

            self._save_to_history(entry.path_display)

            with InQueue(local_path, queue=self.queue_downloading, delay=1):

                if osp.isdir(local_path):
                    delete(local_path)

                md = self.client.download(entry.path_display, local_path)
                self.set_last_sync_for_path(md.path_lower, get_ctime(local_path))
                self.set_local_rev(md.path_lower, md.rev)

            logger.debug('Created local file "%s"', entry.path_display)
            applied = entry

        elif isinstance(entry, FolderMetadata):
            # Store the new entry at the given path in your local state.
            # If the required parent folders don’t exist yet, create them.
            # If there’s already something else at the given path,
            # replace it but leave the children as they are.

            with InQueue(local_path, queue=self.queue_downloading, delay=1):

                if osp.isfile(local_path):
                    delete(local_path)

                try:
                    os.makedirs(local_path)
                except FileExistsError:
                    pass
                self.set_last_sync_for_path(entry.path_lower, get_ctime(local_path))
                self.set_local_rev(entry.path_lower, 'folder')

            logger.debug('Created local folder "%s"', entry.path_display)
            applied = entry

        elif isinstance(entry, DeletedMetadata):
            # If your local state has something at the given path,
            # remove it and all its children. If there’s nothing at the
            # given path, ignore this entry.

            with InQueue(local_path, queue=self.queue_downloading, delay=1):
                err = delete(local_path)
                self.set_local_rev(entry.path_lower, None)
                self.set_last_sync_for_path(entry.path_lower, 0.0)

            if not err:
                logger.debug('Deleted local item "%s"', entry.path_display)
                applied = entry
            else:
                logger.debug('Deletion failed: %s', err)

        return applied

    def _save_to_history(self, dbx_path):
        # add new file to recent_changes
        recent_changes = self._state.get('sync', 'recent_changes')
        recent_changes.append(dbx_path)
        # eliminate duplicates
        recent_changes = list(OrderedDict.fromkeys(recent_changes))
        self._state.set('sync', 'recent_changes', recent_changes[-self._max_history:])


# ========================================================================================
# Workers for upload, download and connection monitoring threads
# ========================================================================================

def connection_helper(sync, syncing, paused_by_user, running, connected,
                      startup, check_interval=4):
    """
    A worker which periodically checks the connection to Dropbox servers.
    This is done through inexpensive calls to :method:`client.get_space_usage`.
    If the connection is lost, ``connection_helper`` pauses all syncing until a
    connection can be reestablished.

    :param UpDownSync sync: UpDownSync instance.
    :param Event syncing: Event that indicates if workers are running or paused.
    :param Event paused_by_user: Set if the syncing was paused by the user.
    :param Event running: Event to shutdown connection helper.
    :param Event connected: Event that indicates if we can connect to Dropbox.
    :param int check_interval: Time in seconds between connection checks.
    :param Event startup: Set when startup scripts have been requested.
    """

    while running.is_set():
        try:
            # use an inexpensive call to `get_space_usage` to test connection
            sync.client.get_space_usage()
            if not connected.is_set() and not paused_by_user.is_set():
                startup.set()
            connected.set()
            time.sleep(check_interval)
        except ConnectionError:
            if connected.is_set():
                logger.debug(DISCONNECTED, exc_info=True)
                logger.info(DISCONNECTED)
            syncing.clear()
            connected.clear()
            time.sleep(check_interval / 2)
        except DropboxAuthError as e:
            running.clear()
            syncing.clear()
            logger.error(e.title, exc_info=True)
        except Exception:
            running.clear()
            syncing.clear()
            logger.error('Unexpected error', exc_info=True)


def download_worker(sync, syncing, running, connected):
    """
    Worker to sync changes of remote Dropbox with local folder.

    :param UpDownSync sync: Instance of :class:`UpDownSync`.
    :param Event syncing: Event that indicates if workers are running or paused.
    :param Event running: Event to shutdown local file event handler and worker threads.
    :param Event connected: Event that indicates if we can connect to Dropbox.
    """

    while running.is_set():

        syncing.wait()

        try:
            has_changes = sync.wait_for_remote_changes(sync.last_cursor, timeout=30)

            if not (running.is_set() and syncing.is_set()):
                continue

            if has_changes:
                with sync.lock:
                    logger.info(SYNCING)

                    changes = sync.list_remote_changes(sync.last_cursor)
                    downloaded, _ = sync.apply_remote_changes(changes)
                    sync.notify_user(downloaded)

                    logger.info(IDLE)

        except ConnectionError:
            syncing.clear()
            connected.clear()
            logger.debug(DISCONNECTED, exc_info=True)
            logger.info(DISCONNECTED)
        except MaestralApiError as e:
            running.clear()
            syncing.clear()
            logger.error(e.title, exc_info=True)
        except Exception:
            running.clear()
            syncing.clear()
            logger.error('Unexpected error', exc_info=True)


def download_worker_added_item(sync, syncing, running, connected):
    """
    Worker to download items which have been newly included in sync.

    :param UpDownSync sync: Instance of :class:`UpDownSync`.
    :param Event syncing: Event that indicates if workers are running or paused.
    :param Event running: Event to shutdown local file event handler and worker threads.
    :param Event connected: Event that indicates if we can connect to Dropbox.
    """

    while running.is_set():

        syncing.wait()

        dbx_path = sync.queued_newly_included_downloads.get()

        if not (running.is_set() and syncing.is_set()):
            sync.pending_downloads.add(dbx_path)
            continue

        try:
            with sync.lock:
                sync.get_remote_item(dbx_path)
            logger.info(IDLE)
        except ConnectionError:
            syncing.clear()
            connected.clear()
            logger.debug(DISCONNECTED, exc_info=True)
            logger.info(DISCONNECTED)
        except MaestralApiError as e:
            running.clear()
            syncing.clear()
            logger.error(e.title, exc_info=True)
        except Exception:
            running.clear()
            syncing.clear()
            logger.error('Unexpected error', exc_info=True)


def upload_worker(sync, syncing, running, connected):
    """
    Worker to sync local changes to remote Dropbox.

    :param UpDownSync sync: Instance of :class:`UpDownSync`.
    :param Event syncing: Event that indicates if workers are running or paused.
    :param Event running: Event to shutdown local file event handler and worker threads.
    :param Event connected: Event that indicates if we can connect to Dropbox.
    """

    while running.is_set():

        syncing.wait()

        try:
            events, local_cursor = sync.wait_for_local_changes(timeout=5)

            if not (running.is_set() and syncing.is_set()):
                continue

            if len(events) > 0:
                with sync.lock:
                    logger.info(SYNCING)
                    sync.apply_local_changes(events, local_cursor)
                    logger.info(IDLE)
            else:
                sync.last_sync = local_cursor

        except ConnectionError:
            syncing.clear()
            connected.clear()
            logger.debug(DISCONNECTED, exc_info=True)
            logger.info(DISCONNECTED)
        except MaestralApiError as e:
            running.clear()
            syncing.clear()
            logger.error(e.title, exc_info=True)
        except Exception:
            running.clear()
            syncing.clear()
            logger.error('Unexpected error', exc_info=True)


def startup_worker(sync, syncing, running, connected, startup, paused_by_user):
    """
    Worker to sync local changes to remote Dropbox.

    :param UpDownSync sync: Instance of :class:`UpDownSync`.
    :param Event syncing: Event that indicates if workers are running or paused.
    :param Event running: Event to shutdown local file event handler and worker threads.
    :param Event connected: Event that indicates if we can connect to Dropbox.
    :param Event startup: Set when we should run startup routines.
    :param Event paused_by_user: Set when syncing has been paused by the user.
    """

    while running.is_set():

        startup.wait()
        assert not syncing.is_set()

        try:
            with sync.lock:
                # run / resume initial download
                # local changes during this download will be registered
                # by the local FileSystemObserver but only uploaded after
                # `syncing` has been set
                if sync.last_cursor == '':
                    sync.clear_all_sync_errors()
                    sync.get_remote_folder()
                    sync.last_sync = time.time()

                if not running.is_set():
                    continue

                # retry failed / interrupted downloads
                logger.info('Checking for pending downloads...')
                for dbx_path in list(sync.download_errors):
                    sync.get_remote_item(dbx_path)

                for dbx_path in list(sync.pending_downloads):
                    sync.get_remote_item(dbx_path)

                # upload changes while inactive
                sync.upload_local_changes_while_inactive()

                if not running.is_set():
                    continue

            if not paused_by_user.is_set():
                syncing.set()

            startup.clear()

            logger.info(IDLE)

        except ConnectionError:
            syncing.clear()
            connected.clear()
            logger.debug(DISCONNECTED, exc_info=True)
            logger.info(DISCONNECTED)
        except MaestralApiError as e:
            running.clear()
            syncing.clear()
            logger.error(e.title, exc_info=True)
        except Exception:
            running.clear()
            syncing.clear()
            logger.error('Unexpected error', exc_info=True)

    startup.clear()


# ========================================================================================
# Main Monitor class to start, stop and coordinate threads
# ========================================================================================

class MaestralMonitor:
    """
    Class to sync changes between Dropbox and a local folder. It creates four
    threads: `observer` to catch local file events, `upload_thread` to upload
    caught changes to Dropbox, `download_thread` to query for and download
    remote changes, and `connection_thread` which periodically checks the
    connection to Dropbox servers.

    :param MaestralApiClient client: The Dropbox API client, a wrapper around the Dropbox
        Python SDK.

    :ivar Thread local_observer_thread: Watchdog observer thread that detects local file
        system events.
    :ivar Thread upload_thread: Thread that sorts uploads local changes.
    :ivar Thread download_thread: Thread to query for and download remote changes.
    :ivar Thread file_handler: Handler to queue file events from `observer` for upload.
    :ivar UpDownSync sync: Object to coordinate syncing. This is the brain of Maestral.
        It contains the logic to process local and remote file events and to apply them
        while checking for conflicts.

    :ivar Event connected: Set when connected to Dropbox servers.
    :ivar Event startup: Set when startup scripts have to be run after syncing
        was inactive, for instance when Maestral is started, the internet connection is
        reestablished or syncing is resumed after pausing.
    :ivar Event syncing: Set when sync is running.
    :ivar Event running: Set when the sync threads are alive.
    :ivar Event paused_by_user: Set when sync is paused by the user.

    :cvar Queue queue_downloading: Holds *local file paths* that are being downloaded.
    :cvar Queue queue_uploading: Holds *local file paths* that are being uploaded.
    :cvar TimedQueue local_file_event_queue: Holds *file events* to be uploaded.
    """

    queue_downloading = Queue()
    queue_uploading = Queue()

    local_file_event_queue = TimedQueue()

    def __init__(self, client):

        self.connected = Event()
        self.syncing = Event()
        self.running = Event()
        self.paused_by_user = Event()
        self.paused_by_user.set()

        self.startup = Event()

        self.client = client
        self.config_name = self.client.config_name
        self.file_handler = FileEventHandler(
            self.syncing, self.startup,
            self.local_file_event_queue, self.queue_downloading
        )

        self.sync = UpDownSync(self.client, self.local_file_event_queue,
                               self.queue_uploading, self.queue_downloading)

    @property
    def uploading(self):
        """Returns a list of all items currently uploading."""
        return tuple(self.queue_uploading.queue)

    @property
    def downloading(self):
        """Returns a list of all items currently downloading."""
        return tuple(self.queue_downloading.queue)

    @property
    def queued_for_upload(self):
        """Returns a list of all items queued for upload."""
        return tuple(self.sync.queued_for_upload.queue)

    @property
    def queued_for_download(self):
        """Returns a list of all items queued for download."""
        return tuple(self.sync.queued_for_download.queue)

    def start(self):
        """Creates observer threads and starts syncing."""

        if self.running.is_set():
            # do nothing if already running
            return

        self.running = Event()  # create new event to let old threads shut down

        self.local_observer_thread = Observer(timeout=0.1)
        self.local_observer_thread.schedule(
            self.file_handler, self.sync.dropbox_path, recursive=True
        )

        self.connection_thread = Thread(
            target=connection_helper,
            daemon=True,
            args=(
                self.sync, self.syncing, self.paused_by_user, self.running,
                self.connected, self.startup,
            ),
            name='maestral-connection-helper'
        )

        self.startup_thread = Thread(
            target=startup_worker,
            daemon=True,
            args=(
                self.sync, self.syncing, self.running, self.connected,
                self.startup, self.paused_by_user
            ),
            name='maestral-startup-worker'
        )

        self.download_thread = Thread(
            target=download_worker,
            daemon=True,
            args=(
                self.sync, self.syncing, self.running, self.connected,
            ),
            name='maestral-download'
        )

        self.download_thread_added_folder = Thread(
            target=download_worker_added_item,
            daemon=True,
            args=(
                self.sync, self.syncing, self.running, self.connected,
            ),
            name='maestral-folder-download'
        )

        self.upload_thread = Thread(
            target=upload_worker,
            daemon=True,
            args=(
                self.sync, self.syncing, self.running, self.connected,
            ),
            name='maestral-upload'
        )

        try:
            self.local_observer_thread.start()
        except OSError as exc:
            if 'inotify' in exc.args[0]:
                title = 'Inotify limit reached'
                msg = ('Changes to your Dropbox folder cannot be monitored because it '
                       'contains too many items. Please increase the inotify limit in '
                       'your system by adding the following line to /etc/sysctl.conf:\n\n'
                       'fs.inotify.max_user_watches=524288')
                new_exc = InotifyError(title, msg).with_traceback(exc.__traceback__)
                exc_info = (type(new_exc), new_exc, new_exc.__traceback__)
                logger.error(title, exc_info=exc_info)
                return
            else:
                raise exc

        self.running.set()
        self.syncing.clear()
        self.connected.set()
        self.startup.set()

        self.connection_thread.start()
        self.startup_thread.start()
        self.upload_thread.start()
        self.download_thread.start()
        self.download_thread_added_folder.start()

        self.paused_by_user.clear()

    def pause(self):
        """Pauses syncing."""

        self.paused_by_user.set()
        self.syncing.clear()
        self._wait_for_idle()
        logger.info(PAUSED)

    def resume(self):
        """Checks for changes while idle and starts syncing."""

        if not self.paused_by_user.is_set():
            return

        self.startup.set()
        self.paused_by_user.clear()

    def stop(self):
        """Stops syncing and destroys worker threads."""

        if not self.running.is_set():
            return

        logger.info('Shutting down threads...')

        self.running.clear()
        self.syncing.clear()
        self.paused_by_user.clear()
        self.startup.clear()

        self._wait_for_idle()

        self.local_observer_thread.stop()
        self.local_observer_thread.join()
        self.connection_thread.join()
        self.upload_thread.join()

        logger.info(STOPPED)

    def _wait_for_idle(self):
        self.sync.lock.acquire()
        self.sync.lock.release()

    def _threads_alive(self):
        """Returns ``True`` if all threads are alive, ``False`` otherwise."""

        try:
            threads = (
                self.local_observer_thread,
                self.upload_thread, self.download_thread,
                self.download_thread_added_folder,
                self.connection_thread,
                self.startup_thread
            )
        except AttributeError:
            return False

        base_threads_alive = (t.is_alive() for t in threads)
        watchdog_emitters_alive = (e.is_alive() for e in self.local_observer_thread.emitters)

        return all(base_threads_alive) and all(watchdog_emitters_alive)

    def rebuild_index(self):
        """
        Rebuilds the rev file by comparing remote with local files and updating rev
        numbers from the Dropbox server. Files are compared by their content hashes and
        conflicting copies are created if the contents differ. File changes during the
        rebuild process will be queued and uploaded once rebuilding has completed.

        Rebuilding will be performed asynchronously.

        :raises: :class:`MaestralApiError`
        """

        logger.info('Rebuilding index...')

        self.pause()

        self.sync.last_sync = 0.0
        self.sync.last_cursor = ''
        self.sync.clear_rev_index()

        if not self.running.is_set():
            self.start()
        else:
            self.resume()


# ========================================================================================
# Helper functions
# ========================================================================================


def get_dest_path(event):
    """
    Returns dest_path or src_path of local FileEvent

    :param FileEvent event: Watchdog file event.
    :return:
    :rtype: str
    """
    return getattr(event, 'dest_path', event.src_path)


def get_local_hash(local_path):
    """
    Computes content hash of a local file.

    :param str local_path: Path to local file.
    :returns: Content hash to compare with Dropbox's content hash,
        or 'folder' if the path points to a directory. ``None`` if there
        is nothing at the path.
    :rtype: str
    """

    hasher = DropboxContentHasher()

    try:
        with open(local_path, 'rb') as f:
            while True:
                chunk = f.read(1024)
                if len(chunk) == 0:
                    break
                hasher.update(chunk)

        return str(hasher.hexdigest())
    except IsADirectoryError:
        return 'folder'
    except FileNotFoundError:
        return None
    finally:
        del hasher


def get_ctime(local_path):
    """
    Returns the ctime of a local item or -1.0 if there is nothing at the path. If the item
    is a directory, return the largest ctime of itself and its children.

    :param str local_path:
    :returns: Ctime or -1.0.
    :rtype: float
    """

    try:
        stat = os.stat(local_path)
        if S_ISDIR(stat.st_mode):
            ctime = stat.st_ctime
            with os.scandir(local_path) as it:
                for entry in it:
                    ctime = max(ctime, entry.stat().st_ctime)
            return ctime
        else:
            return os.stat(local_path).st_ctime
    except FileNotFoundError:
        return -1.0


def remove_from_queue(q, *items):
    """
    Tries to remove an item from a queue.

    :param Queue q: Queue to remove item from.
    :param items: Items to remove
    """

    with q.mutex:
        for item in items:
            try:
                q.queue.remove(item)
            except ValueError:
                pass


def iter_to_str(iterable):
    return '\n'.join(str(e) for e in iterable)


def entries_to_str(entries):
    str_reps = [f'<{e.__class__.__name__}(path_display={e.path_display})>'
                for e in entries]
    return '\n'.join(str_reps)
