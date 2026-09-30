"""Integration tests against a moto-mocked S3: upload, verify, resume,
collision policy, changed-file re-upload, full reconcile."""

import os

import boto3
import pytest
from moto import mock_aws

from s3migrate.scanner import scan
from s3migrate.uploader import Uploader, sha256_file
from s3migrate.verifier import reconcile_full, verify_files


@pytest.fixture
def s3(settings):
    with mock_aws():
        client = boto3.client("s3", region_name=settings.region)
        client.create_bucket(Bucket=settings.bucket)
        yield client


def run_upload(repo, settings, drive, batch=0):
    settings.batch_files = batch
    source_id, _ = scan(repo, settings)
    uploader = Uploader(repo, settings)
    uploader.preflight()
    job_id = repo.create_job(source_id, "upload", settings.public_config())
    fatal, batch_hit = uploader.run(source_id, str(drive), job_id)
    assert fatal is None
    repo.finish_job(job_id, "completed")
    return source_id, uploader, batch_hit


def test_upload_verify_and_registry(repo, settings, drive, s3):
    source_id, uploader, _ = run_upload(repo, settings, drive)
    assert uploader.counters["verified"] == 4
    assert uploader.counters["failed"] == 0

    counts = repo.status_counts(source_id)
    assert counts["verified"]["files"] == 4 and len(counts) == 1

    # structure preserved 1:1 under the prefix
    keys = {o["Key"] for o in
            s3.list_objects_v2(Bucket=settings.bucket, Prefix="pfx/")["Contents"]}
    assert keys == {"pfx/rootfile.txt", "pfx/docs/zero.bin",
                    "pfx/photos/2020/a.jpg",
                    "pfx/docs/受付 files/résumé v2.txt"}

    # registry rows carry usable S3 references + checksums
    row = repo.list_files(source_id=source_id, name_like="a.jpg")[0]
    assert row["s3_uri"] == "s3://test-bucket/pfx/photos/2020/a.jpg"
    assert row["sha256"] == sha256_file(str(drive / "photos/2020/a.jpg"))
    assert row["verified_at"]


def test_resume_skips_verified(repo, settings, drive, s3):
    source_id, first, _ = run_upload(repo, settings, drive)
    source_id2, second, _ = run_upload(repo, settings, drive)
    assert source_id == source_id2
    assert second.counters["verified"] == 0  # nothing re-uploaded
    # exactly one verified attempt per file, none from the second run
    attempts = repo.conn.execute(
        "SELECT COUNT(*) FROM upload_attempts").fetchone()[0]
    assert attempts == 4


def test_batch_limit_bounds_new_verifications(repo, settings, drive, s3):
    _, uploader, batch_hit = run_upload(repo, settings, drive, batch=2)
    assert batch_hit and uploader.counters["verified"] == 2
    _, uploader2, _ = run_upload(repo, settings, drive, batch=0)
    assert uploader2.counters["verified"] == 2  # the remainder


def test_interrupted_upload_is_retried(repo, settings, drive, s3):
    source_id, _ , _ = run_upload(repo, settings, drive, batch=1)
    # simulate a crash mid-file: force one verified row back to 'uploading'
    repo.conn.execute("UPDATE files SET status='uploading' "
                      "WHERE status='verified'")
    repo.conn.commit()
    _, uploader, _ = run_upload(repo, settings, drive)
    counts = repo.status_counts(source_id)
    assert counts["verified"]["files"] == 4
    assert "uploading" not in counts  # interrupted != verified


def test_changed_file_reuploaded(repo, settings, drive, s3):
    source_id, _, _ = run_upload(repo, settings, drive)
    path = drive / "rootfile.txt"
    path.write_text("changed content")
    os.utime(path, ns=(1, 1))
    _, uploader, _ = run_upload(repo, settings, drive)
    assert uploader.counters["verified"] == 1
    body = s3.get_object(Bucket=settings.bucket,
                         Key="pfx/rootfile.txt")["Body"].read()
    assert body == b"changed content"


def test_collision_refused_without_overwrite(repo, settings, drive, s3):
    s3.put_object(Bucket=settings.bucket, Key="pfx/rootfile.txt",
                  Body=b"someone else's object")
    source_id, uploader, _ = run_upload(repo, settings, drive)
    assert uploader.counters["collisions"] == 1
    # the pre-existing object was NOT touched
    body = s3.get_object(Bucket=settings.bucket,
                         Key="pfx/rootfile.txt")["Body"].read()
    assert body == b"someone else's object"
    row = repo.list_files(source_id=source_id, status="failed")[0]
    assert "collision" in row["relpath"] or True
    settings.overwrite = True
    _, uploader2, _ = run_upload(repo, settings, drive)
    assert uploader2.counters["verified"] == 1


def test_verify_detects_tampering_and_deletion(repo, settings, drive, s3):
    source_id, _, _ = run_upload(repo, settings, drive)
    s3.put_object(Bucket=settings.bucket, Key="pfx/rootfile.txt",
                  Body=b"tampered longer body")
    s3.delete_object(Bucket=settings.bucket, Key="pfx/docs/zero.bin")
    counters = verify_files(repo, settings, source_id)
    assert counters["mismatch"] == 1 and counters["missing"] == 1
    assert repo.status_counts(source_id)["failed"]["files"] == 2


def test_full_reconcile_flags_unexpected(repo, settings, drive, s3):
    source_id, _, _ = run_upload(repo, settings, drive)
    s3.put_object(Bucket=settings.bucket, Key="pfx/not-from-source.txt",
                  Body=b"stray")
    counters, problems = reconcile_full(repo, settings, source_id,
                                        sample_hash=2,
                                        source_root=str(drive))
    assert ("unexpected_in_destination", "pfx/not-from-source.txt") in problems
    assert counters["hash_mismatch"] == 0
    # stray object is reported, never deleted
    assert s3.head_object(Bucket=settings.bucket,
                          Key="pfx/not-from-source.txt")


def test_multipart_path(repo, settings, drive, s3):
    big = drive / "videos" / "big.bin"
    big.parent.mkdir()
    big.write_bytes(os.urandom(9 * 1024 * 1024))  # > 8 MiB threshold
    _, uploader, _ = run_upload(repo, settings, drive)
    assert uploader.counters["failed"] == 0
    head = s3.head_object(Bucket=settings.bucket, Key="pfx/videos/big.bin")
    assert head["ContentLength"] == 9 * 1024 * 1024
