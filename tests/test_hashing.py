"""Tests du codage base32 Nix et des hashs.

Deux sources d'attente, indépendantes de l'implémentation :

* ``sha256("")`` : la valeur hex de la bibliothèque standard est confrontée au
  décodage base32 Nix (le test relit son propre encodage *vers* l'hex),
* une ``.narinfo`` réelle de ``cache.nixos.org`` (``hello-2.12.3``) : son
  ``NarHash`` doit correspondre au hash du NAR du dépôt ``tests/fixtures``.
"""

import hashlib
import unittest
from pathlib import Path

from npkg.hashing import (
    NIX_BASE32_ALPHABET,
    base32_nix_decode,
    base32_nix_encode,
    hash_part_of,
    is_valid_hash_part,
    sha256_bytes,
    sha256_file,
)

FIXTURES = Path(__file__).parent / "fixtures"

HELLO_STORE_PATH = "/nix/store/zi2bj2hlavv8q743li2s9diqbcpmrf9b-hello-2.12.3"
HELLO_NAR_HASH = "sha256:0vwm6sr61cx6hydqlx3phhg1a0830k61dbfwqlkhilkpcbppjmdw"
HELLO_FILE_HASH = "sha256:02f66k40d4vlmzg53wh6b6hz82w0bqmg6jj18ldjq33v4y8f1sl0"


class TestBase32(unittest.TestCase):
    def test_alphabet_est_celui_de_nix(self):
        self.assertEqual(len(NIX_BASE32_ALPHABET), 32)
        for forbidden in "eotu":  # nix exclut e, o, t, u pour lever les ambiguïtés
            self.assertNotIn(forbidden, NIX_BASE32_ALPHABET)

    def test_digest_zero(self):
        self.assertEqual(base32_nix_encode(bytes(32)), "0" * 52)

    def test_sha256_vide_contre_hex_stdlib(self):
        digest = hashlib.sha256(b"").digest()
        text = base32_nix_encode(digest)
        self.assertEqual(len(text), 52)
        self.assertEqual(base32_nix_decode(text, 32), digest)
        self.assertEqual(
            text,
            "0mdqa9w1p6cmli6976v4wi0sw9r4p5prkj7lzfd1877wk11c9c73",
            "sha256(\"\") encodé en base32 Nix",
        )

    def test_encode_decode_aleatoire(self):
        for seed in range(32):
            digest = hashlib.sha256(str(seed).encode()).digest()
            self.assertEqual(base32_nix_decode(base32_nix_encode(digest), 32), digest)

    def test_hash_part(self):
        self.assertEqual(hash_part_of(HELLO_STORE_PATH), "zi2bj2hlavv8q743li2s9diqbcpmrf9b")
        self.assertTrue(is_valid_hash_part("zi2bj2hlavv8q743li2s9diqbcpmrf9b"))
        self.assertFalse(is_valid_hash_part("Z" * 32))
        self.assertFalse(is_valid_hash_part("0" * 31))


class TestHashing(unittest.TestCase):
    def test_format(self):
        got = sha256_bytes(b"")
        self.assertTrue(got.startswith("sha256:"))
        self.assertEqual(len(got), len("sha256:") + 52)

    def test_nar_reel_de_nixpkgs(self):
        nar = FIXTURES / "hello.nar"
        if not nar.exists():
            self.skipTest("fixture hello.nar absente")
        self.assertEqual(sha256_file(nar), HELLO_NAR_HASH)
        self.assertEqual(nar.stat().st_size, 279624)


if __name__ == "__main__":
    unittest.main()
