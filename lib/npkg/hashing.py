"""Hashage façon Nix : encodage base32 « nix base-32 » et hasheurs.

Nix n'utilise pas le base32 de RFC 4648 mais un alphabet ordonné par fréquence
dans les chemins du store (alphabetic order of the digest bits), sans padding.
Cet algorithme est indispensable pour vérifier les champs ``FileHash`` /
``NarHash`` des ``.narinfo`` et pour retrouver le *hashPart* d'un chemin du
store.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO

# alphabet nix : 0-9 a b c d f g h i j k l m n p q r s v w x y z (ni e, o, t, u)
NIX_BASE32_ALPHABET = "0123456789abcdfghijklmnpqrsvwxyz"
_SHA256_DIGITS = 52  # ceil(256 / 5)


def base32_nix_encode(digest: bytes) -> str:
    """Encode un digest en base32 Nix (52 caractères pour sha256)."""
    nbytes = len(digest)
    digits = (nbytes * 8 + 4) // 5
    out = []
    for i in range(0, digits * 5, 5):
        acc = 0
        for j in range(5):
            bit = i + j
            if bit // 8 < nbytes:
                acc |= ((digest[bit // 8] >> (bit % 8)) & 1) << j
        out.append(NIX_BASE32_ALPHABET[acc])
    return "".join(reversed(out))


def base32_nix_decode(text: str, nbytes: int) -> bytes:
    """Décode un hash base32 Nix en ``nbytes`` octets (inverse de l'encodeur)."""
    digits = len(text)
    acc = 0
    for shift, char in enumerate(reversed(text)):
        value = NIX_BASE32_ALPHABET.index(char)
        acc |= value << (5 * shift)
    need = ((digits * 5) + 7) // 8
    raw = acc.to_bytes(need, "little")
    if len(raw) < nbytes:
        raw = raw + b"\x00" * (nbytes - len(raw))
    return raw[:nbytes]


def is_valid_hash_part(part: str) -> bool:
    if len(part) != 32:
        return False
    return all(c in NIX_BASE32_ALPHABET for c in part)


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + base32_nix_encode(hashlib.sha256(data).digest())


def sha256_file(path: Path | str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return "sha256:" + base32_nix_encode(h.digest())


def sha256_stream(fh: BinaryIO, tee: BinaryIO | None = None, chunk: int = 1 << 20) -> tuple[str, int]:
    """Hashe un flux ; si ``tee`` est fourni, y recopie les octets lus.

    Utilisé pour vérifier le ``NarHash`` d'un NAR tout en l'écrivant sur
    disque, sans jamais garder l'archive en mémoire.
    """
    h = hashlib.sha256()
    total = 0
    while True:
        block = fh.read(chunk)
        if not block:
            break
        h.update(block)
        total += len(block)
        if tee is not None:
            tee.write(block)
    return "sha256:" + base32_nix_encode(h.digest()), total


def hash_part_of(store_path: str) -> str:
    """/nix/store/<hashPart>-<name> -> <hashPart>"""
    base = store_path.rstrip("/").rsplit("/", 1)[-1]
    return base.split("-", 1)[0]
