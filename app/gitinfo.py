"""État du dépôt d'un projet, vu depuis le conteneur.

/home/dev/projects y est monté en lecture seule au même chemin que sur l'hôte :
on peut donc interroger git pour expliquer à l'écran pourquoi la file n'a pas
démarré. Un garde-fou qui refuse en silence ressemble à une panne.
"""
import re
import subprocess
from pathlib import Path


def _git(path: str, *args: str) -> tuple[int, str]:
    # `safe.directory=*` est indispensable : les dépôts appartiennent à `dev` et
    # le conteneur tourne en root. Sans lui, git répond « dubious ownership » et
    # l'inspection échoue — silencieusement, ce qui est pire que pas d'inspection.
    try:
        out = subprocess.run(["git", "-c", "safe.directory=*", "-C", path, *args],
                             capture_output=True, text=True, timeout=10)
        return out.returncode, out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def _git_brut(path: str, *args: str, timeout: int = 20) -> tuple[int, str]:
    """Comme `_git`, mais la sortie n'est pas rognée — pour lire un fichier."""
    try:
        out = subprocess.run(["git", "-c", "safe.directory=*", "-C", path, *args],
                             capture_output=True, text=True, timeout=timeout)
        return out.returncode, out.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def blocker(path: str | None) -> str | None:
    """Raison qui empêche la file de démarrer sur ce projet, ou None si prêt."""
    if not path:
        return "aucun chemin enregistré pour ce projet"
    if not Path(path).is_dir():
        return f"chemin introuvable : {path}"
    code, inside = _git(path, "rev-parse", "--is-inside-work-tree")
    if code != 0 or inside != "true":
        return None  # hors dépôt git : rien à vérifier, la file peut démarrer
    _, changed = _git(path, "status", "--porcelain")
    if changed:
        count = len(changed.splitlines())
        return (f"dépôt non propre — {count} fichier(s) modifié(s) non commités. "
                "La file attend un arbre net pour qu'un commit corresponde à une tâche.")
    return None


MAX_PATCH_LINES = 1500


def patch_files(path: str | None, base: str | None, ref: str | None,
                max_lines: int = MAX_PATCH_LINES) -> tuple[list[dict], bool]:
    """Le diff d'une branche d'agent, découpé fichier par fichier.

    On compare avec `base...ref` — trois points — pour ne montrer que ce que la
    branche ajoute, sans y mêler ce qui a bougé sur la base entre-temps.

    Renvoie (fichiers, tronqué). Un diff énorme n'est pas relisible à l'écran :
    on coupe et on le dit, plutôt que de faire ramer la page.
    """
    if not path or not ref:
        return [], False
    code, out = _git(path, "diff", f"{base or 'HEAD'}...{ref}")
    if code != 0 or not out:
        return [], False

    lignes = out.splitlines()
    tronque = len(lignes) > max_lines
    fichiers: list[dict] = []
    courant: dict | None = None
    for ligne in lignes[:max_lines]:
        if ligne.startswith("diff --git"):
            nom = ligne.split(" b/")[-1] if " b/" in ligne else ligne
            courant = {"name": nom, "lines": [], "added": 0, "removed": 0}
            fichiers.append(courant)
            continue
        if courant is None:
            continue
        if ligne.startswith(("index ", "--- ", "+++ ", "new file", "deleted file",
                             "similarity", "rename ", "old mode", "new mode")):
            continue
        if ligne.startswith("@@"):
            kind = "hunk"
        elif ligne.startswith("+"):
            kind = "add"
            courant["added"] += 1
        elif ligne.startswith("-"):
            kind = "del"
            courant["removed"] += 1
        else:
            kind = "ctx"
        courant["lines"].append({"kind": kind, "text": ligne})
    return fichiers, tronque


