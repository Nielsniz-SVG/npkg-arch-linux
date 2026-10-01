"""Le repli « bibliothèque standard » face à l'API réelle de Python 3.14.

Python 3.14 — celui d'Arch Linux, dont ``python`` est en Core — expose
``compression.zstd`` avec ``decompress()``, ``ZstdFile()``,
``ZstdCompressor``/``ZstdDecompressor``, mais **ni** ``stream_decompress()`` **ni**
``decompressobj()`` : ce sont des facilités du paquet ``zstandard``. Un npkg qui
appellerait ``stream_decompress`` casse donc sur Arch ; ces tests figent le
contrat avec un module bouchon limité à la surface documentée en 3.14.

Le codec est simulé (deux « frames » concaténées) : ce qu'on vérifie ici est la
*sélection et l'usage de l'API*, y compris l'enchaînement des frames. La
décompression réelle est couverte par le bout-en-bout (NAR brut et NAR .xz).
"""

import io
import struct
import unittest
import zlib

from npkg import zstd as zstd_mod

MAGIC = b"NPZ1"


def fake_compress(data: bytes, chunk: int = 7) -> bytes:
    """Deux frames, chacune découpée en plusieurs blocs : piège pour les moqueries."""
    mid = len(data) // 2
    parts = [data[:mid], data[mid:]] if mid else [data]
    out = bytearray()
    for part in parts:
        blob = zlib.compress(part, 6)
        for offset in range(0, len(blob), chunk):
            block = blob[offset : offset + chunk]
            out += MAGIC + struct.pack("<I", len(block)) + block
        out += MAGIC + struct.pack("<I", 0)  # terminateur de frame
    return bytes(out)


def fake_decompress_all(data: bytes) -> bytes:
    out = bytearray()
    frame = bytearray()
    i = 0
    while i < len(data):
        if data[i : i + 4] != MAGIC:
            raise ValueError(f"cadre inconnu à l'offset {i}")
        (size,) = struct.unpack("<I", data[i + 4 : i + 8])
        i += 8
        frame += data[i : i + size]
        i += size
        if size == 0:
            out += zlib.decompress(bytes(frame))
            frame = bytearray()
    if frame:
        raise ValueError("frame tronquée")
    return bytes(out)


class StubPy314:
    """Uniquement ce que ``compression.zstd`` de Python 3.14 publie."""

    ZstdError = ValueError

    @staticmethod
    def decompress(data: bytes) -> bytes:
        return fake_decompress_all(bytes(data))

    class ZstdFile:
        """Flux décompressé avec une position, comme le vrai."""

        def __init__(self, fileobj, mode="rb"):
            if mode != "rb":
                raise ValueError("seule la lecture est simulée")
            self._data = fake_decompress_all(fileobj.read())
            self._pos = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size=-1):
            if size is None or size < 0:
                chunk, self._pos = self._data[self._pos :], len(self._data)
            else:
                chunk = self._data[self._pos : self._pos + size]
                self._pos += len(chunk)
            return chunk


class StubSansZstdFile:
    """Dernier repli : ``decompress()`` seulement (pas de lecture fluide)."""

    @staticmethod
    def decompress(data: bytes) -> bytes:
        return fake_decompress_all(bytes(data))


class TestBackendStdlib(unittest.TestCase):
    def patch(self, stub):
        original = zstd_mod._stdlib_zstd
        zstd_mod._stdlib_zstd = lambda: stub
        self.addCleanup(lambda: setattr(zstd_mod, "_stdlib_zstd", original))

    def test_surface_du_bouchon(self):
        self.assertFalse(hasattr(StubPy314, "stream_decompress"))
        self.assertFalse(hasattr(StubPy314, "decompressobj"))

    def test_backend_detecte_comme_stdlib(self):
        self.patch(StubPy314())
        self.assertEqual(zstd_mod.zstd_backend(), "stdlib")
        self.assertTrue(zstd_mod.available("zstd"))

    def test_multi_frames_via_zstdfile(self):
        self.patch(StubPy314())
        original = b"nix-archive-1" + bytes(256) * 40 + b"fin"
        out = io.BytesIO()
        written = zstd_mod.decompress_to_file(
            io.BytesIO(fake_compress(original)), out, "zstd", len(original)
        )
        self.assertEqual(out.getvalue(), original, "les deux frames doivent être recollées")
        self.assertEqual(written, len(original))

    def test_decompress_bytes(self):
        self.patch(StubPy314())
        original = b"bonjour zstd" * 100
        self.assertEqual(zstd_mod.decompress_bytes(fake_compress(original), "zstd"), original)

    def test_repli_bloc_entier_sans_zstdfile(self):
        self.patch(StubSansZstdFile())
        original = b"repli decompress()"
        out = io.BytesIO()
        written = zstd_mod.decompress_to_file(io.BytesIO(fake_compress(original)), out, "zstd")
        self.assertEqual(out.getvalue(), original)
        self.assertEqual(written, len(original))

    def test_xz_et_brut_nexigent_rien(self):
        import lzma

        payload = lzma.compress("données".encode())
        out = io.BytesIO()
        zstd_mod.decompress_to_file(io.BytesIO(payload), out, "xz")
        self.assertEqual(out.getvalue().decode(), "données")
        self.assertTrue(zstd_mod.available("none"))
        self.assertTrue(zstd_mod.available("xz"))


if __name__ == "__main__":
    unittest.main()
