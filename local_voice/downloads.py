"""Resumable HTTPS transfers with certificate validation and bounded retries."""
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def download(url, target, *, attempts=3, progress=None, deadline=1800, cancel=None):
    if not url.startswith("https://"):
        raise ValueError("Downloads require HTTPS.")
    last_error = None
    for attempt in range(attempts):
        if cancel is not None and cancel.is_set():
            raise RuntimeError("Download cancelled. Partial data was kept.")
        offset = target.stat().st_size if target.exists() else 0
        request = Request(url, headers={"Range": f"bytes={offset}-"} if offset else {})
        started = time.monotonic()
        try:
            with urlopen(request, timeout=30) as response:
                if response.geturl().split(":", 1)[0] != "https":
                    raise ValueError("Download redirected outside HTTPS.")
                resumed = response.status == 206
                if resumed:
                    match = re.match(r"bytes (\d+)-\d+/", response.headers.get("Content-Range", ""))
                    if not match or int(match[1]) != offset:
                        target.unlink(missing_ok=True)
                        raise OSError("Server returned an invalid resume range.")
                with target.open("ab" if resumed else "wb") as handle:
                    received = offset if resumed else 0
                    length = response.headers.get("Content-Length")
                    total = received + int(length) if length is not None else None
                    announced = received
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise RuntimeError("Download cancelled. Partial data was kept.")
                        chunk = response.read(512 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        received += len(chunk)
                        if progress and received - announced >= 16 * 1024 * 1024:
                            if total:
                                progress(f"Downloaded {received / 1024**2:.0f} of {total / 1024**2:.0f} MB ({min(100, received * 100 // total)}%)")
                            else:
                                progress(f"Downloaded {received / 1024**2:.0f} MB…")
                            announced = received
                        if time.monotonic() - started > deadline:
                            raise TimeoutError("Download deadline reached; partial data was kept for retry.")
                    expected = response.headers.get("Content-Length")
                    if expected is not None and received - (offset if resumed else 0) != int(expected):
                        raise OSError("Download ended before all bytes arrived.")
                    if progress and total:
                        progress(f"Downloaded {received / 1024**2:.0f} of {total / 1024**2:.0f} MB (100%)")
            return
        except HTTPError as exc:
            if exc.code == 416:
                target.unlink(missing_ok=True)
            elif exc.code < 500 and exc.code not in (408, 429):
                raise RuntimeError(f"Download server returned HTTP {exc.code}.") from exc
            last_error = exc
        except (OSError, URLError, TimeoutError) as exc:
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(min(2 ** attempt, 4))
    raise RuntimeError(f"Download failed after {attempts} attempts; retry to resume. {last_error}") from last_error
