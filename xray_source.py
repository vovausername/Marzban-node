"""Persist installation provenance, bound to the executable's SHA256."""
import hashlib
import json
import os
import re
import tempfile
from functools import lru_cache


@lru_cache(maxsize=8)
def _digest(path, signature):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _binary_digest(path):
    st = os.stat(path)
    return _digest(path, (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns))


def record_source(path, repository, tag=None):
    data = {"repository": repository, "tag": tag, "sha256": _binary_digest(path)}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=os.path.dirname(path) or ".", delete=False) as stream:
            temporary = stream.name
            json.dump(data, stream)
        os.replace(temporary, path + ".source.json")
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def read_source(path):
    try:
        with open(path + ".source.json") as stream:
            data = json.load(stream)
        repository = data.get("repository", "")
        if not isinstance(repository, str) or not re.fullmatch(
            r"https://github\.com/[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*", repository
        ):
            return None
        if data.get("sha256") != _binary_digest(path):
            return None
        tag = data.get("tag")
        return {"repository": repository, "tag": tag if isinstance(tag, str) else None}
    except (OSError, ValueError, AttributeError, TypeError):
        return None