def orphan_files(path: str | None, base: str | None, ref: str | None) -> list[str]:
    """Fichiers ajoutés par la branche que rien d'autre ne mentionne.

    Un agent peut livrer un module **et son test**, tout vert, sans jamais le
    brancher dans l'application : les tests passent, le lint aussi, et la
    fonctionnalité n'existe pas. C'est arrivé sur mare avec `gains.ts`.

    On cherche une forme d'import du module dans les autres fichiers de code, en
    excluant les tests — un module dont seul son propre test se sert n'est pas
    branché. Ça ne prouve rien, mais ça pose la bonne question au bon moment.
    """
    if not path or not ref:
        return []
    code, out = _git(path, "diff", "--diff-filter=A", "--name-only", f"{base or 'HEAD'}...{ref}")
    if code != 0 or not out:
        return []
    orphelins = []
    for fichier in out.splitlines():
        chemin = Path(fichier)
        tige = chemin.stem
        if not tige or chemin.suffix not in CODE_SUFFIXES or est_un_test(fichier):
            continue
        # On cherche une forme d'IMPORT, pas le mot. Chercher le mot donnait des
        # faux négatifs cocasses : « gains » se trouve dans `saturatedAgainst`,
        # ce qui faisait passer un module orphelin pour utilisé.
        motif = (rf"['\"/]{re.escape(tige)}(\.[a-z]+)?['\"]"
                 rf"|\b(import|from)\s+[\w.]*{re.escape(tige)}\b")
        code, trouve = _git(path, "grep", "-lE", motif, ref, "--",
                            *[f"*{s}" for s in CODE_SUFFIXES])
        mentions = [l.split(":", 1)[-1] for l in trouve.splitlines()] if code == 0 else []
        ailleurs = [m for m in mentions
                    if m != fichier and not est_un_test(m)]
        if not ailleurs:
            orphelins.append(fichier)
    return orphelins


CODE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".py", ".go", ".rs",
                 ".php", ".rb", ".java", ".vue", ".svelte")


def est_un_test(chemin: str) -> bool:
    """Un fichier de test n'a pas à être appelé : c'est lui qui appelle."""
    nom = Path(chemin).name.lower()
    parties = {p.lower() for p in Path(chemin).parts}
    return (".test." in nom or ".spec." in nom or nom.startswith("test_")
            or bool(parties & {"test", "tests", "__tests__", "spec"}))


def branches(path: str | None) -> list[str]:
    """Branches locales, la courante d'abord."""
    if not path:
        return []
    code, out = _git(path, "branch", "--format=%(refname:short)")
    if code != 0:
        return []
    toutes = [b.strip() for b in out.splitlines() if b.strip()]
    _, courante = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    return ([courante] if courante in toutes else []) + [b for b in toutes if b != courante]


def read_blob(path: str | None, ref: str, fichier: str, max_octets: int = 400_000
              ) -> tuple[str | None, str]:
    """Contenu d'un fichier **à une référence donnée**, sans toucher au disque.

    `git show <ref>:<chemin>` lit dans l'objet, pas dans la copie de travail :
    on peut donc afficher la documentation d'une branche d'agent pendant qu'un
    autre travail est sorti, et sans jamais changer de branche sous les pieds
    de quelqu'un.

    Renvoie (contenu, commit court). Contenu à None si la référence ou le
    fichier n'existe pas à cette référence — cas banal : un document ajouté par
    une branche n'existe pas sur `master`.
    """
    if not path or not ref or not fichier:
        return None, ""
    # **Sans `strip`** : `_git` rogne les blancs, ce qui conviendrait à un chemin
    # mais mangerait l'indentation de la première ligne d'un fichier. C'est la
    # même famille d'erreur que le `.strip()` sur `status --porcelain`, qui avait
    # amputé un chemin d'un caractère et fait livrer un module sans le brancher.
    #
    # `--` sépare les références des chemins : sans lui, un fichier nommé comme
    # une branche rendrait la commande ambiguë.
    code, contenu = _git_brut(path, "show", f"{ref}:{fichier}", "--")
    if code != 0:
        return None, ""
    _, commit = _git(path, "rev-parse", "--short", ref)
    return contenu[:max_octets], commit


def summary(path: str | None) -> dict:
    """Branche, dernier commit et blocage éventuel."""
    info = {"blocker": blocker(path), "branch": None, "head": None}
    if path and Path(path).is_dir():
        _, info["branch"] = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
        _, info["head"] = _git(path, "log", "-1", "--pretty=%h %s")
    return info
