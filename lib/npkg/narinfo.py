"""Lecture/écriture du format ``.narinfo`` des caches binaires Nix.

Exemple de fichier réel (cache.nixos.org)::

    StorePath: /nix/store/zi2bj2hlavv8q743li2s9diqbcpmrf9b-hello-2.12.3
    URL: nar/0vwm6sr61cx6hydqlx3phhg1a0830k61dbfwqlkhilkpcbppjmdw.nar.zst
    Compression: zstd
    FileHash: sha256:02f66k40d4vlmzg53wh6b6hz82w0bqmg6jj18ldjq33v4y8f1sl0
    FileSize: 75357
    NarHash: sha256:0vwm6sr61cx6hydqlx3phhg1a0830k61dbfwqlkhilkpcbppjmdw
    NarSize: 279624
    References: 57iz36553175g3178pvxjij8z5rcsd4n-glibc-2.42-61 zi2bj2...-hello-2.12.3
    Deriver: 67mdzby3g0maqqp93xj03rc99nnrpdp9-hello-2.12.3.drv
    Sig: cache.nixos.org-1:DwOHEUMyxq4aUrwzcZJrPPqqlImdA7042VJ+HWOnyjZe...

``References`` est la liste des chemins du store *directement* référencés :
c'est à partir de là que l'on calcule la fermeture (closure) d'un paquet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# StoreDir canonique des caches publics ; un miroir peut en déclarer un autre
# (champ `StoreDir` de nix-cache-info) : voir npkg.cache.Substituter.store_dir.
STORE_DIR = "/nix/store"

_KEYRE = re.compile(r"^([A-Za-z0-9-]+):\s?(.*)$")


@dataclass
class Narinfo:
    store_path: str
    url: str = ""
    compression: str = "none"
    file_hash: str = ""
    file_size: int | None = None
    nar_hash: str = ""
    nar_size: int | None = None
    nar_bytes: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    deriver: str = ""
    system: str = ""
    sig: list[str] = field(default_factory=list)
    ca: str = ""
    cache_url: str = ""  # d'où vient ce narinfo (informatif)
    store_dir: str = STORE_DIR  # StoreDir déclaré par le miroir source

    # -- accès pratiques -------------------------------------------------
    @property
    def hash_part(self) -> str:
        base = self.store_path.rsplit("/", 1)[-1]
        return base.split("-", 1)[0]

    @property
    def name(self) -> str:
        base = self.store_path.rsplit("/", 1)[-1]
        return base.split("-", 1)[1] if "-" in base else base

    def as_dict(self) -> dict:
        return {
            "StorePath": self.store_path,
            "URL": self.url,
            "Compression": self.compression,
            "FileHash": self.file_hash,
            "FileSize": "" if self.file_size is None else str(self.file_size),
            "NarHash": self.nar_hash,
            "NarSize": "" if self.nar_size is None else str(self.nar_size),
            "References": " ".join(_strip_store_dir(r, self.store_dir) for r in self.references),
            "Deriver": self.deriver,
            "System": self.system,
            "Sig": "\nSig: ".join(self.sig),
            "CA": self.ca,
        }

    def to_text(self) -> str:
        lines = []
        for key, val in self.as_dict().items():
            if val == "" or val is None:
                continue
            lines.append(f"{key}: {val}")
        return "\n".join(lines) + "\n"


def _strip_store_dir(path: str, store_dir: str = STORE_DIR) -> str:
    if path.startswith(store_dir.rstrip("/") + "/"):
        return path[len(store_dir.rstrip("/")) + 1 :]
    return path


def parse(text: str, cache_url: str = "", store_dir: str = STORE_DIR) -> Narinfo:
    refs: list[str] = []
    sigs: list[str] = []
    fields: dict[str, list[str]] = {}
    for raw in text.splitlines():
        m = _KEYRE.match(raw.strip())
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        fields.setdefault(key, []).append(value)
    if "StorePath" not in fields:
        raise ValueError("narinfo sans StorePath")
    for bucket in fields.get("References", []):
        refs.extend(p for p in bucket.split() if p)
    for bucket in fields.get("Sig", []):
        sigs.extend(p for p in bucket.split() if p)

    def one(key: str, default: str = "") -> str:
        vals = fields.get(key) or []
        return vals[0] if vals else default

    def num(key: str) -> int | None:
        val = one(key)
        return int(val) if val.isdigit() else None

    return Narinfo(
        store_path=one("StorePath"),
        url=one("URL"),
        compression=one("Compression", "none") or "none",
        file_hash=one("FileHash"),
        file_size=num("FileSize"),
        nar_hash=one("NarHash"),
        nar_size=num("NarSize"),
        nar_bytes=fields.get("NarBytes", []),
        references=[r if r.startswith("/") else f"{store_dir.rstrip('/')}/{r}" for r in refs],
        deriver=one("Deriver"),
        system=one("System"),
        sig=sigs,
        ca=one("CA"),
        cache_url=cache_url,
        store_dir=store_dir,
    )


