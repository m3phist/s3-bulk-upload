import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from s3migrate.config import Settings  # noqa: E402
from s3migrate.repository import Repository  # noqa: E402


@pytest.fixture
def repo(tmp_path):
    r = Repository(str(tmp_path / "registry.sqlite3"))
    yield r
    r.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        bucket="test-bucket", region="us-east-1",
        access_key="testing", secret_key="testing",
        source=str(tmp_path / "drive"), prefix="pfx/",
        db_path=str(tmp_path / "registry.sqlite3"),
        multipart_threshold=8 * 1024 * 1024,
        multipart_chunk=8 * 1024 * 1024,
    )


@pytest.fixture
def drive(tmp_path):
    """A little fake external drive with the awkward cases."""
    root = tmp_path / "drive"
    (root / "docs" / "受付 files").mkdir(parents=True)
    (root / "photos" / "2020").mkdir(parents=True)
    (root / "empty-dir").mkdir()
    (root / "docs" / "受付 files" / "résumé v2.txt").write_text("unicode")
    (root / "docs" / "zero.bin").write_bytes(b"")
    (root / "photos" / "2020" / "a.jpg").write_bytes(b"jpg")
    (root / "rootfile.txt").write_text("root")
    (root / ".DS_Store").write_bytes(b"junk")
    (root / "._resource").write_bytes(b"junk")
    try:  # symlinks need privileges on Windows; the scanner records, not follows
        os.symlink("/etc/hosts", root / "docs" / "a-symlink")
    except OSError:
        pass
    return root
