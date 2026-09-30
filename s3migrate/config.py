"""Environment + runtime settings. Credentials come only from the .env file
(or process env) and are never persisted to the database or logs."""

import os
from dataclasses import dataclass, field

MiB = 1024 * 1024


def load_env(path):
    """Minimal .env parser — KEY=VALUE lines, # comments, no interpolation."""
    values = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip().strip('"').strip("'")
    # process environment wins over the file
    for key in ("STORAGE_HOST", "STORAGE_REGION", "STORAGE_BUCKET_NAME",
                "STORAGE_ACCESS_KEY", "STORAGE_SECRET_KEY",
                "SOURCE_DIR", "S3_PREFIX"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


class ConfigError(Exception):
    pass


@dataclass
class Settings:
    # S3 target
    bucket: str
    region: str
    access_key: str
    secret_key: str
    host: str = ""              # empty = AWS S3; else S3-compatible endpoint
    # migration
    source: str = ""
    prefix: str = ""            # normalised to "" or "path/"
    db_path: str = "registry.sqlite3"
    # behaviour
    batch_files: int = 100      # newly VERIFIED files per run; 0 = unlimited
    max_workers: int = 4
    multipart_threshold: int = 64 * MiB
    multipart_chunk: int = 64 * MiB
    verify: str = "checksum"    # size | checksum | readback
    excludes: list = field(default_factory=list)
    overwrite: bool = False
    dir_markers: bool = False   # materialise empty dirs as zero-byte "key/" objects

    @property
    def endpoint_url(self):
        if not self.host:
            return None
        return self.host if self.host.startswith("http") else f"https://{self.host}"

    def public_config(self):
        """Job configuration safe to persist — no credentials."""
        return {
            "bucket": self.bucket, "region": self.region,
            "endpoint": self.endpoint_url or "aws",
            "source": self.source, "prefix": self.prefix,
            "batch_files": self.batch_files, "max_workers": self.max_workers,
            "multipart_threshold": self.multipart_threshold,
            "multipart_chunk": self.multipart_chunk, "verify": self.verify,
            "excludes": self.excludes, "overwrite": self.overwrite,
            "dir_markers": self.dir_markers,
        }


def build_settings(args):
    env = load_env(args.env_file)
    missing = [v for v in ("STORAGE_REGION", "STORAGE_BUCKET_NAME",
                           "STORAGE_ACCESS_KEY", "STORAGE_SECRET_KEY")
               if not env.get(v)]
    if missing:
        raise ConfigError(f"missing in {args.env_file}: {', '.join(missing)}")

    source = getattr(args, "source", None) or env.get("SOURCE_DIR", "")
    prefix = getattr(args, "prefix", None)
    if prefix is None:
        prefix = env.get("S3_PREFIX", "")
    prefix = prefix.strip("/")
    prefix = f"{prefix}/" if prefix else ""

    return Settings(
        bucket=env["STORAGE_BUCKET_NAME"], region=env["STORAGE_REGION"],
        access_key=env["STORAGE_ACCESS_KEY"], secret_key=env["STORAGE_SECRET_KEY"],
        host=env.get("STORAGE_HOST", "").strip(),
        source=source, prefix=prefix,
        db_path=args.db,
        batch_files=getattr(args, "batch_files", 100),
        max_workers=getattr(args, "max_workers", 4),
        multipart_threshold=getattr(args, "multipart_threshold", 64) * MiB,
        multipart_chunk=getattr(args, "multipart_chunk", 64) * MiB,
        verify=getattr(args, "verify", "checksum"),
        excludes=list(getattr(args, "exclude", []) or []),
        overwrite=getattr(args, "overwrite", False),
        dir_markers=getattr(args, "dir_markers", False),
    )
