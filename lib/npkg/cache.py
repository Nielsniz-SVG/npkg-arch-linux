"""Accès aux caches binaires Nix (``https://cache.nixos.org`` et semblables).

Un « substituter » est une base d'URL qui expose :

* ``<hashPart>.narinfo``  -> métadonnées + ``References`` (la fermeture !)
* le champ ``URL`` du narinfo -> ``nar/<narHash>.nar.zst``, l'archive compressée

npkg interroge les substituers dans l'ordre et s'arrête au premier qui connaît
le chemin. ``NPKG_SUBSTITUTERS`` (séparés par des espaces) remplace la liste par
défaut ; un chemin local ou ``file://`` fonctionne, ce qui permet de tester et
d'héberger un miroir sans réseau.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import http, narinfo as narinfo_mod, zstd
from .hashing import sha256_file, sha256_stream

DEFAULT_SUBSTITUTERS = ("https://cache.nixos.org",)
NARINFO_TTL = int(os.environ.get("NPKG_NARINFO_TTL", str(6 * 3600)))


def substituters(env=None) -> list[str]:
    env = os.environ if env is None else env
    raw = env.get("NPKG_SUBSTITUTERS", "").strip()
    if not raw:
        return list(DEFAULT_SUBSTITUTERS)
    return [item for item in raw.replace(",", " ").split() if item]


@dataclass
class FetchResult:
    store_path: str
    nar_file: Path
    nar_hash: str
    nar_size: int
    cache: str
    from_local: bool = False


class Substituter:
    def __init__(self, base: str, cache_dir: Path | None = None, ttl: int = NARINFO_TTL):
        self.base = base.rstrip("/")
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.ttl = ttl
        self._store_dir: str | None = None

    @property
    def name(self) -> str:
        return self.base.split("//", 1)[-1].rstrip("/")

    @property
    def store_dir(self) -> str:
        """``StoreDir:`` déclaré par le miroir (``nix-cache-info``), sinon /nix/store.

        Les ``References`` d'une narinfo sont relatives au store : il faut la
        valeur du miroir pour les rendre absolues, exactement ce que fait Nix.
        """
        if self._store_dir is None:
            try:
                text = http.get_text(http.join_url(self.base, "nix-cache-info"), timeout=10, retries=1)
                fields = {}
                for line in text.splitlines():
                    key, _, value = line.partition(":")
                    fields[key.strip()] = value.strip()
                self._store_dir = fields.get("StoreDir") or narinfo_mod.STORE_DIR
            except (http.FetchError, http.NotFound, OSError):
                self._store_dir = narinfo_mod.STORE_DIR
        return self._store_dir

    def info(self) -> dict:
        return {"base": self.base, "store_dir": self.store_dir}

    # -- narinfo -----------------------------------------------------------
    def _narinfo_file(self, digest: str) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"{digest}.json"

    def narinfo_text(self, digest: str, *, use_cache: bool = True) -> str:
        url = http.join_url(self.base, f"{digest}.narinfo")
        path = self._narinfo_file(digest)
        if use_cache and path and path.exists() and (time.time() - path.stat().st_mtime) < self.ttl:
            try:
                return json.loads(path.read_text())["text"]
            except (json.JSONDecodeError, KeyError, OSError):
                pass
        text = http.get_text(url)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"text": text, "url": url, "at": time.time()}))
        return text

    def lookup(self, digest: str) -> narinfo_mod.Narinfo | None:
        try:
            text = self.narinfo_text(digest)
        except http.NotFound:
            return None
        except http.FetchError:
            return None
        ni = narinfo_mod.parse(text, cache_url=self.base, store_dir=self.store_dir)
        return ni

    # -- NAR ---------------------------------------------------------------
    def nar_url(self, ni: narinfo_mod.Narinfo) -> str:
        if ni.url:
            return http.join_url(self.base, ni.url)
        stem = ni.nar_hash.split(":", 1)[-1]
        suffix = {"zstd": ".nar.zst", "xz": ".nar.xz", "bzip2": ".nar.bz2"}.get(
            ni.compression, ".nar"
        )
        return http.join_url(self.base, "nar", stem + suffix)

    def fetch(
        self,
        ni: narinfo_mod.Narinfo,
        dest_dir: Path,
        *,
        verify: bool = True,
        progress: bool = False,
    ) -> FetchResult:
        """Télécharge, vérifie et décompresse le NAR d'un chemin du store.

        Deux hashs sont contrôlés : celui de l'archive compressée
        (``FileHash``) et celui du NAR décompressé (``NarHash``) — c'est lui qui
        garantit le contenu réel du store.
        """
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        digest = ni.hash_part
        blob = dest_dir / f"{digest}.blob"
        nar_path = dest_dir / f"{digest}.nar"
        url = self.nar_url(ni)
        http.download(url, blob, expected_size=ni.file_size, progress=progress,
                      progress_label=ni.name)
        if verify and ni.file_hash:
            got = sha256_file(blob)
            if got != ni.file_hash:
                blob.unlink(missing_ok=True)
                raise ValueError(
                    f"FileHash divergent pour {ni.store_path}\n  attendu {ni.file_hash}\n  obtenu {got}"
                )
        if nar_path.exists():
            nar_path.unlink()
        compression = ni.compression or "none"
        if ni.url.endswith(".xz") and compression == "none":
            compression = "xz"
        if not zstd.available(compression):
            raise RuntimeError(
                f"impossible de décompresser {compression!r} : installez `zstd` "
                "ou `pip install zstandard`"
            )
        with blob.open("rb") as src, nar_path.open("wb") as out:
            written = zstd.decompress_to_file(src, out, compression, ni.nar_size)
        if ni.nar_size and written != ni.nar_size:
            raise ValueError(f"taille NAR divergente pour {ni.store_path} : {written} != {ni.nar_size}")
        if verify and ni.nar_hash:
            with nar_path.open("rb") as fh:
                got, _ = sha256_stream(fh)
            if got != ni.nar_hash:
                raise ValueError(
                    f"NarHash divergent pour {ni.store_path}\n  attendu {ni.nar_hash}\n  obtenu {got}"
                )
        blob.unlink(missing_ok=True)
        return FetchResult(
            store_path=ni.store_path,
            nar_file=nar_path,
            nar_hash=ni.nar_hash,
            nar_size=ni.nar_size or written,
            cache=self.base,
        )


class Substituters:
    def __init__(self, bases: list[str], cache_dir: Path | None = None):
        self.items = [Substituter(base, cache_dir) for base in bases]

    def __iter__(self):
        return iter(self.items)

    def lookup(self, digest: str, only: str | None = None) -> narinfo_mod.Narinfo | None:
        for sub in self.items:
            if only and only not in sub.base:
                continue
            ni = sub.lookup(digest)
            if ni is not None:
                return ni
        return None

    def fetch(self, ni: narinfo_mod.Narinfo, dest_dir: Path, **kw) -> FetchResult:
        for sub in self.items:
            if sub.base == ni.cache_url or ni.cache_url == "":
                return sub.fetch(ni, dest_dir, **kw)
        return self.items[0].fetch(ni, dest_dir, **kw)

    def describe(self) -> str:
        return ", ".join(sub.base for sub in self.items)
