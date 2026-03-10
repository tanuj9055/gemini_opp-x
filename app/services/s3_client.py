"""
Amazon S3 download helper.

Downloads objects from S3 URLs (s3://bucket/key) into local temp files.
Uses boto3 with asyncio.to_thread to avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path
from typing import List, Tuple
from urllib.parse import urlparse, unquote

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

from app.config import get_settings
from app.logging_cfg import logger

_log = logger.getChild("s3_client")

# ────────────────────────────────────────────────────────
# S3 client singleton
# ────────────────────────────────────────────────────────

_s3_client = None


def _get_s3_client():
    """Lazy-init a boto3 S3 client.

    Credentials are resolved by the standard boto3 chain:
      1. Environment variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
      2. Shared credentials file (~/.aws/credentials)
      3. IAM role (if running on EC2/ECS/Lambda)
    Optionally, the AWS region can be set via AWS_REGION in Settings.
    """
    global _s3_client
    if _s3_client is None:
        settings = get_settings()
        kwargs = {}
        if settings.aws_region:
            kwargs["region_name"] = settings.aws_region
        if settings.aws_access_key_id and settings.aws_secret_access_key:
            kwargs["aws_access_key_id"] = settings.aws_access_key_id
            kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
        _s3_client = boto3.client("s3", **kwargs)
        _log.info("S3 client initialised (region=%s)", settings.aws_region or "default")
    return _s3_client


# ────────────────────────────────────────────────────────
# URL parsing
# ────────────────────────────────────────────────────────

def is_s3_url(url: str) -> bool:
    """Return True if *url* uses the ``s3://`` protocol (requires boto3/credentials).

    HTTPS S3 URLs (e.g. ``https://bucket.s3.amazonaws.com/key``) are treated
    as plain HTTP downloads so that public buckets work without credentials.
    """
    return url.strip().startswith("s3://")


def parse_s3_url(url: str) -> Tuple[str, str]:
    """Parse an ``s3://bucket/key`` URL into ``(bucket, key)``.

    Also supports ``https://bucket.s3.amazonaws.com/key`` and
    ``https://bucket.s3.region.amazonaws.com/key`` style URLs.
    """
    url = url.strip()

    # s3://bucket/key
    if url.startswith("s3://"):
        parsed = urlparse(url)
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        if not bucket or not key:
            raise ValueError(f"Invalid S3 URL (missing bucket or key): {url}")
        return bucket, key

    # https://bucket.s3.amazonaws.com/key  or  https://bucket.s3.region.amazonaws.com/key
    if "s3" in url and "amazonaws.com" in url:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        path = parsed.path.lstrip("/")

        # Virtual-hosted: bucket.s3.amazonaws.com  or  bucket.s3.region.amazonaws.com
        m = re.match(r"^(.+?)\.s3[.-]", host)
        if m:
            return m.group(1), unquote(path)

        # Path-style: s3.amazonaws.com/bucket/key
        parts = path.split("/", 1)
        if len(parts) == 2:
            return parts[0], unquote(parts[1])

    raise ValueError(
        f"Unsupported URL format: {url}. Expected s3://bucket/key or "
        f"https://bucket.s3.amazonaws.com/key"
    )


# ────────────────────────────────────────────────────────
# Download helpers
# ────────────────────────────────────────────────────────

async def _download_http(url: str, dest_dir: Path) -> Path:
    """Download a file from a plain HTTP(S) URL."""
    import urllib.request
    import ssl

    parsed = urlparse(url)
    filename = Path(unquote(parsed.path)).name or "document.pdf"
    # Ensure the file has a proper extension (GeM portal URLs like
    # /showbidDocument/8434342 have none, but serve PDFs).
    if not Path(filename).suffix:
        filename = f"{filename}.pdf"
    local_path = dest_dir / filename

    _log.info("Downloading (HTTP) %s → %s", url, local_path)

    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "QistonPe-GemAudit/1.0"})

    def _do_download():
        with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
            with open(local_path, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)

    await asyncio.to_thread(_do_download)
    _log.info("Download complete: %s (%.1f KB)", local_path, local_path.stat().st_size / 1024)
    return local_path


async def download_file(url: str, dest_dir: Path) -> Path:
    """Download a single file to ``dest_dir``.

    Supports S3 URLs (``s3://``) and plain HTTP(S) URLs
    (e.g. GeM portal links or public S3 HTTPS URLs).

    Returns the local ``Path`` to the downloaded file.
    """
    if not is_s3_url(url):
        return await _download_http(url, dest_dir)

    bucket, key = parse_s3_url(url)
    filename = Path(key).name or "document.pdf"
    local_path = dest_dir / filename

    _log.info("Downloading s3://%s/%s → %s", bucket, key, local_path)

    client = _get_s3_client()
    try:
        await asyncio.to_thread(
            client.download_file,
            Bucket=bucket,
            Key=key,
            Filename=str(local_path),
        )
    except NoCredentialsError as exc:
        _log.error("AWS credentials not configured: %s", exc)
        raise RuntimeError(
            "AWS credentials are not configured. Set AWS_ACCESS_KEY_ID and "
            "AWS_SECRET_ACCESS_KEY environment variables, or configure an IAM role."
        ) from exc
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        _log.error("S3 download failed [%s]: s3://%s/%s – %s", error_code, bucket, key, exc)
        raise RuntimeError(
            f"Failed to download s3://{bucket}/{key}: {error_code} – {exc}"
        ) from exc

    _log.info("Download complete: %s (%.1f KB)", local_path, local_path.stat().st_size / 1024)
    return local_path


async def download_files(urls: List[str], dest_dir: Path) -> List[Path]:
    """Download multiple files from S3 concurrently.

    Returns a list of local ``Path`` objects in the same order as ``urls``.
    Handles filename collisions by prefixing an index.
    """
    # Prefix with index to avoid collisions when multiple files have the same name
    async def _download_indexed(idx: int, url: str) -> Path:
        if not is_s3_url(url):
            # Plain HTTP(S) URL – download directly, prefix for uniqueness
            downloaded = await _download_http(url, dest_dir)
            unique_path = dest_dir / f"{idx:02d}_{downloaded.name}"
            downloaded.rename(unique_path)
            return unique_path

        bucket, key = parse_s3_url(url)
        filename = Path(key).name or f"document_{idx}.pdf"
        unique_filename = f"{idx:02d}_{filename}"
        local_path = dest_dir / unique_filename

        _log.info("Downloading [%d] s3://%s/%s → %s", idx, bucket, key, local_path)

        client = _get_s3_client()
        try:
            await asyncio.to_thread(
                client.download_file,
                Bucket=bucket,
                Key=key,
                Filename=str(local_path),
            )
        except (NoCredentialsError, ClientError) as exc:
            _log.error("S3 download failed for %s: %s", url, exc)
            raise RuntimeError(f"Failed to download {url}: {exc}") from exc

        return local_path

    tasks = [_download_indexed(i, url) for i, url in enumerate(urls)]
    return await asyncio.gather(*tasks)
