# npkg — un gestionnaire de paquets Linux qui parle nixpkgs

`npkg` installe des paquets **depuis les canaux nixpkgs** en utilisant le cache
binaire public de NixOS (`cache.nixos.org`) : il résout un nom en chemin du
store, calcule la fermeture réelle du paquet, télécharge et vérifie les archives
NAR, les dépaquète dans un store, puis expose le tout dans un profil versionné
avec `bin/`, généraitons et retour en arrière.

Il n'y a **rien à installer d'autre** : pas de démon Nix, pas de démon systemd,
pas de dépendance Python. Si Nix est présent sur la machine, npkg s'en sert
volontiers pour résoudre (c'est plus juste), sinon il passe par Hydra.

```console
$ npkg install hello
  5 chemin(s) à dépaqueter (36.7 Mio au total)
  [  1/5] g60m3aky9f59wgy1gi0y06xwmbw19d6m-xgcc-16.2.0-libgcc 197.5 Kio
  [  2/5] q0lbdx5yv93a3md74kjhm0v2kljjii89-libidn2-2.3.8      363.6 Kio
  [  3/5] jp8ql2fnd61xhvb4vsfp0bqwrzf16qrp-libunistring-1.4.2   2.0 Mio
  [  4/5] 5z2yp3ysx8476c8g5w25b0smlgkjvaq3-hello-2.12.3       273.0 Kio
  [  5/5] h4wfwic161kxrr74jlzla5lsm28hgary-glibc-2.44-25       33.9 Mio
✓ 1 paquet(s) installés — 5 chemin(s) dépaquetés, 36.7 Mio reçus en 0.4s
$ eval $(npkg env) && hello
Hello, world!
```

---

## Sommaire

- [Installation et démarrage](#installation-et-démarrage)
- [Sur Arch Linux (et toute distribution déjà garnie)](#sur-arch-linux-et-toute-distribution-déjà-garnie)
- [Commandes](#commandes)
- [Comment ça marche](#comment-ça-marche)
- [Ce que npkg stocke](#ce-que-npkg-stocke)
- [Options globales et variables d'environnement](#options-globales-et-variables-denvironnement)
- [Limites assumées](#limites-assumées)
- [Développement et tests](#développement-et-tests)

---

## Installation et démarrage

npkg est un paquet Python de la bibliothèque standard, donc :

```sh
# 1) soit directement depuis un checkout (aucune installation)
./npkg install hello

# 2) soit « installé » pour tout le monde
python3 -m pip install --user -e .        # fournit la commande npkg
export PATH="$HOME/.local/bin:$PATH"
```

Prérequis : Python ≥ 3.9 et un décompresseur zstd **au choix** — le module
`compression.zstd` de Python 3.14+, `pip install zstandard`, ou le binaire
`zstd`. Les caches au format `.nar.xz` ou `.nar.bz2` sont lus sans rien
installer. `npkg doctor` résume tout ça.

Comme `npkg` est du Python pur, le launcher du checkout suffit : `./npkg`
ajoute `lib/` à `PYTHONPATH`, et rien n'est à installer. C'est même la voie
recommandée là où `pip install --user` est refusé par le fichier
`EXTERNALLY-MANAGED` (Arch, Debian, Fedora — [PEP 668]) : sinon, un venv ou
`pipx install .`.

Ensuite, **un seul point compte** : un paquet nixpkgs est lié
statiquement vers `/nix/store/…`. Pour que les binaires démarrent, npkg doit
dépaqueter dans `/nix/store` :

```sh
npkg init --yes     # sudo mkdir -p /nix/store && sudo chown $UID:$GID /nix/store
npkg status         # store: /nix/store (écrivable) — layout ok
```

Sans cette étape, npkg utilise `~/.local/state/npkg/store` : la résolution, le
téléchargement, la vérification, le dépaquetage, l'état et le GC fonctionnent
identiquement — seuls les binaires dont l'interpréteur est codé en dur
refuseront de se lancer. C'est explicitement signalé par `npkg doctor`.

Pour que les commandes installées soient disponibles au démarrage de la
session :

```sh
echo 'eval "$(npkg env)"' >> ~/.profile     # ou ~/.config/fish/config.fish
```

Sur une machine qui a déjà un `/usr/bin` garni, préférez
`eval "$(npkg env --after)"` : le profil est ajouté **après** le PATH système,
et un paquet npkg ne peut plus y masquer `ls`, `cat` ou `grep`.

---

## Sur Arch Linux (et toute distribution déjà garnie)

npkg ne connaît que HTTP et un dépôt de fichiers plats : il n'est pas lié à
NixOS, et il n'emprunte rien à `pacman`. Sur Arch, il tourne tel quel depuis un
checkout — sans AUR, sans rien compiler. Quatre points de frottement, tous
traités :

| Sujet | Ce qu'il en est |
|---|---|
| **zstd** | Les `.nar` du cache sont en zstd. Trois replis, dans l'ordre : `compression.zstd` de la bibliothèque standard (Python ≥ 3.14), le paquet `zstandard`, puis le binaire `zstd`. Sur Arch les deux premiers sont réunis d'office : `python 3.14.7-1` est en Core et **dépend non optionnellement de `zstd`** (vérifié sur la page du paquet). Le piège est ailleurs : `zstd` ne fait pas partie de `base`, donc une image minimale — container, CI, système de secours — n'a aucun backend ; `npkg doctor` le nomme, et sans lui le téléchargement refuse au lieu d'écrire un store corrompu. |
| **Installation** | PEP 668, appliqué de force : le paquet `python` d'Arch `Provides: python-externally-managed`, donc `pip install` est refusé (`/usr/lib/python3.14/EXTERNALLY-MANAGED`). Launcher, venv, ou `pacman -S python-pipx && pipx install .` — paquet qu'Arch propose justement en dépendance optionnelle de `python`. |
| **`/nix/store`** | `npkg init` le crée (`sudo mkdir -p /nix/store && sudo chown "$USER" /nix/store`). Rien dans `base` n'écrit sous `/nix` (les 28 dépendances du paquet ont été vérifiées) : `pacman -Qo /nix` vous le confirmera, et une mise à jour du système ne touchera jamais un binaire npkg — réciproquement npkg ne sait rien de pacman, ce n'est pas un frontal. Sur une machine partagée, `NPKG_STORE_DIR=$HOME/.local/share/npkg/store` évite la question du droit d'écriture. |
| **`PATH`** | `eval $(npkg env)` prépende le profil — voulu dans un conteneur minimal, gênant ici : `install busybox` puis `coreutils` produit 89 collisions sur `bin/`, et le premier arrivé gagne. D'où `npkg env --after` (et `--shell fish` / `csh`). |

Reste une divergence d'API qui ne se voit qu'ici : `compression.zstd` de la
bibliothèque standard publie `decompress()` et `ZstdFile()`, mais ni
`stream_decompress()` ni `decompressobj()` — ce sont des facilités du paquet
`zstandard`. npkg teste donc `ZstdFile` en premier (il enchaîne les frames, ce
que certains miroirs produisent), puis `stream_decompress` s'il existe, puis
`decompress()` sur le bloc entier ; `ZstdDecompressor` seul est écarté, il lève
`EOFError` sur une entrée à deux frames.

Si `nix` est installé sur la machine, npkg bascule seul sur `nix eval` pour la
résolution : les spécifications deviennent des attributs de l'expression nix,
les releases (`nixpkgs-25.05`) sont admissibles, et Hydra redevient un repli. Le
store est alors partagé avec Nix : npkg y lit les chemins déjà valides, `npkg
remove` refuse ceux créés par `nix`, `npkg gc` ne touche jamais à ce qui n'est
pas à lui.

Enfin, tout ce qui ne demande que la bibliothèque standard — `search`, `info`,
`closure`, `tree`, `size`, `which`, `verify`, `list`, `history`, `rollback`,
`gc --dry-run`, `nar`, `index`, `completion`, `env` — fonctionne même sans
backend zstd : c'est le cas du sandbox de développement de ce dépôt.

---

## Commandes

| commande | ce que ça fait |
| --- | --- |
| `npkg install hello [ripgrep…]` | résout, calcule la fermeture, télécharge, vérifie, dépaquette, met à jour le profil |
| `npkg install -n hello` | idem, mais s'arrête au plan : chemins à télécharger, taille de la fermeture, collisions de `bin/` |
| `npkg install 5z2yp3ysx8476c8g5w25b0smlgkjvaq3-hello-2.12.3` | installe un **chemin du store** précis (reproductibilité, miroir interne) |
| `npkg remove hello` | désinstalle la racine (le profil est régénéré, le store est nettoyé par `gc`) |
| `npkg reinstall hello` | revérifie et refait uniquement ce qui est abîmé ou manquant |
| `npkg list [-l]` | racines installées, version, taille de fermeture, date |
| `npkg search ripgrep` | recherche dans l'index local (`npkg index …`) |
| `npkg info nix` | description, licence, page, chemin résolu, révision nixpkgs, fraîcheur du build |
| `npkg closure hello [--sizes]` | les 5 (ou 500) chemins dont le paquet dépend vraiment |
| `npkg tree hello --depth 3` | graphe de dépendances (lu dans les narinfo) |
| `npkg tree glibc --reverse` | qui dépend de quoi, à l'envers |
| `npkg size hello` | poids téléchargé de la fermeture |
| `npkg verify [hello]` | présence + dépendances + **NarHash recalculé depuis le disque** |
| `npkg gc [--dry-run] [--clear-cache]` | supprime les chemins que npkg a déposés et qui ne servent plus |
| `npkg history` / `npkg rollback [--to N]` | généraitons du profil, retour en arrière atomique |
| `npkg which ls` | suit le lien du profil jusqu'au paquet réel |
| `npkg env` | la ligne `export PATH=…` à évaluer (`--after`, `--shell fish\|csh`) |
| `npkg status` / `npkg doctor` | état complet / diagnostic d'environnement |
| `npkg channel nixpkgs-25.05` | change le canal utilisé par les résolutions |
| `npkg index fetch\|import\|stats\|clear` | gère l'index de recherche |
| `npkg pin <attr> <chemin>` / `npkg unpin` / `npkg pins` | épingle un attribut sur un chemin (CI, miroir hors-ligne) |
| `npkg nar ls\|dump\|hash <path>` | outils NAR bruts, pour fabriquer ou auditer un miroir |
| `npkg completion [bash\|zsh]` | autocomplétion |

Presque tout accepte `--json`, pour être composé dans un script :

```sh
npkg closure hello --json | jq -r '.paths[]' 
npkg list --json | jq 'to_entries[].value.store_path'
```

---

## Comment ça marche

```
   « hello »                                   narinfo  →  references
      │                                                  (la fermeture réelle)
      ▼  1. résolution                                          │
 ┌──────────────┐   pins.json / nix eval / Hydra               ▼
 │  Resolver    │ ──────────────────────────────►  2. closure : BFS sur les References
 └──────────────┘   /nix/store/5z2y…-hello-2.12.3              │   (parallèle, cache narinfo)
      │                                                        ▼
      │            3. plan : ce qui manque, taille, conflits bin/*
      ▼                                                        │
 ┌──────────────┐   <hashPart>.narinfo → URL: nar/….nar.zst   ▼
 │  Substituer  │ ─────────────────────────────────►  4. téléchargement + FileHash + NarHash
 │ (cache Nix)  │                                                │
 └──────────────┘                                                ▼
      │                      5. dépaquetage : NAR → /nix/store/<hash>-<name>
      ▼                              (atomique : staging/ + os.replace, +x et liens du NAR)
 ┌──────────────┐                                                │
 │   State +    │ ◄──────────────────────────────────────────────┘
 │  Profiles    │  6. state.json + génération du profil, symlink remplacé atomiquement
 └──────────────┘
```

Étapes en détail :

1. **Résolution.** Un nom de paquet n'est pas un chemin du store : il faut
   *évaluer* nixpkgs. npkg essaie dans l'ordre : une épingle locale
   (`pins.json`), `nix eval` si Nix est installé, sinon la page de build Hydra
   du job `nixpkgs/<jobset>/<attr>.<system>`, dont la table
   *Output store paths* donne le chemin exact (`out`, `dev`, `bin`…), la
   dérivation, la révision nixpkgs et l'annonce de taille. Le chemin résolu est
   mémorisé dans `state.json` : une réinstallation ultérieure rejoue le même
   binaire, pas « la dernière version à la mode ».
2. **Fermeture.** Chaque sortie du store publie, dans son `.narinfo`, le champ
   `References` : la liste des chemins qu'elle référence *réellement*
   (bibliothèques liées, interpréteur, données). Parcourir ces listes
   récursivement donne la fermeture. Pas de graphe de dépendances approximatif,
   pas de méta-dépendances « recommandées » : ce que le lien dit.
3. **Plan.** Ce qui n'est pas déjà valide dans le store est listé avec sa
   taille ; les collisions (`bin/x` déjà pris par un autre paquet) sont
   annoncées avant d'écrire quoi que ce soit.
4. **Téléchargement et vérification.** `nar/<NarHash>.nar.zst` est récupéré, son
   `FileHash` (sha256, base32 Nix) est contrôlé, décompressé (zstd, xz, bzip2
   ou brut), et le **NarHash du NAR décompressé** est contrôlé. Un contenu qui
   ne correspond pas au hash annoncé est une erreur bloquante.
5. **Dépaquetage.** Le NAR est extrait dans un répertoire tampon du store puis
   `os.replace` vers le chemin définitif : un lecteur concurrent ne voit jamais
   un demi-paquet. Les bits `+x` et les liens symboliques viennent du NAR
   lui-même, donc le binaire est immédiatement exécutable.
6. **Commit.** `state.json` enregistre la racine (attribut, chemin, version,
   fermeture) ; le profil `~/.local/state/npkg/profiles/default` est
   reconstruit dans une nouvelle *génération* dont chaque `bin/` est un lien
   vers le store, et le lien `default` est remplacé atomiquement. `rollback`
   re-pointe simplement ce lien.

### Modules

| fichier | rôle |
| --- | --- |
| `hashing.py` | base32 « à la Nix », hashs de fichiers/flux — le format qui valide un paquet |
| `narinfo.py` | parser/écrivain `.narinfo` (dont `References`, la fermeture) |
| `nar.py` | lecteur + écrivain NAR (extraction disque, `dump` pour miroirs/tests) |
| `zstd.py` | décompression avec 4 chemins de repli, sans dépendance obligatoire |
| `http.py` | HTTP minimal (retry, `file://`, cache) |
| `cache.py` | substituers : `nix-cache-info`, narinfo, NAR, StoreDir |
| `hydra.py` | lecture des pages de build Hydra (résolution + métadonnées) |
| `resolver.py` | pins → nix → Hydra, `nom@version`, chemins du store |
| `closure.py` | parcours du graphe de références (parallèle) |
| `store.py` | déploiement atomique, index `valid.json`, orphelins |
| `state.py` | `state.json`, verrou `flock`, généraitons et rollback |
| `ops.py` | le moteur : plan / install / remove / reinstall / gc / verify |
| `index.py` | index de recherche (GitHub `pkgs/by-name`, import d'un checkout) |
| `cli.py` | argparse, tableaux, `--json`, `doctor` |

---

## Ce que npkg stocke

```
~/.local/state/npkg/
├── state.json          racines demandées : {name → attr, store_path, version, closure}
├── valid.json          ce qui est dans le store : NarHash vérifié, refs, taille, « créé par npkg »
├── pins.json           épingles attr → chemin du store (optionnel)
├── lock                flock : un seul npkg mutateur à la fois
├── cache/
│   ├── narinfo/        .narinfo mises en cache (TTL 6 h)
│   ├── hydra/          pages de build analysées (TTL 1 h)
│   └── index.json      index de recherche
├── profiles/
│   ├── default -> generations/2-20261001140819
│   └── generations/…  bin/{hello,sl}, .npkg-manifest.json (liens, racines, conflits)
└── tmp/                blobs et NAR en cours (nettoyés par `npkg gc`)
```

Deux principes : un chemin du store est **immuable** une fois posé, et npkg ne
supprime **que ce qu'il a déposé lui-même**. Un `/nix/store` partagé avec une
vraie installation de Nix ne risque donc rien : `npkg gc` signale les chemins
qu'il ne connaît pas et les laisse en paix (c'est testé).

---

## Options globales et variables d'environnement

```
--home DIR          état (défaut $NPKG_HOME ou ~/.local/state/npkg)
--store-dir DIR     store (défaut $NPKG_STORE_DIR, sinon /nix/store si écrivable)
--channel CANAL     canal nixpkgs pour les résolutions
--system SYS        x86_64-linux, aarch64-linux, …
--jobset JS         force le jobset Hydra (unstable, staging-26.05, …)
--cache URL         substituer (répétable) ; remplace la liste par défaut
-j N                téléchargements parallèles (défaut 4)
-n, --dry-run       planifie sans écrire
--no-verify         ne pas vérifier FileHash/NarHash (déconseillé)
--refresh           ignore les caches narinfo/Hydra
--json, --quiet     sortie machine, sortie muette
--no-nix            ignorer nix même s'il est installé
```

| variable | effet |
| --- | --- |
| `NPKG_HOME`, `NPKG_STORE_DIR`, `NPKG_CHANNEL`, `NPKG_SYSTEM` | idem options |
| `NPKG_SUBSTITUTERS` | liste des miroirs (séparés par des espaces, `file://` accepté) |
| `NPKG_OFFLINE=1` | n'utiliser que `pins.json` (CI hermétique, tests) |
| `NPKG_NARINFO_TTL`, `NPKG_HYDRA_TTL` | âges max des caches (secondes) |
| `NPKG_STALE_DAYS` | seuil d'alerte « build Hydra très ancien » (défaut 120) |
| `NPKG_HTTP_TIMEOUT`, `NPKG_HTTP_RETRIES`, `NPKG_JOBS` | réseau et parallélisme |
| `NPKG_DEBUG=1` | laisse fuser la traceback au lieu d'un message propre |

Un miroir local, ça se fabrique en trois lignes et ça évite le réseau aux tests :

```sh
mkdir -p ~/miroir/nar
# un NAR brut + sa narinfo suffisent (Compression: none)
npkg nar dump /chemin/vers/paquet > ~/miroir/nar/<NarHash>.nar
NPKG_SUBSTITUTERS=~/miroir NPKG_OFFLINE=1 npkg install <attr>
```

---

## Limites assumées

Ce projet est *basique* volontairement. Ce qu'il ne fait pas :

- **Pas d'évaluation Nix.** npkg ne lit pas `default.nix`, ne calcule pas de
  `drv` et ne construit rien : il installe des binaires déjà publiés. Sans
  Nix, la résolution dépend de Hydra (jobset `unstable` surtout) ; avec Nix,
  elle est exacte.
- **Canaux et jobsets.** `--channel nixpkgs-unstable` est le plus sûr. Les
  releases passent par les jobsets `staging-XX.YY` côté Hydra, où tous les
  paquets n'ont pas forcément un job dédié — `--jobset` et `--pin` dépannent.
- **Profils plats.** Si deux paquets fournissent le même `bin/x`, le premier
  arrivé (ordre alphabétique des racines) garde la place et npkg l'annonce —
  mais il n'y a pas de système de *priorités* comme `nix-env`.
- **Signatures détachées non vérifiées.** L'intégrité vient du `FileHash` et du
  `NarHash` (sha256, contrôlés à chaque téléchargement *et* par `npkg verify`) ;
  l'authentification du narinfo vient du HTTPS. npkg ne valide pas le champ
  `Sig:` ed25519, et ne vérifie donc pas un miroir en HTTP clair : rester sur
  `https://cache.nixos.org` ou un miroir de confiance.
- **Un utilisateur, pas de système.** npkg écrit dans votre store et votre
  profil ; il ne touche pas à `/etc`, ne gère pas de services, pas de
  `ldconfig`, pas de hooks d'installation, pas de `NIX_PATH`, pas de
  multiplexage multi-utilisateurs, pas de `sandboxed build`.
- **Pas de `unstable`→frozen.** `npkg install` prend le dernier build Hydra
  connu, puis épingle le chemin dans `state.json`. Un vrai `flake.lock`
  n'existe pas ; `npkg pins` en tient lieu si besoin.
- **Python uniquement.** `site-packages` des paquets Python installés via npkg
  ne sont pas mis en évidence (pas de `PYTHONPATH` magique) ; `npkg env` ne
  fait que le `PATH`.

---

## Développement et tests

```sh
python3 run-tests.py            # 54 tests, bibliothèque standard seulement
python3 run-tests.py -q
python3 -m pytest tests         # si pytest est installé
```

54 tests, **aucun réseau requis** — la suite est conçue ainsi :

- `test_hashing.py` / `test_nar.py` portent sur un **vrai** NAR de
  `cache.nixos.org` (`hello-2.12.3`, 279 624 octets, dans `tests/fixtures/`) :
  extraction puis re-sérialisation doivent redonner l'archive **octet pour
  octet**, et le `NarHash` doit correspondre à celui publié par le cache. C'est
  ce test qui a corrigé deux erreurs de format (la racine porte bien un
  `type directory`, et le jeton d'exécution s'appelle `executable`).
- `test_narinfo.py`, `test_hydra_index.py` : formats réels (narinfo brute, page
  de build Hydra sauvée en fixture).
- `test_e2e_offline.py` : fabrique un miroir binaire complet (nix-cache-info,
  narinfo, NAR), deux paquets dont l'un dépend de l'autre, puis roule la CLI
  pour vérifier installation, dédup, `verify`, corruption détectée puis
  réparée par `reinstall`, `--dry-run` inerte, `remove`/`gc`, sécurité du GC
  face aux chemins d'autrui, collisions de `bin/`, `rollback`.
- `test_zstd_backend.py` tend à npkg un module bouchon réduit à la surface que
  `compression.zstd` de Python 3.14 publie réellement — `decompress` et
  `ZstdFile`, pas de `stream_decompress` — avec deux frames concaténées à
  recoller. C'est exactement l'endroit où la stdlib diverge de `zstandard`, et
  le genre de bug invisible tant qu'on ne teste que sur un Python 3.13.
- Les tests marqués `skip` le sont quand une fixture manque ; ils ne masquent
  jamais un échec.

### Arborescence

```
npkg/
├── npkg              # lanceur sans installation (PYTHONPATH=lib)
├── lib/npkg/         # le paquet (14 modules, ~4,2 kL avec les commentaires)
├── tests/            # unittest, fixtures réelles, zéro réseau
├── run-tests.py      # runner sans dépendance
└── pyproject.toml    # pip install -e . → commande npkg
```

---

## Licence

MIT. Ce projet est un outil pédagogique et expérimental : il manipule votre
`/nix/store`. Lisez `npkg doctor` et les avertissements avant de l'utiliser sur
une machine qui compte.
