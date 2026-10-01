"""Décompression des ``nar.zst`` / ``nar.xz`` / ``nar.bz2`` des caches binaires Nix.

Aucune dépendance obligatoire : on tente dans l'ordre

1. ``compression.zstd`` (bibliothèque standard depuis Python 3.14 — c'est le cas
   d'Arch Linux, dont le ``python`` de Core est en 3.14),
2. ``zstandard`` (paquet PyPI, très courant),
3. le binaire ``zstd`` / ``unzstd`` système (``zstd`` n'est PAS une dépendance du
   paquet ``base`` d'Arch : le vérifier avec ``pacman -Q zstd``),
4. ``lzma`` / ``bz2`` de la bibliothèque standard pour les caches ``nar.xz``.

Formes d'API divergentes, d'où les replis ci-dessous : ``compression.zstd``
expose ``decompress()`` et ``ZstdFile()`` mais ni ``stream_decompress()`` ni
``decompressobj()``, contrairement à ``zstandard``. ``ZstdFile`` (comme
``decompress()``) enchaîne les frames concaténées, ce que produisent certains
miroirs ; ``ZstdDecompressor`` seul s'arrête à la fin de la première.

``npkg doctor`` indique lequel est retenu.
"""

from __future__ import annotations

import bz2
import lzma
import shutil
import subprocess
from typing import BinaryIO

SUPPORTED = ("none", "", "zstd", "xz", "bzip2")


class DecompressionUnavailable(RuntimeError):
    pass


def _stdlib_zstd():
    try:  # Python >= 3.14
        from compression import zstd as _z  # type: ignore

        return _z
    except Exception:
        return None


def _pyzstd():
    try:
        import zstandard  # type: ignore

        return zstandard
    except Exception:
        return None


def zstd_backend() -> str:
    """``stdlib`` | ``zstandard`` | ``cli`` | ``none``."""
    if _stdlib_zstd() is not None:
        return "stdlib"
    if _pyzstd() is not None:
        return "zstandard"
    if shutil.which("zstd") or shutil.which("unzstd"):
        return "cli"
    return "none"


def available(compression: str) -> bool:
    if compression in ("none", ""):
        return True
    if compression == "zstd":
        return zstd_backend() != "none"
    return compression in ("xz", "bzip2")


def decompress_bytes(data: bytes, compression: str, expected_size: int | None = None) -> bytes:
    """Décompresse un bloc entier (utile pour les petits NAR et les tests)."""
    if compression in ("none", ""):
        return data
    if compression == "zstd":
        std = _stdlib_zstd()
        if std is not None:
            return std.decompress(data)
        pz = _pyzstd()
        if pz is not None:
            maxsize = expected_size if expected_size else max(len(data) * 40, 1 << 20)
            return pz.ZstdDecompressor().decompress(data, max_output_size=maxsize)
        return _cli_decompress(data, expected_size)
    if compression == "xz":
        return lzma.LZMADecompressor().decompress(data)
    if compression == "bzip2":
        return bz2.BZ2Decompressor().decompress(data)
    raise DecompressionUnavailable(f"compression inconnue : {compression!r}")


def _cli_decompress(data: bytes, expected_size: int | None) -> bytes:
    exe = shutil.which("zstd") or shutil.which("unzstd")
    if not exe:
        raise DecompressionUnavailable(
            "zstd indisponible : installez le paquet `zstd`, `pip install zstandard`, "
            "ou utilisez Python >= 3.14"
        )
    proc = subprocess.run(
        [exe, "-d", "-c"], input=data, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, check=True,
    )
    return proc.stdout


def decompress_to_file(
    src: BinaryIO, dst: BinaryIO, compression: str, expected_size: int | None = None
) -> int:
    """Décompresse un flux vers un fichier et renvoie la taille écrite.

    Les caches Nix empilent parfois plusieurs frames (``Compression: zstd`` en
    mode *source*, plusieurs fichiers compressés concaténés) : on boucle donc
    tant qu'il reste des données, en s'arrêtant à ``NarSize`` quand il est connu.
    """
    if compression in ("none", ""):
        return _copy(src, dst)
    if compression == "zstd":
        std = _stdlib_zstd()
        if std is not None:
            return _stdlib_stream(std, src, dst)
        pz = _pyzstd()
        if pz is not None:
            return _feed_chunks(src, dst, pz.ZstdDecompressor().decompressobj(), expected_size)
        blob = src.read()
        data = _cli_decompress(blob, expected_size)
        dst.write(data)
        return len(data)
    if compression in ("xz", "bzip2"):
        decomp = lzma.LZMADecompressor() if compression == "xz" else bz2.BZ2Decompressor()
        return _feed_chunks(src, dst, decomp, expected_size)
    raise DecompressionUnavailable(f"compression inconnue : {compression!r}")


def _stdlib_stream(std, src: BinaryIO, dst: BinaryIO) -> int:
    """Décompression fluide avec ``compression.zstd`` (Python >= 3.14).

    Ordre de préférence : ``ZstdFile`` (lit un objet fichier, enchaîne les
    frames), puis ``stream_decompress`` si une version le fournit, puis
    ``decompress()`` sur le bloc entier en dernier repli.
    """
    zfile = getattr(std, "ZstdFile", None)
    if callable(zfile):
        try:
            with zfile(src, "rb") as fh:
                return _copy(fh, dst)
        except (AttributeError, OSError, EOFError, TypeError, ValueError):
            pass  # signature ou contenu divergent : on tente la suite
    stream = getattr(std, "stream_decompress", None)
    if callable(stream):
        return _copy(stream(src), dst)
    blob = src.read()
    data = std.decompress(blob)
    dst.write(data)
    return len(data)


def _feed_chunks(src: BinaryIO, dst: BinaryIO, decomp, expected_size: int | None) -> int:
    written = 0
    while True:
        chunk = src.read(1 << 20)
        if not chunk:
            break
        out = decomp.decompress(chunk)
        if out:
            dst.write(out)
            written += len(out)
        if expected_size and written >= expected_size:
            break
        if getattr(decomp, "eof", False):
            break
    return written


def _copy(src: BinaryIO, dst: BinaryIO) -> int:
    written = 0
    while True:
        chunk = src.read(1 << 20)
        if not chunk:
            break
        dst.write(chunk)
        written += len(chunk)
    return written
