"""The latest BRB release on GitHub, and the download link for each kind of computer.

Read from the GitHub API (cached for a few minutes per worker). If GitHub can't be reached, the download buttons
still work: they use GitHub's permanent "latest release" links, just without the version number on the page.
"""
import json
import threading
import time
import urllib.error
import urllib.request

from . import settings

PLATFORMS = {
    'windows': {'asset': 'BRBSetup.exe', 'label': 'Windows', 'detail': 'Windows 10 / 11'},
    'mac-arm64': {'asset': 'BRB-mac-arm64.dmg', 'label': 'Mac', 'detail': 'Apple Silicon (M1 and newer)'},
    'mac-intel': {'asset': 'BRB-mac-x86_64.dmg', 'label': 'Mac', 'detail': 'Intel'},
}
# names the downloads had before the rename to BRB (still offered until a BRB build of that kind is released)
OLD_ASSETS = {'windows': 'DopplegangerSetup.exe', 'mac-arm64': 'Doppleganger-mac-arm64.dmg',
              'mac-intel': 'Doppleganger-mac-x86_64.dmg'}
CACHE_SECONDS = 300

_lock = threading.Lock()
_cache = {'at': 0.0, 'data': None}


LEGACY_REPO = 'busybeavernc/doppleganger'   # where releases lived before the public downloads repo


def _fetch():
    """The latest release; if the downloads repo has none yet, the one in the app's code repo (while it's public)."""
    try:
        return _fetch_repo(settings.releases_repo())
    except urllib.error.HTTPError as e:
        if e.code != 404 or settings.releases_repo() == LEGACY_REPO:
            raise
    try:
        return _fetch_repo(LEGACY_REPO)
    except urllib.error.HTTPError as e:
        if e.code == 404:   # no release anywhere yet: the page shows "coming soon" instead of dead links
            return {'version': '', 'published': '', 'url': '', 'assets': {}, 'repo': settings.releases_repo()}
        raise


def _fetch_repo(repo):
    """The newest release, with each download taken from the newest release that has it: a Windows-only release
    still leaves the latest Mac build on offer."""
    url = f'https://api.github.com/repos/{repo}/releases?per_page=20'
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'doppleganger-site'}
    if settings.get('GITHUB_TOKEN'):
        headers['Authorization'] = 'Bearer ' + settings.get('GITHUB_TOKEN')
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=6) as r:
        rels = [x for x in json.load(r) if not x.get('draft') and not x.get('prerelease')]
    if not rels:
        raise urllib.error.HTTPError(url, 404, 'no releases', None, None)
    newest = rels[0]   # GitHub lists the newest first
    assets = {}
    for rel in rels:
        for a in rel.get('assets', []):
            assets.setdefault(a['name'], {'url': a['browser_download_url'], 'size': a.get('size', 0),
                                          'version': str(rel.get('tag_name', '')).lstrip('v')})
    return {'version': str(newest.get('tag_name', '')).lstrip('v'), 'published': newest.get('published_at', ''),
            'url': newest.get('html_url', ''), 'assets': assets, 'repo': repo}


def latest(force=False):
    """{version, published, url, assets: {name: {url, size}}}, or None if GitHub can't be reached."""
    with _lock:
        fresh = time.time() - _cache['at'] < CACHE_SECONDS
        if fresh and not force:
            return _cache['data']
    try:
        data = _fetch()
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        data = None
        with _lock:
            if _cache['data'] is not None:   # keep showing the last good answer
                _cache['at'] = time.time() - CACHE_SECONDS + 60
                return _cache['data']
    with _lock:
        _cache.update(at=time.time(), data=data)
    return data


def reset():
    with _lock:
        _cache.update(at=0.0, data=None)


def _asset(rel, platform):
    """This platform's download in the release info: the BRB build, else the one from before the rename."""
    return rel['assets'].get(PLATFORMS[platform]['asset']) or rel['assets'].get(OLD_ASSETS.get(platform, ''))


def link(platform):
    """(url, version) for a platform's download, or (None, None) if this release doesn't have it."""
    p = PLATFORMS.get(platform)
    if not p:
        return None, None
    rel = latest()
    if rel is None:   # GitHub API unreachable: GitHub's permanent latest-release link
        return f'https://github.com/{settings.releases_repo()}/releases/latest/download/{p["asset"]}', ''
    a = _asset(rel, platform)
    return (a['url'], a.get('version') or rel['version']) if a else (None, None)


def available():
    """{platform: bool} for the buttons on the download page."""
    rel = latest()
    if rel is None:
        return {k: True for k in PLATFORMS}
    return {k: _asset(rel, k) is not None for k in PLATFORMS}


def size_mb(platform):
    rel = latest()
    a = rel and _asset(rel, platform)
    return round(a['size'] / 1e6) if a and a.get('size') else None
