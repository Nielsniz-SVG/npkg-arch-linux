"""Lecture des ``.narinfo`` : données réelles de cache.nixos.org."""

import unittest
from pathlib import Path

from npkg.narinfo import parse

FIXTURES = Path(__file__).parent / "fixtures"

SAMPLE = """StorePath: /nix/store/abc-hello-1.0
URL: nar/xyz.nar.xz
Compression: xz
FileHash: sha256:aaa
FileSize: 12
NarHash: sha256:bbb
NarSize: 34
References: def-glibc-2.40 abc-hello-1.0
Deriver: 789-hello-1.0.drv
Sig: cache.nixos.org-1:sig1= cache.nixos.org-1:sig2=
"""


class TestNarinfo(unittest.TestCase):
    def test_parse_champs(self):
        ni = parse(SAMPLE)
        self.assertEqual(ni.store_path, "/nix/store/abc-hello-1.0")
        self.assertEqual(ni.url, "nar/xyz.nar.xz")
        self.assertEqual(ni.compression, "xz")
        self.assertEqual(ni.file_size, 12)
        self.assertEqual(ni.nar_size, 34)
        self.assertEqual(ni.hash_part, "abc")
        self.assertEqual(ni.name, "hello-1.0")
        # les références sont normalisées en chemins absolus
        self.assertEqual(
            ni.references, ["/nix/store/def-glibc-2.40", "/nix/store/abc-hello-1.0"]
        )
        self.assertEqual(ni.deriver, "789-hello-1.0.drv")

    def test_references_sur_plusieurs_lignes_et_sigs(self):
        text = SAMPLE.replace(
            "References: def-glibc-2.40 abc-hello-1.0",
            "References: def-glibc-2.40\nReferences: abc-hello-1.0",
        )
        ni = parse(text)
        self.assertEqual(len(ni.references), 2)
        self.assertEqual(len(ni.sig), 2)

    def test_sans_refs_ni_taille(self):
        ni = parse("StorePath: /nix/store/x-p-1\n")
        self.assertEqual(ni.references, [])
        self.assertIsNone(ni.nar_size)
        self.assertEqual(ni.compression, "none")

    def test_storepath_manquant(self):
        with self.assertRaises(ValueError):
            parse("URL: nar/a.nar\n")

    def test_roundtrip_sur_fixture_reelle(self):
        path = FIXTURES / "hello.narinfo"
        if not path.exists():
            self.skipTest("fixture hello.narinfo absente")
        ni = parse(path.read_text())
        self.assertEqual(ni.nar_hash, "sha256:0vwm6sr61cx6hydqlx3phhg1a0830k61dbfwqlkhilkpcbppjmdw")
        self.assertEqual(ni.file_size, 75357)
        self.assertEqual(len(ni.references), 2)
        reserialized = parse(ni.to_text())
        self.assertEqual(reserialized.references, ni.references)
        self.assertEqual(reserialized.nar_hash, ni.nar_hash)
        self.assertEqual(reserialized.store_path, ni.store_path)


if __name__ == "__main__":
    unittest.main()
