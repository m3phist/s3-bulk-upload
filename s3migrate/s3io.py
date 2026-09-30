"""S3 client construction and low-level object helpers."""

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

# Errors that mean the whole run must stop, not just one file
FATAL_S3_CODES = {
    "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch",
    "ExpiredToken", "TokenRefreshRequired", "NoSuchBucket",
    "PermanentRedirect", "301", "403",
}


def make_client(settings):
    kwargs = {
        "region_name": settings.region,
        "aws_access_key_id": settings.access_key,
        "aws_secret_access_key": settings.secret_key,
        # standard/adaptive retry modes use exponential backoff with jitter
        "config": BotoConfig(retries={"max_attempts": 10, "mode": "adaptive"}),
    }
    if settings.endpoint_url:
        kwargs["endpoint_url"] = settings.endpoint_url
    return boto3.client("s3", **kwargs)


def make_transfer_config(settings):
    return TransferConfig(
        multipart_threshold=settings.multipart_threshold,
        multipart_chunksize=settings.multipart_chunk,
        max_concurrency=4,
    )


def error_code(exc):
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code", "")
    return ""


def whole_object_sha256(head):
    """The remote SHA-256 usable as a whole-file hash, or None.

    Multipart uploads carry a COMPOSITE checksum ('...-N' or
    ChecksumType=COMPOSITE) — a hash of part hashes, never comparable to a
    whole-file digest."""
    remote = head.get("ChecksumSHA256", "")
    if not remote or "-" in remote:
        return None
    checksum_type = head.get("ChecksumType")
    if checksum_type and checksum_type != "FULL_OBJECT":
        return None
    # a multipart ETag carries a "-<parts>" suffix; its checksum is composite
    # even when the response omits ChecksumType
    if "-" in head.get("ETag", "").strip('"'):
        return None
    return remote


def head_or_none(client, bucket, key):
    try:
        return client.head_object(Bucket=bucket, Key=key,
                                  ChecksumMode="ENABLED")
    except ClientError as exc:
        if error_code(exc) in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
