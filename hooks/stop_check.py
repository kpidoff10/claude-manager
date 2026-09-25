#!/usr/bin/env python3
"""Hook Stop : empêche de terminer un tour sur du travail non consigné.

Le hook SessionStart règle la lecture — le briefing arrive tout seul. Celui-ci
règle l'écriture : si des fichiers du projet ont changé et que rien n'a été
écrit dans claude-manager depuis le début de la session, il rend la main à
Claude avec la consigne de consigner.

Trois garde-fous, parce qu'un hook bloquant mal conçu est pire que pas de hook :

1. `stop_hook_active` — Claude Code le passe à vrai quand le tour reprend à
   cause d'un hook Stop. On sort immédiatement : jamais de boucle.
2. un marqueur par session — on ne réclame qu'une seule fois, quoi qu'il arrive.
3. des preuves — sans fichier modifié, aucun rappel. Une session de discussion
   ne déclenche rien.

En cas de doute (serveur injoignable, projet inconnu, dépôt illisible), le hook
se tait : il vaut mieux rater un rappel que bloquer une session à tort.
"""
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import api_get, load_state, read_payload, save_state  # noqa: E402

MAX_LISTED_FILES = 8

# Certains projets tiennent leur mémoire dans le dépôt — mare a SESSION-LOG.md
# et DECISIONS.md en append-only. Une session qui les met à jour a consigné son
# travail à l'endroit prévu : la réclamer serait contredire la règle « en cas de
# recouvrement, le dépôt gagne ». On ne reconnaît que les fichiers de type
# journal ; toucher DESIGN.md ou ARCHITECTURE.md, c'est du travail, pas une trace.
REPO_MEMORY_FILES = ("SESSION-LOG", "DECISIONS", "CHANGELOG", "JOURNAL", "CONTEXT")


def run(args: list[str], timeout: int = 5) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def evidence_of_work(path: str, since: str) -> list[str]:
    """Fichiers modifiés ou commités depuis le début de la session."""
    inside_git = run(["git", "-C", path, "rev-parse", "--is-inside-work-tree"]) == "true"
    if inside_git:
        changed = [line[3:].strip() for line in
                   run(["git", "-C", path, "status", "--porcelain"]).splitlines() if line]
        committed = run(["git", "-C", path, "log", f"--since={since}",
                         "--name-only", "--pretty=format:"]).split()
        return sorted({*changed, *committed})

    # Projet hors dépôt git : on retombe sur les dates de modification, en
    # restant peu profond pour ne pas parcourir un arbre entier.
    found = run(["find", path, "-maxdepth", "3", "-type", "f", "-newermt", since,
                 "-not", "-path", "*/node_modules/*", "-not", "-path", "*/.git/*",
                 "-not", "-path", "*/data/*"], timeout=8)
    return [f for f in found.splitlines() if f][:50]


def records_in_repo(files: list[str]) -> bool:
    """Le travail a-t-il été consigné dans la mémoire écrite du dépôt ?

    On exige un document Markdown : sans cette contrainte, un gabarit nommé
    `journal.html` suffirait à désactiver tout le garde-fou — ce qui est
    précisément arrivé au premier essai.
    """
    for name in files:
        path = Path(name)
        if path.suffix.lower() == ".md" and path.stem.upper().startswith(REPO_MEMORY_FILES):
            return True
    return False


def block(reason: str) -> None:
    json.dump({"decision": "block", "reason": reason}, sys.stdout)
    sys.stdout.write("\n")


def main() -> None:
    payload = read_payload(sys.stdin)
    if payload.get("stop_hook_active"):
        return

    session_id = payload.get("session_id") or ""
    state = load_state(session_id)
    if state.get("nagged"):
        return

    cwd = payload.get("cwd") or state.get("cwd") or ""
    if not cwd or not Path(cwd).is_dir():
        return

    since = state.get("started_at") or (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).replace(microsecond=0).isoformat()

    status, body = api_get("/api/activity", {"path": cwd, "since": since})
    if status != 200 or not body:
        return
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return
    files = evidence_of_work(cwd, since)
    if not files:
        return  # aucune trace de travail : session de discussion, on se tait

    activity = data.get("activity", {})
    manques = []

    # Le suivi des tâches ne se délègue à rien d'autre : c'est la seule chose que
    # Kevin pilote, et elle ne vit que dans le manager. Un journal bien tenu ne
    # remplace pas une tâche fermée — l'avancement d'un projet resterait à 0 %.
    if not activity.get("tasks"):
        manques.append("tâches")

    # Le récit de ce qui s'est passé, en revanche, peut légitimement vivre dans
    # la mémoire écrite du dépôt quand le projet en tient une.
    if not activity.get("journal") and not records_in_repo(files):
        manques.append("journal")

    if not manques:
        return

    save_state(session_id, nagged=True)

    listed = ", ".join(files[:MAX_LISTED_FILES])
    if len(files) > MAX_LISTED_FILES:
        listed += f" (et {len(files) - MAX_LISTED_FILES} autres)"

    lines = [
        f"Il manque {' et '.join(manques)} dans claude-manager pour le projet "
        f"« {data.get('project')} », alors que {len(files)} fichier(s) ont changé "
        f"pendant cette session : {listed}.",
        "",
    ]
    if "tâches" in manques:
        lines += [
            "**Chaque chose faite ou demandée doit exister comme tâche**, terminée "
            "ou non. Sans cela l'avancement du projet reste à zéro et Kevin ne peut "
            "rien prioriser.",
            "- ce qui a été livré : `create_task` puis `update_task(status='done')` "
            "— même rétroactivement, même si ça n'a pris que deux minutes ;",
            "- ce qui a été demandé mais pas fait : `create_task` avec la priorité "
            "qui convient, `owner='user'` si ça demande une décision de sa part ;",
            "- ce qui a été découvert en chemin et laissé de côté : `create_task`, "
            "sinon c'est perdu.",
        ]
    if "journal" in manques:
        lines += [
            "- `log_work` — un résumé de ce qui vient d'être fait, compréhensible "
            "hors contexte dans trois semaines ;",
            "- `add_memory(kind='decision')` — pour toute décision qui engage la "
            "suite, avec le pourquoi.",
        ]
    in_progress = data.get("in_progress") or []
    if in_progress:
        lines.append("")
        lines.append("Tâches encore marquées « en cours » : " + ", ".join(
            f"#{t['id']} {t['title']}" for t in in_progress) + ".")
    lines.append("")
    lines.append("Si le travail effectué ne mérite vraiment pas de trace "
                 "(essai abandonné, fichier temporaire), dis-le simplement et termine : "
                 "ce rappel ne se répétera pas dans cette session.")

    block("\n".join(lines))


if __name__ == "__main__":
    main()
