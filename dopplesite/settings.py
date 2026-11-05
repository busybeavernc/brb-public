"""Settings from the environment (.env is loaded by wsgi.py). Real environment variables win."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, '') else default


def flag(name, default=False):
    v = get(name)
    return default if v is None else str(v).strip().lower() in ('1', 'true', 'yes', 'on')


def data_dir():
    d = get('DATA_DIR') or os.path.join(ROOT, 'data')
    os.makedirs(d, exist_ok=True)
    return d


def public_url():
    return (get('PUBLIC_URL') or 'https://brb.happyhacks.io').rstrip('/')


def releases_repo():
    """The public GitHub repository the app's releases are published to."""
    repo = get('RELEASES_REPO') or 'busybeavernc/brb-public'
    return 'busybeavernc/brb-public' if repo == 'busybeavernc/doppleganger-public' else repo   # renamed on GitHub


def contacts(name):
    """Comma-separated emails / mobiles from a setting, normalised for comparing."""
    return {norm_contact(c) for c in str(get(name, '')).split(',') if c.strip()}


def norm_contact(c):
    c = str(c or '').strip().lower()
    if '@' in c:
        return c
    digits = ''.join(ch for ch in c if ch.isdigit())
    if len(digits) == 10:  # a US number written without the country code
        digits = '1' + digits
    return '+' + digits if digits else ''
