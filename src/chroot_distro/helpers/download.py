import hashlib
import os
import time
import urllib.error
import urllib.request

from chroot_distro.atomic import atomic_replace
from chroot_distro.constants import PROGRAM_NAME, PROGRAM_VERSION
from chroot_distro.message import log_error, log_info, msg, warn
from chroot_distro.progress import clear_bar, draw_bytes_bar, fmt_size

__all__ = ("download_file", "sha256_file")

_MAX_RETRIES = 3
_RETRY_DELAYS = (1, 2, 4)  # seconds between retries (exponential backoff)


def _is_retriable(exc: BaseException) -> bool:
    """Return True for transient server or connection failures."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    return isinstance(exc, ConnectionError) or (
        isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, ConnectionError)
    )


def sha256_file(path: str) -> str:
    """Compute and return the SHA-256 hex digest of *path*, with a progress bar."""
    h = hashlib.sha256()
    total = os.path.getsize(path)
    processed = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            processed += len(chunk)
            draw_bytes_bar(processed, total, noun="processed")
    clear_bar()
    return h.hexdigest()


def download_file(url: str, dest: str) -> None:
    """Download *url* to *dest* with progress output, redirects, and retries."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"{PROGRAM_NAME}/{PROGRAM_VERSION}"},
    )
    last_exc: BaseException | None = None
    for attempt in range(_MAX_RETRIES + 1):
        if attempt > 0:
            delay = _RETRY_DELAYS[attempt - 1]
            warn(f"Retry {attempt}/{_MAX_RETRIES} in {delay}s (reason: {last_exc})...")
            time.sleep(delay)

        try:
            with atomic_replace(dest) as tmp, urllib.request.urlopen(req) as resp, open(tmp, "wb") as fh:
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
                    downloaded += len(chunk)
                    draw_bytes_bar(downloaded, total, noun="downloaded")
                fh.flush()
                os.fsync(fh.fileno())
            clear_bar()
            log_info(f"Finished downloading ({fmt_size(downloaded)}).")
            return
        except KeyboardInterrupt:
            clear_bar()
            raise
        except BaseException as exc:
            clear_bar()
            if _is_retriable(exc) and attempt < _MAX_RETRIES:
                last_exc = exc
                continue
            msg()
            log_error("Download failure, please check your network connection.")
            raise RuntimeError(f"Cannot download {url}: {exc}") from exc
