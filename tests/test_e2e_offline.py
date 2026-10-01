from __future__ import annotations  # (python 3.9 dans les tests aussi)

"""Bout-en-bout hors-ligne : npkg installe depuis un cache Nix factice.

On construit un vrai miroir binaire au format de ``cache.nixos.org``
(``nix-cache-info``, ``<hashPart>.narinfo``, ``nar/<NarHash>.nar``) avec deux
paquets dont l'un dépend de l'autre, puis on roule la CLI complète :
épinglage, installation, fermeture, profil, vérification, réinstallation après
corruption, désinstallation, gc, rollback.

Aucun réseau, aucun Nix : c'est le code de production (résolution, hachage,
décompression, extraction NAR, état, profils) qui est exercé de bout en bout.
"""

import io
import json
import os
import contextlib
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from npkg import nar
from npkg.hashing import base32_nix_encode
from npkg.cli import main as cli_main

STORE_DIRNAME = "store"


def fake_digest(name: str) -> str:
    """32 caractères de l'alphabet nix, comme un vrai hashPart."""
    return base32_nix_encode(sha256(name.encode()).digest())[:32]


def build_package(
    cache: Path,
    store: Path,
    name: str,
    version: str,
    files: dict[str, tuple[str, bool]],
    refs: list[str],
    symlinks: dict[str, str] | None = None,
    compression: str = "none",
) -> str:
    """Écrit un paquet dans le store temporaire et son double dans le cache."""
    digest = fake_digest(f"{name}-{version}")
    path = store / f"{digest}-{name}-{version}"
    path.mkdir(parents=True, exist_ok=True)
    for rel, (content, executable) in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        os.chmod(target, 0o755 if executable else 0o644)
    for rel, target in (symlinks or {}).items():
        link = path / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    nar_bytes = nar.dump_dir(path)
    nar_hash = "sha256:" + base32_nix_encode(sha256(nar_bytes).digest())
    stem = nar_hash.split(":", 1)[1]
    suffix = {"none": ".nar", "xz": ".nar.xz", "zstd": ".nar.zst"}[compression]
    blob = nar_bytes if compression == "none" else _compress(nar_bytes, compression)
    (cache / "nar").mkdir(parents=True, exist_ok=True)
    (cache / "nar" / f"{stem}{suffix}").write_bytes(blob)
    reference_names = " ".join(Path(ref).name for ref in ([f"{path}"] + refs))
    # le store ne doit PAS contenir le paquet : npkg doit aller le chercher
    # dans le cache, comme face à un vrai miroir
    import shutil as _shutil

    _shutil.rmtree(path)
    (cache / f"{digest}.narinfo").write_text(
        "StorePath: {store}/{digest}-{name}-{version}\n"
        "URL: nar/{stem}{suffix}\n"
        "Compression: {compression}\n"
        "FileHash: {file_hash}\n"
        "FileSize: {blob_size}\n"
        "NarHash: {nar_hash}\n"
        "NarSize: {size}\n"
        "References: {refs}\n".format(
            store=store, digest=digest, name=name, version=version, stem=stem,
            nar_hash=nar_hash, size=len(nar_bytes), refs=reference_names,
            suffix=suffix, compression=compression,
            file_hash="sha256:" + base32_nix_encode(sha256(blob).digest()),
            blob_size=len(blob),
        )
    )
    return str(path)


def _compress(data: bytes, kind: str) -> bytes:
    """Fabrique l'archive du cache comme le ferait `nix copy --to s3://…`."""
    if kind == "xz":
        import lzma

        return lzma.compress(data)
    if kind == "zstd":
        try:
            import zstandard
        except ImportError:
            raise unittest.SkipTest("zstandard requis pour écrire un nar.zst")
        return zstandard.ZstdCompressor().compress(data)
    raise AssertionError(f"compression inattendue : {kind}")


