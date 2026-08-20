import os

from conftest import mkfile

from mobile_broom.sizer import Sizer, human, walk_size


def test_walk_size_counts_allocated_bytes_and_skips_symlinks(tmp_path):
    mkfile(tmp_path / "a" / "f1", size=4096)
    mkfile(tmp_path / "a" / "b" / "f2", size=4096)
    os.symlink("/", tmp_path / "a" / "root-link")
    size = walk_size(tmp_path / "a")
    assert 8192 <= size < 8192 + 5 * 4096  # files + dir blocks, nothing from the symlink target


def test_walk_size_never_crosses_a_mountpoint(tmp_path, monkeypatch):
    mkfile(tmp_path / "a" / "f1", size=4096)
    mkfile(tmp_path / "a" / "Volumes" / "huge", size=65536)
    real_scandir = os.scandir

    class Entry:
        def __init__(self, e):
            self._e = e
            self.path, self.name = e.path, e.name

        def stat(self, follow_symlinks=True):
            st = self._e.stat(follow_symlinks=follow_symlinks)
            if self.name == "Volumes":
                vals = list(st)
                vals[2] = st.st_dev + 1  # st_dev: pretend a different filesystem is mounted here
                return os.stat_result(vals)
            return st

    class Scan:
        def __init__(self, it):
            self.it = it

        def __iter__(self):
            return (Entry(e) for e in self.it)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.it.close()

    monkeypatch.setattr(os, "scandir", lambda p: Scan(real_scandir(p)))
    size = walk_size(tmp_path / "a")
    assert size < 65536  # the 'mount' was skipped entirely


def test_walk_size_honours_cachedir_tag(tmp_path):
    mkfile(
        tmp_path / "a" / "cache" / "CACHEDIR.TAG",
        text="Signature: 8a477f597d28d172789f06886806bc55",
    )
    mkfile(tmp_path / "a" / "cache" / "big", size=65536)
    mkfile(tmp_path / "a" / "f", size=4096)
    assert walk_size(tmp_path / "a", honour_cachedir_tag=True) < 65536
    assert walk_size(tmp_path / "a") >= 65536


def test_sizer_cache_roundtrip(tmp_path):
    mkfile(tmp_path / "d" / "f", size=4096)
    cache = tmp_path / "sizes.json"
    s1 = Sizer(cache_path=cache)
    n = s1.size_path(str(tmp_path / "d"))
    s1.save()
    assert cache.exists()
    s2 = Sizer(cache_path=cache)
    assert s2.size_path(str(tmp_path / "d")) == n
    s3 = Sizer(cache_path=cache, refresh=True)
    assert s3.size_path(str(tmp_path / "d")) == n


def test_human():
    assert human(None).strip() == "?"
    assert human(8_389_757_281).strip() == "8.4G"
    assert human(512).strip() == "512B"
