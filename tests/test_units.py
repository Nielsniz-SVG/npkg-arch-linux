"""Tests unitaires des briques pures (aucun disque, aucun réseau)."""

import shutil
import tempfile
import unittest
from pathlib import Path

from npkg.resolver import Resolver, split_spec
from npkg.state import Home, Profiles, Root, State
from npkg.store import Store, ValidEntry, ValidIndex, dir_size, human_size, parse_store_path


class TestStorePaths(unittest.TestCase):
    def test_parse_valide(self):
        digest, name = parse_store_path("/nix/store/zi2bj2hlavv8q743li2s9diqbcpmrf9b-hello-2.12.3")
        self.assertEqual(digest, "zi2bj2hlavv8q743li2s9diqbcpmrf9b")
        self.assertEqual(name, "hello-2.12.3")

    def test_parse_refuse(self):
        for bad in ("/nix/store/pas-un-hash-ni-longueur-ici-xyz-hello", "/nix/store/hello", "hello-1.0"):
            with self.assertRaises(ValueError, msg=bad):
                parse_store_path(bad)

    def test_human_size(self):
        self.assertEqual(human_size(999), "999 o")
        self.assertTrue(human_size(2048).endswith("Kio"))
        self.assertTrue(human_size(5 * 1024 * 1024).endswith("Mio"))


class TestEnvironnementShell(unittest.TestCase):
    """`npkg env` : le profil doit pouvoir se brancher aussi *après* le PATH."""

    def script(self, shell="sh", after=False):
        tmp = Path(tempfile.mkdtemp(prefix="npkg-env"))
        self.addCleanup(shutil.rmtree, tmp, True)
        home = Home(tmp / "state")
        home.ensure()
        bindir = home.profile("default") / "bin"
        text = Profiles(home, State(home)).env_script("default", shell, after=after)
        return text, str(bindir)

    def test_le_profil_est_prepende_par_defaut(self):
        text, bindir = self.script()
        self.assertEqual(text, f'export PATH="{bindir}:$PATH"')

    def test_apres_ne_masque_pas_le_systeme(self):
        text, bindir = self.script(after=True)
        self.assertEqual(text, f'export PATH="$PATH:{bindir}"')

    def test_chaque_shell_a_sa_syntaxe(self):
        text, bindir = self.script("fish")
        self.assertEqual(text, f"set -gx PATH {bindir} $PATH")
        text, bindir = self.script("fish", after=True)
        self.assertEqual(text, f"set -gx PATH $PATH {bindir}")
        text, bindir = self.script("csh")
        self.assertEqual(text, f'setenv PATH "{bindir}:$PATH"')
        text, bindir = self.script("csh", after=True)
        self.assertEqual(text, f'setenv PATH "$PATH:{bindir}"')


class TestSpecs(unittest.TestCase):
    def test_split_spec(self):
        self.assertEqual(split_spec("hello"), ("hello", ""))
        self.assertEqual(split_spec("hello@2.12.3"), ("hello", "2.12.3"))
        self.assertEqual(split_spec("python313Packages.requests"), ("python313Packages.requests", ""))

    def test_recoit_chemin_du_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolver = Resolver(Path(tmp), use_nix=False)
            self.assertTrue(
                resolver.looks_like_store_path("/nix/store/zi2bj2hlavv8q743li2s9diqbcpmrf9b-hello-2.12.3")
            )
            self.assertTrue(
                resolver.looks_like_store_path("zi2bj2hlavv8q743li2s9diqbcpmrf9b-hello-2.12.3")
            )
            self.assertFalse(resolver.looks_like_store_path("hello"))
            self.assertFalse(resolver.looks_like_store_path("ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ-hello"))

    def test_resoudre_un_pin_explicite(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolver = Resolver(Path(tmp), use_nix=False)
            resolver.add_pin("hello", "/nix/store/zi2bj2hlavv8q743li2s9diqbcpmrf9b-hello-2.12.3")
            res = resolver.resolve("hello")
            self.assertEqual(res.source, "pin")
            self.assertEqual(res.version, "2.12.3")
            self.assertEqual(res.name, "hello")
            self.assertTrue(resolver.remove_pin("hello"))
            self.assertFalse(resolver.remove_pin("hello"))


class TestGcPrudent(unittest.TestCase):
    def test_store_refuse_de_supprimer_chez_autrui(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "store"
            root.mkdir()
            index = ValidIndex(Path(tmp) / "valid.json")
            store = Store(root, index)
            foreign = root / "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-chose-dautrui"
            foreign.mkdir()
            (foreign / "fichier").write_text("précieux\n")
            index.put(ValidEntry(path=str(foreign), created=False))
            self.assertFalse(store.remove(str(foreign)))
            self.assertTrue((foreign / "fichier").exists())

            ours = root / "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-de-nous-1.0"
            ours.mkdir()
            index.put(ValidEntry(path=str(ours), created=True))
            self.assertTrue(store.remove(str(ours)))
            self.assertFalse(ours.exists())

    def test_dir_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x"
            (path / "sous").mkdir(parents=True)
            (path / "sous" / "f").write_text("12345")
            self.assertGreaterEqual(dir_size(path), 5)


class TestProfils(unittest.TestCase):
    def test_generations_et_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Home(Path(tmp) / "state")
            home.ensure()
            state = State(home)
            profiles = Profiles(home, state)
            paquets = Path(tmp) / "store"
            (paquets / "aaaa-hello" / "bin").mkdir(parents=True)
            (paquets / "aaaa-hello" / "bin" / "hello").write_text("a\n")
            (paquets / "bbbb-sl" / "bin").mkdir(parents=True)
            (paquets / "bbbb-sl" / "bin" / "sl").write_text("b\n")

            state.put_root(Root(name="hello", attr="hello", store_path=str(paquets / "aaaa-hello")))
            state.save()
            first = profiles.build()
            self.assertEqual(first["generation"], 1)
            self.assertTrue((home.profile() / "bin" / "hello").is_symlink())

            state.put_root(Root(name="sl", attr="sl", store_path=str(paquets / "bbbb-sl")))
            state.save()
            second = profiles.build()
            self.assertEqual(second["generation"], 2)
            self.assertTrue((home.profile() / "bin" / "sl").is_symlink())

            profiles.switch("default", 1)
            self.assertEqual(profiles.current_generation(), 1)
            self.assertFalse((home.profile() / "bin" / "sl").exists())
            self.assertTrue((home.profile() / "bin" / "hello").exists())

            self.assertIn("export PATH=", profiles.env_script())
            with self.assertRaises(FileNotFoundError):
                profiles.switch("default", 42)

    def test_lock_est_exclusif(self):
        from npkg.state import LockBusy, locked

        with tempfile.TemporaryDirectory() as tmp:
            home = Home(Path(tmp) / "state")
            home.ensure()
            with locked(home):
                with self.assertRaises(LockBusy):
                    with locked(home):
                        pass


if __name__ == "__main__":
    unittest.main()
