"""Signed license replies: BRB only accepts LOOP time from replies signed with this site's private key.

Without this, someone could point BRB at a fake server (for example by adding their own certificate authority to the
certificates BRB ships with) that always says "unlimited". The app has the matching public key built in, checks
the signature on every status and lease reply, and checks that each reply answers its own request (a fresh random
nonce) for its own computer, so old replies can't be replayed either.

The key is an Ed25519 key in DATA_DIR/license-signing.key, made on first use. Back it up (it isn't in the database
backups): if it's lost, BRB versions that know the old key can't use the LOOP until they update.
Its public half: https://<site>/api/v1/license/public-key, or
    python -m dopplesite.signing
"""
import base64
import os
import sys
import threading

from . import settings

_lock = threading.Lock()
_key = None


def key_path():
    return os.path.join(settings.data_dir(), 'license-signing.key')


def _load():
    global _key
    p = key_path()
    if _key is not None and _key[0] == p:
        return _key[1]
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    with _lock:
        if _key is None or _key[0] != p:
            if os.path.exists(p):
                with open(p, 'rb') as f:
                    raw = base64.b64decode(f.read().strip())
            else:
                raw = os.urandom(32)
                fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as f:
                    f.write(base64.b64encode(raw) + b'\n')
            _key = (p, Ed25519PrivateKey.from_private_bytes(raw))
    return _key[1]


def reset():
    global _key
    _key = None


def sign(data: bytes) -> str:
    return base64.b64encode(_load().sign(data)).decode()


def public_key() -> str:
    from cryptography.hazmat.primitives import serialization
    raw = _load().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


if __name__ == '__main__':
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(settings.ROOT, '.env'))
    except ImportError:
        pass
    print(public_key())
    sys.exit(0)
