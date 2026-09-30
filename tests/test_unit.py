"""Unit tests: config, key mapping, scanner, repository state machine."""

import os

import pytest

from s3migrate.config import load_env
from s3migrate.db import migrate
from s3migrate.scanner import (extension_of, is_excluded, is_junk,
                               native_path, rel_to_key, scan, to_posix_rel)


def test_load_env(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nSTORAGE_REGION=ap-southeast-5\n"
                   'STORAGE_BUCKET_NAME="quoted"\nBROKEN LINE\n')
    values = load_env(str(env))
    assert values["STORAGE_REGION"] == "ap-southeast-5"
    assert values["STORAGE_BUCKET_NAME"] == "quoted"
    assert "BROKEN LINE" not in values


def test_rel_to_key_preserves_structure_and_case():
    rel = to_posix_rel(os.path.join("Clients", "Client-A", "2024.PDF"))
    assert rel_to_key(rel, "backup/") == "backup/Clients/Client-A/2024.PDF"
    assert rel_to_key("a.txt", "") == "a.txt"


def test_rel_to_key_rejects_escape():
    with pytest.raises(ValueError):
        rel_to_key("../outside.txt", "")


def test_native_path_roundtrip(tmp_path):
    rel = to_posix_rel(os.path.join("a b", "c", "d.txt"))
    assert rel == "a b/c/d.txt"
    assert native_path(str(tmp_path), rel) == \
        os.path.join(str(tmp_path), "a b", "c", "d.txt")


def test_extension_of():
    assert extension_of("a.PDF") == "pdf"
    assert extension_of("archive.tar.gz") == "gz"
    assert extension_of("noext") == ""
    assert extension_of(".env") == ""


def test_junk_and_excludes():
    assert is_junk(".DS_Store") and is_junk("._resource")
    assert not is_junk("real.txt")
    # a directory is pruned at its own level, so children are never visited
    assert is_excluded("sub/.venv", ".venv", [".venv"])
    assert not is_excluded("sub/lib", "lib", [".venv"])
    assert is_excluded("x/big.log", "big.log", ["*.log"])


def test_migrations_are_idempotent(repo):
    assert migrate(repo.conn) == []  # already applied by Repository()


def test_scan_populates_registry(repo, settings, drive):
    has_symlink = (drive / "docs" / "a-symlink").is_symlink()
    source_id, counters = scan(repo, settings)
    assert counters["files"] == 4
    assert counters["symlinks"] == (1 if has_symlink else 0)
    assert counters["empty_dirs"] == 1
    assert counters["errors"] == 0

    rows = repo.list_files(source_id=source_id, limit=100)
    rels = {r["relpath"] for r in rows}
    assert "docs/受付 files/résumé v2.txt" in rels  # POSIX form, any OS
    assert not any(".DS_Store" in r or "._resource" in r for r in rels)

    row = repo.list_files(source_id=source_id, name_like="a.jpg")[0]
    assert row["s3_uri"] == "s3://test-bucket/pfx/photos/2020/a.jpg"

    empties = [r["relpath"] for r in repo.empty_directories(source_id)]
    assert empties == ["empty-dir"]
    if has_symlink:
        specials = repo.specials(source_id)
        assert [(s["kind"], s["relpath"]) for s in specials] == \
            [("symlink", "docs/a-symlink")]
        assert specials[0]["target"] == "/etc/hosts"


def test_rescan_is_idempotent_and_detects_change(repo, settings, drive):
    source_id, first = scan(repo, settings)
    assert first["new"] == 4

    # verified file stays verified across an unchanged rescan
    row = repo.list_files(source_id=source_id, name_like="rootfile")[0]
    file_id = repo.conn.execute("SELECT id FROM files WHERE relpath=?",
                                (row["relpath"],)).fetchone()["id"]
    repo.set_file_status(file_id, "verified", sha256="ab" * 32, verified=True)

    _, second = scan(repo, settings)
    assert second["new"] == 0 and second["changed"] == 0
    assert repo.get_file(file_id)["status"] == "verified"

    # touching the file re-marks it 'changed' and clears verification
    path = drive / "rootfile.txt"
    path.write_text("root v2!")
    os.utime(path, ns=(1, 1))
    _, third = scan(repo, settings)
    assert third["changed"] == 1
    updated = repo.get_file(file_id)
    assert updated["status"] == "changed"
    assert updated["sha256"] is None and updated["verified_at"] is None


def test_upsert_file_returns_same_row(repo, settings, drive):
    source_id, _ = scan(repo, settings)
    before = repo.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    scan(repo, settings)
    after = repo.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    assert before == after == 4


def test_list_files_filters(repo, settings, drive):
    source_id, _ = scan(repo, settings)
    assert len(repo.list_files(source_id=source_id, extension="jpg")) == 1
    assert len(repo.list_files(source_id=source_id, extension=".JPG")) == 1
    assert len(repo.list_files(source_id=source_id, path_like="photos")) == 1
    assert len(repo.list_files(source_id=source_id, min_size=1)) == 3
    assert len(repo.list_files(source_id=source_id, status="discovered")) == 4


def test_empty_source_refused(repo, settings, tmp_path):
    os.makedirs(settings.source, exist_ok=True)
    with pytest.raises(RuntimeError):
        scan(repo, settings)


def test_missing_source_refused(repo, settings):
    settings.source = str(settings.source) + "-nonexistent"
    with pytest.raises(FileNotFoundError):
        scan(repo, settings)


def test_job_and_attempt_records(repo, settings, drive):
    source_id, _ = scan(repo, settings)
    job_id = repo.create_job(source_id, "upload", settings.public_config())
    row = repo.conn.execute("SELECT * FROM upload_jobs WHERE id=?",
                            (job_id,)).fetchone()
    assert row["status"] == "running"
    assert "secret" not in (row["config_json"] or "").lower()

    file_id = repo.conn.execute("SELECT id FROM files LIMIT 1").fetchone()["id"]
    a1 = repo.start_attempt(file_id, job_id)
    repo.finish_attempt(a1, "failed", error="boom")
    a2 = repo.start_attempt(file_id, job_id)
    attempts = repo.conn.execute(
        "SELECT attempt, outcome FROM upload_attempts WHERE file_id=? "
        "ORDER BY attempt", (file_id,)).fetchall()
    assert [(a["attempt"], a["outcome"]) for a in attempts] == \
        [(1, "failed"), (2, None)]

    repo.finish_job(job_id, "completed", files_verified=3)
    row = repo.conn.execute("SELECT * FROM upload_jobs WHERE id=?",
                            (job_id,)).fetchone()
    assert row["status"] == "completed" and row["files_verified"] == 3
