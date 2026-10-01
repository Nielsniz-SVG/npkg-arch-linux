"""Téléchargements HTTP minimaux (bibliothèque standard uniquement).

* retry + backoff exponentiel,
* timeout par requête,
* support de ``file://`` et des chemins bruts — indispensable pour les tests
  hors-ligne et pour pointer sur un miroir local,
* enregistrement dans le cache ``~/.cache/npkg`` pour ne pas repayer un
  NAR à chaque ``npkg gc`` / ``npkg verify``.
"""

from __future__ import annotations

import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_TIMEOUT = float(os.environ.get("NPKG_HTTP_TIMEOUT", "30"))
RETRIES = int(os.environ.get("NPKG_HTTP_RETRIES", "3"))
USER_AGENT = os.environ.get("NPKG_USER_AGENT", "npkg/0.1 (+https://github.com/npkg/npkg)")


class FetchError(RuntimeError):
    pass


class NotFound(FetchError):
    pass


def is_local(url: str) -> bool:
    return url.startswith("file://") or url.startswith("/")


def _local_path(url: str) -> Path:
    if url.startswith("file://"):
        parsed = urllib.parse.urlparse(url)
        return Path(urllib.parse.unquote(parsed.path))
    return Path(url)


def open_stream(url: str, timeout: float | None = None):
    """Renvoie un objet fichier binaire positionné au début."""
    if is_local(url):
        path = _local_path(url)
        if not path.exists():
            raise NotFound(f"fichier absent : {path}")
        return path.open("rb")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        return urllib.request.urlopen(req, timeout=timeout or DEFAULT_TIMEOUT)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise NotFound(f"404 : {url}") from exc
        raise FetchError(f"HTTP {exc.code} : {url}") from exc
    except (urllib.error.URLError, socket.timeout) as exc:
        raise FetchError(f"{type(exc).__name__} : {url}") from exc


def get_text(url: str, timeout: float | None = None, retries: int | None = None) -> str:
    last: Exception | None = None
    for attempt in range(1, (retries or RETRIES) + 1):
        try:
            with open_stream(url, timeout) as fh:
                return fh.read().decode("utf-8", "replace")
        except NotFound:
            raise
        except FetchError as exc:
            last = exc
            if attempt < (retries or RETRIES):
                time.sleep(min(2 ** attempt * 0.4, 6))
    raise last or FetchError(url)  # pragma: no cover - garde-fou


def download(
    url: str,
    dest: Path | str,
    *,
    expected_size: int | None = None,
    progress: bool = False,
    progress_label: str = "",
) -> int:
    """Télécharge ``url`` vers ``dest`` ; renvoie le nombre d'octets écrits."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    last: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        written = 0
        try:
            with open_stream(url) as src, tmp.open("wb") as out:
                while True:
                    block = src.read(1 << 20)
                    if not block:
                        break
                    out.write(block)
                    written += len(block)
                    if progress:
                        _progress(written, expected_size, progress_label)
            if progress and expected_size and sys.stderr.isatty():
                sys.stderr.write("\r" + " " * 79 + "\r")
                sys.stderr.flush()
            if expected_size and written != expected_size:
                raise FetchError(
                    f"taille inattendue pour {url} : {written} octets, attendu {expected_size}"
                )
            os.replace(tmp, dest)
            return written
        except NotFound:
            tmp.unlink(missing_ok=True)
            raise
        except FetchError as exc:
            last = exc
        except OSError as exc:  # disque plein, réseau coupé en cours...
            last = FetchError(f"{exc}")
        if attempt < RETRIES:
            time.sleep(min(2 ** attempt * 0.4, 6))
    tmp.unlink(missing_ok=True)
    raise last or FetchError(url)  # pragma: no cover


def _progress(done: int, total: int | None, label: str) -> None:
    if not sys.stderr.isatty():
        return
    if total:
        pct = 100.0 * done / total
        bar = "#" * int(pct / 4)
        sys.stderr.write(f"\r  {label[:28]:<28} [{bar:<25}] {pct:5.1f}% {done/1e6:.1f} Mo")
    else:
        sys.stderr.write(f"\r  {label[:28]:<28} {done/1e6:.1f} Mo")
    sys.stderr.flush()


def join_url(base: str, *parts: str) -> str:
    if is_local(base) and not base.startswith("file://"):
        return str(Path(base).joinpath(*parts))
    return base.rstrip("/") + "/" + "/".join(p.lstrip("/") for p in parts)
