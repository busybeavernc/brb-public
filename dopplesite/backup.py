"""Nightly copy of the site's database: DATA_DIR/backups/doppleganger-YYYY-MM-DD.sqlite, the last KEEP days kept.

Runs by itself (at most once a day, in the background, when the site is in use), or by hand:
    python -m dopplesite.backup
Copy DATA_DIR/backups off the server now and then (or point your server backup at it).
"""
import glob
import logging
import os
import sqlite3
import sys
import threading
import time

from . import settings, users

log = logging.getLogger('dopplesite')
KEEP = 14
_lock = threading.Lock()


def folder():
    d = os.path.join(settings.data_dir(), 'backups')
    os.makedirs(d, exist_ok=True)
    return d


def run():
    """Make today's backup (a consistent snapshot, safe while the site runs) and drop old ones. -> its path."""
    dest = os.path.join(folder(), time.strftime('doppleganger-%Y-%m-%d.sqlite'))
    tmp = dest + '.part'
    src = sqlite3.connect(users.path(), timeout=30)
    try:
        out = sqlite3.connect(tmp)
        with out:
            src.backup(out)
        out.close()
    finally:
        src.close()
    os.replace(tmp, dest)
    for old in sorted(glob.glob(os.path.join(folder(), 'doppleganger-*.sqlite')))[:-KEEP]:
        try:
            os.remove(old)
        except OSError:
            pass
    return dest


def latest():
    files = sorted(glob.glob(os.path.join(folder(), 'doppleganger-*.sqlite')))
    return (files[-1], os.path.getmtime(files[-1])) if files else (None, None)


def maybe_run():
    """Called on requests: starts today's backup if there isn't one yet. Cheap when there is."""
    if settings.flag('NO_AUTO_BACKUP'):
        return
    path, at = latest()
    if at and time.time() - at < 23 * 3600:
        return
    if not _lock.acquire(blocking=False):
        return

    def work():
        try:
            log.info('Database backup: %s', run())
            from . import support
            n = support.purge_old()
            if n:
                log.info('Deleted %d support requests closed over %d days ago', n, support.KEEP_CLOSED_DAYS)
        except Exception:  # noqa: BLE001
            log.exception('Database backup failed')
        finally:
            _lock.release()
    threading.Thread(target=work, daemon=True).start()


if __name__ == '__main__':
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(settings.ROOT, '.env'))
    except ImportError:
        pass
    print(run())
    sys.exit(0)