@contextlib.contextmanager
def environment(home: Path, store: Path, cache: Path):
    saved = {k: os.environ.get(k) for k in ("NPKG_HOME", "NPKG_STORE_DIR", "NPKG_SUBSTITUTERS", "NPKG_OFFLINE")}
    os.environ.update(
        NPKG_HOME=str(home),
        NPKG_STORE_DIR=str(store),
        NPKG_SUBSTITUTERS=str(cache),
        NPKG_OFFLINE="1",
    )
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = cli_main(argv)
    return code, buffer.getvalue()


class OfflineInstallTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="npkg-e2e-"))
        cls.home = cls.tmp / "state"
        cls.store = cls.tmp / STORE_DIRNAME
        cls.cache = cls.tmp / "cache"
        cls.store.mkdir(parents=True)
        cls.cache.mkdir(parents=True)
        (cls.cache / "nix-cache-info").write_text(f"StoreDir: {cls.store}\nWantMassQuery: 1\nPriority: 40\n")
        cls.lib_path = build_package(
            cls.cache,
            cls.store,
            "libgreet",
            "1.0",
            {"lib/libgreet.so": ("/* libgreet */\n", False), "include/greet.h": ("int greet(void);\n", False)},
            [],
        )
        (cls.home / "profiles" / "generations").mkdir(parents=True, exist_ok=True)
        cls.greet_path = build_package(
            cls.cache,
            cls.store,
            "greet",
            "2.0",
            {
                "bin/greet": ("#!/bin/sh\necho hello\n", True),
                "bin/greet-legacy": ("vieux binaire\n", True),
                "share/man/man1/greet.1": ("greet(1)\n", False),
            },
            [cls.lib_path],
            # un paquet peut exporter un lien vers une dépendance : le NAR doit le porter
            symlinks={"lib": f"{cls.lib_path}/lib"},
        )
        # épingles écrites d'emblée : chaque test peut tourner isolément
        (cls.home / "pins.json").write_text(
            json.dumps({"greet": cls.greet_path, "libgreet": cls.lib_path}, indent=1) + "\n"
        )

    # ---------------------------------------------------------------- helpers
    def profile_bin(self, *parts) -> Path:
        gens = sorted((self.home / "profiles" / "generations").glob("default-*"))
        return gens[-1].joinpath(*parts)

    def read_state(self) -> dict:
        state = self.home / "state.json"
        if not state.exists():
            return {"roots": {}}
        return json.loads(state.read_text())

    def read_valid(self) -> dict:
        return json.loads((self.home / "valid.json").read_text())["valid"]

    # ------------------------------------------------------------------ tests
    def test_a_install(self):
        with environment(self.home, self.store, self.cache):
            code, out = run(["pin", "greet", self.greet_path])
            self.assertEqual(code, 0, out)
            code, out = run(["pin", "libgreet", self.lib_path])
            self.assertEqual(code, 0, out)
            code, out = run(["install", "greet"])
            self.assertEqual(code, 0, out)

            # les deux chemins sont dépaquetés (fermeture suivie)
            self.assertTrue((Path(self.lib_path) / "lib" / "libgreet.so").exists())
            binary = Path(self.greet_path) / "bin" / "greet"
            self.assertTrue(binary.exists())
            self.assertTrue(os.access(binary, os.X_OK), "le bit +x doit venir du NAR")
            self.assertEqual(binary.read_text(), "#!/bin/sh\necho hello\n")
            # liens symboliques du paquet conservés
            self.assertTrue((Path(self.greet_path) / "lib").is_symlink())

            # profil : les commandes sont exposées
            linked = self.profile_bin("bin", "greet")
            self.assertTrue(linked.is_symlink())
            self.assertEqual(os.path.realpath(str(linked)), os.path.realpath(str(binary)))
            self.assertTrue(self.profile_bin("bin", "greet-legacy").exists())
            # mais pas lib/ ni share/
            self.assertFalse(self.profile_bin("share", "man").exists())

            state = self.read_state()
            self.assertIn("greet", state["roots"])
            root = state["roots"]["greet"]
            self.assertEqual(root["store_path"], self.greet_path)
            self.assertEqual(root["version"], "2.0")
            self.assertEqual(sorted(Path(p).name for p in root["closure"]),
                             sorted([Path(self.greet_path).name, Path(self.lib_path).name]))
            valid = self.read_valid()
            self.assertIn(self.greet_path, valid)
            self.assertIn(self.lib_path, valid)
            self.assertEqual(valid[self.greet_path]["refs"], [self.lib_path])

    def test_b_list_json_et_status(self):
        with environment(self.home, self.store, self.cache):
            code, out = run(["list", "--json"])
            self.assertEqual(code, 0, out)
            listing = json.loads(out)
            self.assertEqual(list(listing), ["greet"])
            code, out = run(["status", "--json"])
            status = json.loads(out)
            self.assertEqual(code, 0, out)
            self.assertEqual(status["roots"], ["greet"])
            self.assertEqual(status["generation"], 1)
            self.assertFalse(status["nix_store_layout"])
            self.assertEqual(status["store_paths"], 2)
            self.assertGreater(status["store_bytes"], 0)
            code, out = run(["size", "greet"])
            self.assertEqual(code, 0, out)

    def test_c_closure_et_tree(self):
        with environment(self.home, self.store, self.cache):
            code, out = run(["closure", "greet", "--json"])
            self.assertEqual(code, 0, out)
            data = json.loads(out)
            self.assertEqual(sorted(Path(p).name for p in data["paths"]),
                             sorted([Path(self.greet_path).name, Path(self.lib_path).name]))
            self.assertGreater(data["bytes"], 0)
            code, out = run(["tree", "greet", "--depth", "3"])
            self.assertEqual(code, 0, out)
            self.assertIn("libgreet-1.0", out)
            code, out = run(["which", "greet"])
            self.assertEqual(code, 0, out)
            self.assertIn(os.path.realpath(self.greet_path), out)

    def test_d_verify_ok_puis_corruption_puis_reinstall(self):
        with environment(self.home, self.store, self.cache):
            code, out = run(["verify", "greet"])
            self.assertEqual(code, 0, out)
            self.assertIn("NarHash recalculé identique", out)

            binaire = Path(self.greet_path) / "bin" / "greet"
            binaire.write_text("#!/bin/sh\necho pwned\n")
            code, out = run(["verify", "greet"])
            self.assertEqual(code, 1, f"la corruption doit être détectée :\n{out}")
            self.assertIn("NarHash divergent", out)

            code, out = run(["reinstall", "greet"])
            self.assertEqual(code, 0, out)
            self.assertIn("refait", out)
            self.assertEqual(binaire.read_text(), "#!/bin/sh\necho hello\n")
            code, out = run(["verify", "greet"])
            self.assertEqual(code, 0, out)

    def test_e_dry_run_nechange_rien(self):
        with environment(self.home, self.store, self.cache):
            before = sorted(p.name for p in self.store.iterdir())
            code, out = run(["--dry-run", "install", "greet"])
            self.assertEqual(code, 0, out)
            self.assertIn("mode --dry-run", out)
            self.assertEqual(sorted(p.name for p in self.store.iterdir()), before)

    def test_f_remove_gc_rollback(self):
        with environment(self.home, self.store, self.cache):
            avant = json.loads(run(["history", "--json"])[1])
            code, out = run(["remove", "greet"])
            self.assertEqual(code, 0, out)
            self.assertFalse(self.profile_bin("bin", "greet").exists() or self.profile_bin("bin", "greet").is_symlink())
            # rien n'a disparu du store avant le gc
            self.assertTrue(Path(self.greet_path).exists())

            code, out = run(["gc", "--dry-run"])
            self.assertEqual(code, 0, out)
            self.assertIn("2 chemin(s) libérables", out)
            self.assertTrue(Path(self.greet_path).exists())

            code, out = run(["gc"])
            self.assertEqual(code, 0, out)
            self.assertFalse(Path(self.greet_path).exists())
            self.assertFalse(Path(self.lib_path).exists())
            self.assertEqual(list(self.store.iterdir()), [])

            # le rollback ramène le profil à la génération d'avant, même store vide
            code, out = run(["rollback"])
            self.assertEqual(code, 0, out)
            # on revient à la génération qui était active avant le remove
            cible = max(item["generation"] for item in avant)
            self.assertIn(f"génération {cible}", out)
            after = json.loads(run(["history", "--json"])[1])
            current = [item for item in after if item["current"]]
            self.assertEqual(current[0]["generation"], cible)
            self.assertTrue(
                any("greet-2.0" in r for r in current[0]["roots"]),
                "le profil restauré doit de nouveau exposer greet",
            )
            code, out = run(["history"])
            self.assertEqual(code, 0, out)
            self.assertIn("*", out)

    def test_i_nar_compresse_xz(self):
        """Le même paquet, publié en ``nar.xz`` : décompression + double hachage."""
        packed = build_package(
            self.cache, self.store, "greet-xz", "3.0",
            {"bin/greet-xz": ("#!/bin/sh\necho compressé\n", True)}, [],
            compression="xz",
        )
        with environment(self.home, self.store, self.cache):
            self.assertEqual(run(["pin", "greet-xz", packed])[0], 0)
            code, out = run(["install", "greet-xz"])
            self.assertEqual(code, 0, out)
            self.assertTrue(Path(packed).is_dir(), packed)
            self.assertEqual(
                (Path(packed) / "bin" / "greet-xz").read_text(), "#!/bin/sh\necho compressé\n"
            )
            code, out = run(["verify", "greet-xz"])
            self.assertEqual(code, 0, out)
            run(["remove", "greet-xz"])
            run(["gc"])

    def test_h_gc_epargne_les_chemins_dautrui(self):
        """Un chemin posé par quelqu'un d'autre (Nix, un humain) n'est jamais supprimé."""
        foreign = self.store / f"{fake_digest('foreign')}-trousse-a-outils-9.9"
        (foreign / "bin").mkdir(parents=True)
        (foreign / "bin" / "outil").write_text("important\n")
        with environment(self.home, self.store, self.cache):
            code, out = run(["gc", "--dry-run"])
            self.assertEqual(code, 0, out)
            self.assertIn("inconnus de npkg", out)
            code, out = run(["gc"])
            self.assertEqual(code, 0, out)
            self.assertTrue(
                (foreign / "bin" / "outil").exists(),
                "npkg ne doit pas détruire ce qu'il n'a pas déposé lui-même",
            )
        import shutil

        shutil.rmtree(foreign, ignore_errors=True)

    def test_g_conflit_de_profil_signale(self):
        """Deux paquets qui exposent le même ``bin/x`` : le profil garde le premier, et le dit."""
        other = build_package(
            self.cache, self.store, "greet-alt", "1.0",
            {"bin/greet": ("#!/bin/sh\necho autre greet\n", True)}, [],
        )
        with environment(self.home, self.store, self.cache):
            self.assertEqual(run(["pin", "greet-alt", other])[0], 0)
            if "greet" not in self.read_state()["roots"]:
                self.assertEqual(run(["install", "greet"])[0], 0)

            code, out = run(["install", "greet-alt"])
            self.assertEqual(code, 0, out)
            self.assertIn("conflit de profil : bin/greet", out)

            keep = Path(self.greet_path) / "bin" / "greet"
            self.assertEqual(
                os.path.realpath(str(self.profile_bin("bin", "greet"))),
                os.path.realpath(str(keep)),
                "le profil ne doit pas être volé par le nouveau venu",
            )
            # le second paquet reste utilisable par son chemin du store
            self.assertEqual(
                (Path(other) / "bin" / "greet").read_text(), "#!/bin/sh\necho autre greet\n"
            )
            run(["remove", "greet-alt"])
            run(["gc"])

    def test_gc_ne_touche_pas_au_vivant(self):
        with environment(self.home, self.store, self.cache):
            code, out = run(["install", "greet"])
            self.assertEqual(code, 0, out)
            code, out = run(["gc", "--dry-run"])
            self.assertEqual(code, 0, out)
            self.assertIn("0 chemin(s) libérables", out)
            self.assertTrue(Path(self.lib_path).exists(), "une dépendance en usage ne doit pas être collectée")
            self.assertTrue(Path(self.greet_path).exists())


if __name__ == "__main__":
    unittest.main()
