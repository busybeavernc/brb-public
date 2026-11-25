"""Audit log: who did what in Admin (and what customers changed themselves), newest first."""
import json
import time

from . import users

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, who TEXT NOT NULL, action TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '', details TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS audit_at ON audit (at);
CREATE INDEX IF NOT EXISTS audit_target ON audit (target);
"""
_ready = set()


def db():
    con = users.db()
    key = (id(con), users.path())
    if key not in _ready:
        con.executescript(SCHEMA)
        _ready.add(key)
    return con


def log(who, action, target='', **details):
    con = db()
    con.execute('INSERT INTO audit (at, who, action, target, details) VALUES (?,?,?,?,?)',
                (time.time(), str(who)[:120], action, str(target)[:120],
                 json.dumps(details, default=str)[:2000] if details else ''))
    con.commit()


def recent(limit=200, target=None, q=''):
    sql, args = 'SELECT * FROM audit', []
    where = []
    if target:
        where.append('target IN (%s)' % ','.join('?' * len(target)))   # nosec B608 - placeholders only
        args += list(target)
    if q:
        where.append('(lower(who) LIKE ? OR lower(action) LIKE ? OR lower(target) LIKE ? OR lower(details) LIKE ?)')
        args += [f'%{q.lower()}%'] * 4
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY at DESC LIMIT ?'
    args.append(int(limit))
    return [dict(r) for r in db().execute(sql, args)]
