"""Náhrada `xbmcvfs` — cesty zůstávají obyčejné cesty souborového systému."""
import os
import shutil


def translatePath(path):
    return path


def exists(path):
    return os.path.exists(path)


def mkdirs(path):
    os.makedirs(path, exist_ok=True)
    return True


def mkdir(path):
    os.makedirs(path, exist_ok=True)
    return True


def delete(path):
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def rmdir(path, force=False):
    shutil.rmtree(path, ignore_errors=True)
    return True


def listdir(path):
    dirs, files = [], []
    for name in os.listdir(path):
        (dirs if os.path.isdir(os.path.join(path, name)) else files).append(name)
    return dirs, files


def copy(src, dst):
    shutil.copy(src, dst)
    return True


class File:
    def __init__(self, path, mode="r"):
        self.f = open(path, mode + ("b" if "b" not in mode else ""))

    def read(self, n=-1):
        return self.f.read(n)

    def write(self, data):
        return self.f.write(data if isinstance(data, bytes) else data.encode())

    def close(self):
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
