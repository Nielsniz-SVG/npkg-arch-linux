"""Le format NAR : lecture, extraction, réécriture à l'identique.

Le test décisif porte sur un **vrai** NAR de nixpkgs (``hello-2.12.3`` dépaqueté
de ``cache.nixos.org``) : on le lit, on l'extrait sur disque, on le re-sérialise,
et on exige l'octet près ce qu'avait produit ``nix-store --dump``.
"""

import io
import os
import tempfile
import unittest
from pathlib import Path

from npkg import nar
from npkg.hashing import sha256_bytes

FIXTURES = Path(__file__).parent / "fixtures"


def make_tree(root: Path) -> None:
    (root / "bin").mkdir()
    (root / "bin" / "prog").write_text("#!/bin/sh\necho hi\n")
    os.chmod(root / "bin" / "prog", 0o755)
    (root / "share").mkdir()
    (root / "share" / "note.txt").write_text("données")
    (root / "latest").symlink_to("share")
    (root / "empty").mkdir()


class TestSynthese(unittest.TestCase):
    def test_roundtrip_arbre(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            make_tree(src)
            blob = nar.dump_dir(src)
            tree = nar.read_tree(io.BytesIO(blob))
            flat = dict(nar.walk_tree(tree))
            self.assertEqual(sorted(flat), ["/bin", "/bin/prog", "/empty", "/latest", "/share", "/share/note.txt"])
            self.assertTrue(flat["/bin/prog"]["executable"])
            self.assertEqual(flat["/bin/prog"]["contents"], b"#!/bin/sh\necho hi\n")
            self.assertEqual(flat["/latest"]["type"], "symlink")
            self.assertEqual(flat["/latest"]["target"], "share")
            self.assertEqual(flat["/empty"], {"type": "directory", "entries": {}})

    def test_extraction_pose_droits_et_liens(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            make_tree(src)
            out = Path(tmp) / "out"
            stats = nar.extract(io.BytesIO(nar.dump_dir(src)), out)
            self.assertTrue(os.access(out / "bin" / "prog", os.X_OK))
            self.assertEqual((out / "share" / "note.txt").read_text(), "données")
            self.assertTrue((out / "latest").is_symlink())
            self.assertEqual(os.readlink(out / "latest"), "share")
            self.assertEqual((out / "empty").is_dir(), True)
            self.assertEqual(stats["files"], 2)
            self.assertEqual(stats["execs"], 1)
            self.assertEqual(stats["symlinks"], 1)

    def test_nom_dentree_dangereux_refuse(self):
        blob = bytearray()
        nar._write_str(blob, nar.MAGIC)
        nar._write_str(blob, "(")
        nar._write_str(blob, "entry")
        nar._write_str(blob, "(")
        nar._write_str(blob, "name")
        nar._write_str(blob, "../evil")
        nar._write_str(blob, "node")
        nar._write_str(blob, "(")
        nar._write_str(blob, "type")
        nar._write_str(blob, "regular")
        nar._write_str(blob, "contents")
        blob += (0).to_bytes(8, "little")
        nar._write_str(blob, ")")
        nar._write_str(blob, ")")
        nar._write_str(blob, ")")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(nar.NarError):
                nar.extract(io.BytesIO(bytes(blob)), Path(tmp) / "out")

    def test_nar_tronque(self):
        with self.assertRaises(nar.NarError):
            nar.read_tree(io.BytesIO(b"\x0d\x00\x00\x00\x00\x00\x00\x00nix"))


class TestNarReel(unittest.TestCase):
    def setUp(self):
        self.nar_file = FIXTURES / "hello.nar"
        if not self.nar_file.exists():
            self.skipTest("fixture tests/fixtures/hello.nar absente")
        self.data = self.nar_file.read_bytes()

    def test_hachage_figure(self):
        self.assertEqual(
            sha256_bytes(self.data),
            "sha256:0vwm6sr61cx6hydqlx3phhg1a0830k61dbfwqlkhilkpcbppjmdw",
        )

    def test_arbre_attendu(self):
        tree = nar.read_tree(io.BytesIO(self.data))
        flat = dict(nar.walk_tree(tree))
        files = {rel: node for rel, node in flat.items() if node["type"] == "regular"}
        # le paquet = 1 binaire ELF, la doc GNU, 46 fichiers .mo, 1 page de manuel
        self.assertEqual(len(files), 49)
        self.assertEqual(len([1 for _, n in flat.items() if n["type"] == "directory"]), 98)
        self.assertTrue(files["/bin/hello"]["executable"])
        self.assertEqual(files["/bin/hello"]["contents"][:4], b"\x7fELF")
        self.assertEqual(len(files["/bin/hello"]["contents"]), 64472)
        self.assertFalse(files["/share/man/man1/hello.1.gz"]["executable"])
        self.assertIn("/share/locale/fr/LC_MESSAGES/hello.mo", files)
        self.assertNotIn("/share/locale/xx", files)

    def test_reproductibilite_octet_par_octet(self):
        """extraction puis re-dump doit rendre exactement le NAR d'origine."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "zi2bj2hlavv8q743li2s9diqbcpmrf9b-hello-2.12.3"
            nar.extract(io.BytesIO(self.data), out)
            again = nar.dump_dir(out)
            self.assertEqual(len(again), len(self.data))
            self.assertEqual(again, self.data, "le NAR réécrit diffère de celui de nix")


if __name__ == "__main__":
    unittest.main()
