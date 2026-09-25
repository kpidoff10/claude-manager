#!/usr/bin/env python3
"""Démon de file d'agents — tourne sur l'hôte, sous l'utilisateur `dev`.

Le manager vit dans un conteneur et ne peut rien lancer sur la machine : c'est
donc ce démon qui interroge la file et exécute les agents.

Plusieurs agents peuvent tourner de front, dans la limite de CM_MAX_PARALLEL.
Deux projets différents ne partagent aucun fichier : leurs agents cohabitent
sans précaution. Au sein d'UN MÊME projet, un agent juge est consulté pour dire
quelles tâches ont des périmètres disjoints — mais son avis n'est pas la
sécurité du dispositif. La sécurité vient après : on relit ce que chaque agent a
réellement écrit, et si deux d'entre eux ont touché le même fichier, rien n'est
commité pour eux et les tâches partent en vérification. Le juge fait gagner du
temps ; l'attribution empêche les dégâts.

Déroulé d'une tâche :

  dépôt propre ? → réclamation → agent (claude -p) → tests du projet
                                                        │
                                    verts ─────────────┤─────── rouges
                                      │                          │
                              commit unique                 rien de commité
                                      │                          │
                                      └──── tâche « à vérifier » ─┘

Le dépôt sale est un refus, pas une erreur : tant que la tâche précédente n'est
pas relue, sa suivante n'entre pas dans un arbre encombré.
"""
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import runlog  # noqa: E402

MANAGER_DIR = Path(__file__).resolve().parent.parent
BASE_URL = os.environ.get("CM_BASE_URL", "http://127.0.0.1:8099")
# `data/` appartient à root (écrit par le conteneur) ; le démon tourne sous dev.
# Les journaux vont donc dans logs/, que le conteneur relit grâce au montage en
# lecture seule de /home/dev/projects au même chemin.
LOG_DIR = MANAGER_DIR / "logs" / "runs"
# Les copies de travail des agents. Hors de /home/dev/projects pour qu'un
# worktree ne soit jamais pris pour un projet par la résolution de chemin.
WORKTREES = Path(os.environ.get("CM_WORKTREES", "/home/dev/worktrees"))
PREVIEW_PORTS = range(int(os.environ.get("CM_PREVIEW_PORT_MIN", "4500")),
                      int(os.environ.get("CM_PREVIEW_PORT_MAX", "4560")))
POLL_SECONDS = int(os.environ.get("CM_POLL", "10"))
TESTS_TAIL = 4000

# Un processus par exécution en cours. Une variable unique ne suffisait plus dès
# lors que plusieurs agents tournent : seul le dernier aurait été tuable.
_processes: dict[int, subprocess.Popen] = {}
# Dernier refus signalé par projet : sans mémoire, un dépôt sale ferait une
# ligne de journal toutes les dix secondes, indéfiniment.
_last_note: dict[str, str] = {}
# Projets dont un lot est déjà lancé : le sondage suivant ne doit pas en
# relancer un second pendant que les réclamations sont en cours.
# Tâches déjà lancées, par numéro : deux agents sur la même tâche n'auraient
# aucun sens, et le sondage revient toutes les dix secondes.
_batches: set[int] = set()
# Projets sur lesquels une tâche de rangement travaille dans la copie
# principale : elle exige d'être seule, personne d'autre ne démarre là.
_solo: dict[str, bool] = {}
# Exécutions dont on a déjà tenté de résoudre les conflits, pour n'essayer
# qu'une fois : la résolution relance la fusion, et un échec répété bouclerait.
_conflits_tentes: set[int] = set()
_batch_lock = threading.Lock()


def note_once(key: str, message: str) -> None:
    if _last_note.get(key) != message:
        _last_note[key] = message
        log(message)


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def env_value(name: str, default: str = "") -> str:
    """L'environnement d'abord, le .env du manager ensuite."""
    if os.environ.get(name):
        return os.environ[name]
    env = MANAGER_DIR / ".env"
    for line in env.read_text().splitlines() if env.exists() else []:
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    return default


def token() -> str:
    return env_value("CM_API_TOKEN")


def api(path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE_URL}{path}", data=data,
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        return {"_error": error.code}
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as error:
        return {"_error": str(error)}


def git(path: str, *args: str, timeout: int = 30) -> str:
    """Sortie de git, débarrassée des seuls sauts de ligne.

    Surtout PAS `.strip()` : `git status --porcelain` commence chaque ligne par
    deux caractères d'état, et l'espace de tête de la PREMIÈRE ligne y serait
    mangé. Le chemin y perdait alors son premier caractère — « client » devenant
    « lient » — et le fichier n'était plus reconnu comme écrit par l'agent, donc
    exclu de son commit. C'est ce qui a fait livrer un module sans le brancher.
    """
    try:
        out = subprocess.run(["git", "-C", path, *args],
                             capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip("\n")
    except (OSError, subprocess.SubprocessError):
        return ""


def is_git_repo(path: str) -> bool:
    return git(path, "rev-parse", "--is-inside-work-tree") == "true"


def tree_is_clean(path: str) -> bool:
    return is_git_repo(path) and git(path, "status", "--porcelain") == ""


def head(path: str) -> str | None:
    return git(path, "rev-parse", "HEAD") or None


def slugify(text: str, length: int = 40) -> str:
    keep = "".join(c if (c.isalnum() or c in "-_") else "-" for c in text.lower())
    while "--" in keep:
        keep = keep.replace("--", "-")
    return keep.strip("-")[:length].strip("-") or "tache"


def current_branch(path: str) -> str | None:
    name = git(path, "rev-parse", "--abbrev-ref", "HEAD")
    return name if name and name != "HEAD" else None


def open_worktree(project: dict, task: dict) -> tuple[str, str | None, str | None]:
    """Crée une copie de travail dédiée à la tâche, sur sa propre branche.

    L'agent n'entre jamais dans la copie principale : Kevin peut continuer d'y
    travailler, et deux agents ne se croisent pas. Renvoie (répertoire de
    travail, branche, branche de base) ; en cas d'échec on retombe sur la copie
    principale, car mieux vaut un agent sans isolation qu'un agent qui ne part
    pas.
    """
    path = project["path"]
    if not is_git_repo(path) or not head(path):
        return path, None, None
    base = current_branch(path)
    branche = f"agent/t{task['id']}-{slugify(task['title'])}"
    if git(path, "rev-parse", "--verify", branche):
        branche = f"{branche}-{uuid.uuid4().hex[:4]}"
    cible = WORKTREES / f"{project['slug']}-t{task['id']}-{uuid.uuid4().hex[:4]}"
    WORKTREES.mkdir(parents=True, exist_ok=True)
    out = subprocess.run(["git", "-C", path, "worktree", "add", "-b", branche, str(cible)],
                         capture_output=True, text=True, timeout=180)
    if out.returncode != 0:
        log(f"worktree impossible ({out.stderr.strip()[:120]}) — travail sur la copie principale")
        return path, None, base
    return str(cible), branche, base


def drop_worktree(project_path: str, worktree: str) -> None:
    """Retire une copie de travail — après avoir mis à l'abri ce qui y traîne.

    Un worktree peut contenir du travail non commité : ce qui a échoué aux
    tests, ou ce que l'attribution a écarté. Le supprimer sec l'efface pour
    toujours. C'est arrivé une fois, et ça a coûté le branchement d'un module.
    Le remisage va dans le dépôt principal, donc récupérable par git stash list.
    """
    if git(worktree, "status", "--porcelain"):
        subprocess.run(["git", "-C", worktree, "stash", "push", "-u", "-m",
                        f"Sauvegarde avant retrait de {Path(worktree).name}"],
                       capture_output=True, timeout=120)
        log(f"⚑ travail non commité de {Path(worktree).name} remisé avant retrait")
    branche = current_branch(worktree)
    subprocess.run(["git", "-C", project_path, "worktree", "remove", "--force", worktree],
                   capture_output=True, timeout=120)
    subprocess.run(["git", "-C", project_path, "worktree", "prune"],
                   capture_output=True, timeout=60)

    # **La branche ne peut être effacée qu'ici.** Git refuse de supprimer une
    # branche sortie dans un worktree : la tentative faite juste après la fusion
    # échoue donc toujours, et les branches d'agents s'accumulaient. On réessaie
    # une fois la copie retirée, et seulement avec `-d` — qui refuse ce qui n'est
    # pas fusionné, et laisse donc intact ce qui vaut encore d'être récupéré.
    if branche and branche.startswith("agent/"):
        efface = subprocess.run(["git", "-C", project_path, "branch", "-d", branche],
                                capture_output=True, text=True, timeout=60)
        if efface.returncode == 0:
            log(f"⌫ branche {branche} effacée (fusionnée)")


def build_prompt(task: dict, project: dict) -> str:
    return f"""Tu es lancé automatiquement par la file d'agents de claude-manager pour \
traiter UNE seule tâche, sans personne devant l'écran.

Projet : {project['slug']} ({project['path']})
Tâche #{task['id']} — {task['title']}
Priorité : {task.get('priority_label', 'normal')}

Énoncé :
{task.get('body') or '(vide)'}

Règles de cette exécution :
- Reste strictement dans le périmètre de cette tâche. Ne corrige rien d'autre au passage.
- Le briefing du projet t'a été injecté au démarrage : respecte ses conventions, ses \
décisions et sa mémoire écrite.
- Lance les tests du projet avant de conclure.
- Si quoi que ce soit relève d'une décision de Kevin — arbitrage produit, ambiguïté de \
l'énoncé, choix qui engage la suite — n'invente pas : appelle \
ask_user(task_id={task['id']}, question="<ta question>", options=["Choix A — sa \
conséquence", "Choix B — sa conséquence"], recommendation="<celle que tu retiendrais \
et pourquoi>") puis arrête-toi immédiatement. Trois options au maximum, chacune \
actionnable : Kevin doit pouvoir trancher d'un clic.
- NE COMMITE PAS. La file commite elle-même, une fois les tests vérifiés.
- Ne mets pas la tâche en 'done' : elle passe en vérification humaine.
- Termine par log_work décrivant ce que tu as fait et pourquoi, puis arrête-toi.
"""


def run_agent(task: dict, project: dict, log_path: Path, timeout: int,
              run_id: int = 0) -> tuple[int, bool]:
    """Lance l'agent. Renvoie (code de sortie, arrêté par nous)."""
    prompt = build_prompt(task, project)
    # stream-json plutôt que la sortie par défaut : `claude -p` n'écrit rien
    # avant la toute fin, or c'est pendant que l'agent travaille qu'on veut le
    # regarder. Chaque événement est écrit au fil de l'eau, ligne par ligne.
    command = ["claude", "-p", prompt, "--permission-mode", "auto",
               "--output-format", "stream-json", "--verbose"]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as stream:
        stream.write(f"$ claude -p (tâche #{task['id']} — {task['title']})\n\n")
        stream.flush()
        process = subprocess.Popen(command, cwd=project["path"], stdout=stream,
                                   stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        _processes[run_id] = process
        deadline = time.time() + timeout
        stopped = False
        tours = 0
        while process.poll() is None:
            tours += 1
            # Toutes les ~30 s : l'agent répète-t-il le même geste ? Le journal
            # peut peser plusieurs Mo, inutile de le relire à chaque tour.
            if tours % 10 == 0:
                boucle = runlog.repeated_tail(str(log_path))
                if boucle:
                    log(f"boucle détectée sur #{run_id} — « {boucle[:70]} »")
                    process.kill()
                    stopped = True
                    break
            if time.time() > deadline:
                log(f"délai dépassé ({timeout}s) — arrêt de l'agent #{run_id}")
                process.kill()
                stopped = True
                break
            if api("/api/queue/state").get("stop_requested"):
                log("arrêt demandé depuis l'interface")
                for other in list(_processes.values()):
                    if other.poll() is None:
                        other.kill()
                api("/api/queue/ack-stop", {})
                stopped = True
                break
            time.sleep(3)
        code = process.wait()
    _processes.pop(run_id, None)
    return code, stopped


def free_port(used: list[int]) -> int | None:
    for port in PREVIEW_PORTS:
        if port not in used:
            return port
    return None


def start_preview(project: dict, task: dict, commands: list[dict], worktree: str,
                  branche: str | None, used: list[int]) -> tuple[str | None, int | None]:
    """Démarre l'instance d'essai de la tâche, si le projet sait le faire.

    Le projet déclare une commande `preview` dans sa fiche ; elle reçoit le
    port, le worktree et la branche par l'environnement, et se débrouille pour
    s'exposer — labels Traefik ou port publié, c'est son affaire. Un service
    nommé `preview` porte le gabarit d'URL, où {port} et {task} sont remplacés.

    Sans commande `preview`, il n'y a simplement pas d'aperçu.
    """
    entry = next((c for c in commands if c["name"] == "preview"), None)
    if entry is None:
        return None, None
    port = free_port(used)
    if port is None:
        log("aucun port d'aperçu libre")
        return None, None
    env = {**os.environ, "CM_PORT": str(port), "CM_TASK": str(task["id"]),
           "CM_WORKTREE": worktree, "CM_BRANCH": branche or "",
           "CM_PROJECT": project["slug"]}
    try:
        out = subprocess.run(entry["command"], shell=True, cwd=worktree, env=env,
                             capture_output=True, text=True, timeout=900)
    except subprocess.SubprocessError as error:
        log(f"aperçu impossible : {error}")
        return None, None
    if out.returncode != 0:
        log(f"aperçu en échec : {(out.stderr or out.stdout).strip()[:160]}")
        return None, None

    gabarit = next((s.get("url") for s in project.get("services", [])
                    if s.get("name") == "preview" and s.get("url")), None)
    hote = env_value("CM_PREVIEW_HOST", "localhost")
    url = (gabarit or f"http://{hote}:{{port}}").format(port=port, task=task["id"],
                                                        project=project["slug"])
    log(f"⧉ aperçu de #{task['id']} sur {url}")
    return url, port


def stop_preview(project: dict, commands: list[dict], worktree: str, port: int | None,
                 task_id: int) -> None:
    entry = next((c for c in commands if c["name"] == "preview-stop"), None)
    if entry is None or not Path(worktree).is_dir():
        return
    env = {**os.environ, "CM_PORT": str(port or ""), "CM_TASK": str(task_id),
           "CM_WORKTREE": worktree, "CM_PROJECT": project["slug"]}
    subprocess.run(entry["command"], shell=True, cwd=worktree, env=env,
                   capture_output=True, timeout=300)


def in_worktree(workdir: str | None, project_path: str, worktree: str) -> str:
    """Rejoue un répertoire enregistré dans la copie de travail de l'agent."""
    if not workdir:
        return worktree
    if workdir == project_path:
        return worktree
    if workdir.startswith(project_path.rstrip("/") + "/"):
        return str(Path(worktree) / workdir[len(project_path.rstrip("/")) + 1:])
    return workdir


def run_tests(project: dict, commands: list[dict], worktree: str | None = None
              ) -> tuple[str | None, bool | None, str]:
    """Lance la commande de test du projet, puis le lint s'il existe."""
    by_name = {c["name"]: c for c in commands}
    chosen = [by_name[name] for name in ("test", "lint") if name in by_name]
    if not chosen:
        return None, None, "aucune commande de test enregistrée pour ce projet"

    outputs, ok = [], True
    for entry in chosen:
        try:
            cwd = in_worktree(entry.get("workdir"), project["path"],
                              worktree or project["path"])
            result = subprocess.run(entry["command"], shell=True, cwd=cwd,
                                    capture_output=True, text=True, timeout=1800)
        except subprocess.SubprocessError as error:
            outputs.append(f"$ {entry['command']}\n(échec du lancement : {error})")
            ok = False
            continue
        outputs.append(f"$ {entry['command']}\n{(result.stdout + result.stderr)[-TESTS_TAIL:]}")
        if result.returncode != 0:
            ok = False
            break  # inutile de lancer le lint si les tests sont déjà rouges
    return " · ".join(e["command"] for e in chosen), ok, "\n\n".join(outputs)[-TESTS_TAIL:]


def changed_files(path: str) -> list[str]:
    """Chemins absolus des fichiers modifiés dans l'arbre."""
    out = []
    for line in git(path, "status", "--porcelain").splitlines():
        name = line[3:].strip().strip('"')
        if " -> " in name:            # renommage : on retient la destination
            name = name.split(" -> ")[-1]
        if name:
            out.append(str(Path(path) / name))
    return out


def commit(project: dict, task: dict, log_path: Path, allow_all: bool,
           only: list[str] | None = None) -> tuple[str | None, list[str]]:
    """Commite le travail de l'agent — et rien d'autre.

    On ne commite que les fichiers que l'agent déclare avoir écrits dans son
    propre journal. Tout ce qui a bougé sans qu'il l'ait touché appartient à
    quelqu'un d'autre — une session interactive travaillant sur le même dépôt —
    et n'a rien à faire dans son commit, sous son message.

    Renvoie (commit produit, fichiers étrangers laissés de côté).
    """
    path = project["path"]
    if not is_git_repo(path):
        return None, []
    modified = changed_files(path)
    if not modified:
        return None, []

    written = only if only is not None else runlog.written_paths(str(log_path))
    if allow_all:
        # Tâche de rangement : son objet EST le désordre existant.
        to_commit, foreign = modified, []
    else:
        to_commit = [f for f in modified if f in written]
        foreign = [f for f in modified if f not in written]

    if not to_commit:
        log(f"aucun fichier attribuable à l'agent — rien n'est commité "
            f"({len(foreign)} fichier(s) laissé(s) en l'état)")
        return None, foreign

    message = (f"{task['title']}\n\n"
               f"Tâche #{task['id']} traitée par la file d'agents de claude-manager.")
    subprocess.run(["git", "-C", path, "add", "--", *to_commit],
                   capture_output=True, timeout=60)
    subprocess.run(["git", "-C", path, "commit", "-m", message],
                   capture_output=True, text=True, timeout=120)
    if foreign:
        log(f"{len(foreign)} fichier(s) modifié(s) hors de l'agent, laissé(s) non commités")
    return head(path), foreign


"""Délai laissé à un agent auxiliaire — celui qui résout une fusion.

Plus court que celui d'un agent de tâche : résoudre un conflit, c'est arbitrer
entre deux versions d'un même passage, pas concevoir. Au-delà, c'est que le
conflit dépasse ce qu'on peut confier à une machine sans regarder.
"""
CONFLIT_TIMEOUT = 900


def collisions(written: dict[int, list[str]]) -> dict[int, list[str]]:
    """Fichiers écrits par plus d'un agent du même lot.

    Vestige du temps où plusieurs agents partageaient une copie de travail. Avec
    un worktree par tâche ils ne peuvent plus s'écraser ; la fonction reste parce
    qu'un lot peut encore en contenir plusieurs sur demande explicite.
    """
    par_fichier: dict[str, list[int]] = {}
    for task_id, paths in written.items():
        for path in paths:
            par_fichier.setdefault(path, []).append(task_id)
    fautifs: dict[int, list[str]] = {}
    for path, ids in par_fichier.items():
        if len(ids) > 1:
            for task_id in ids:
                fautifs.setdefault(task_id, []).append(path)
    return fautifs


def run_batch(project: dict, tasks: list[dict], state: dict, reason: str = "") -> None:
    """Lance un lot d'agents sur un projet, puis teste et commite une seule fois.

    Les tests ne peuvent pas tourner pendant qu'un agent écrit : on attend donc
    la fin de tout le lot avant de les lancer, une seule fois pour l'ensemble.
    """
    path = project["path"]
    group_id = uuid.uuid4().hex[:8] if len(tasks) > 1 else None
    if len(tasks) > 1:
        log(f"⇉ lot de {len(tasks)} agents sur {project['slug']} — {reason}")

    claimed: list[dict] = []
    before = head(path)
    # Une branche par lot : au sein d'un lot les périmètres sont réputés
    # disjoints, et une branche par agent obligerait à jongler entre plusieurs
    # copies de travail — c'est le rôle des worktrees, pas de celui-ci.
    # Une tâche `allow_dirty` a pour objet le désordre de la copie PRINCIPALE :
    # l'envoyer dans un worktree neuf, net par construction, lui ferait chercher
    # ce qui n'y est pas. Elle travaille donc là où est le désordre.
    if tasks[0].get("allow_dirty"):
        work, branche, base = path, None, current_branch(path)
        log(f"⌥ #{tasks[0]['id']} travaille dans la copie principale (rangement)")
    else:
        work, branche, base = open_worktree(project, tasks[0])
    if branche:
        log(f"⌥ {branche} dans {Path(work).name}")
    # L'agent, les tests et le commit travaillent dans la copie dédiée ; la
    # copie principale de Kevin n'est jamais touchée.
    atelier = {**project, "path": work}
    for task in tasks:
        answer = api("/api/queue/claim", {
            "task_id": task["id"], "commit_before": before, "group_id": group_id,
            # Chaque tâche a sa copie : le parallélisme sur un même projet est
            # sans danger. Une tâche de rangement, elle, travaille dans la copie
            # principale et doit rester seule.
            "allow_parallel": bool(branche),
            "branch": branche, "worktree": work if branche else None})
        if answer.get("claimed"):
            claimed.append(answer)
    if not claimed:
        # **Le worktree a été ouvert avant la réclamation : il faut le refermer.**
        # Sinon chaque refus en laisse un derrière lui — et le démon revient
        # toutes les dix secondes. Cinq copies de trente méga-octets sont
        # apparues ainsi en une minute, avec leurs branches.
        if branche and work != path:
            drop_worktree(path, work)
            log(f"⌫ réclamation refusée pour #{tasks[0]['id']} — worktree {Path(work).name} refermé")
        return

    results: dict[int, dict] = {}
    threads = []

    def travaille(entry: dict) -> None:
        run, full_task = entry["run"], entry["task"]
        log_path = LOG_DIR / f"run-{run['id']}.log"
        log(f"▶ #{full_task['id']} {full_task['title']} ({project['slug']})")
        code, stopped = run_agent(full_task, atelier, log_path,
                                  state.get("agent_timeout", 2700), run_id=run["id"])
        results[full_task["id"]] = {"run": run, "task": full_task, "code": code,
                                    "stopped": stopped, "log_path": log_path,
                                    "written": runlog.written_paths(str(log_path))}

    for entry in claimed:
        thread = threading.Thread(target=travaille, args=(entry,), daemon=True)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()

    fautifs = collisions({k: v["written"] for k, v in results.items()})
    if fautifs:
        log(f"⚠ collision : {len(fautifs)} tâche(s) ont écrit les mêmes fichiers")

    commands = claimed[0]["commands"]
    tout_interrompu = all(r["stopped"] for r in results.values())
    if tout_interrompu:
        tests_command, tests_ok, tests_output = None, None, "exécution interrompue"
        log("exécution interrompue : tests non lancés")
    else:
        tests_command, tests_ok, tests_output = run_tests(project, commands, worktree=work)
    diff_stat = git(work, "diff", "--stat", "HEAD")

    # L'aperçu est lancé une fois le travail fini, pour que Kevin puisse
    # l'essayer pendant sa relecture.
    apercu, port = (None, None)
    if branche and tests_ok is not False and not fautifs:
        apercu, port = start_preview(project, tasks[0], commands, work, branche,
                                     state.get("used_ports", []))

    for task_id, res in results.items():
        run, full_task = res["run"], res["task"]
        commit_after, foreign, collision = None, [], fautifs.get(task_id)

        if res["stopped"]:
            status, task_status = "stopped", "needs_input"
            summary = ("Exécution interrompue — l'agent répétait le même geste, ou "
                       "l'arrêt a été demandé. Son travail est resté dans son worktree.")
        elif collision:
            status, task_status = "failed", "review"
            summary = ("Collision : ces fichiers ont aussi été écrits par une autre "
                       "tâche du même lot, rien n'a été commité — "
                       + ", ".join(Path(f).name for f in collision[:6]))
        elif tests_ok is False:
            status, task_status = "failed", "review"
            summary = "Tests en échec — rien n'a été commité, l'arbre porte les modifications."
        else:
            commit_after, foreign = commit(atelier, full_task, res["log_path"],
                                           allow_all=bool(full_task.get("allow_dirty")),
                                           only=res["written"])
            status = "passed" if res["code"] == 0 else "error"
            task_status = "review"
            summary = ("Travail terminé, tests verts." if res["code"] == 0
                       else f"L'agent s'est arrêté avec le code {res['code']}.")
            if foreign:
                summary += (f" {len(foreign)} fichier(s) modifié(s) hors de cet agent : "
                            "laissés non commités.")

        api(f"/api/runs/{run['id']}/finish", {
            "status": status, "task_status": task_status, "commit_after": commit_after,
            "diff_stat": diff_stat[-2000:] if diff_stat else None,
            "tests_command": tests_command, "tests_ok": tests_ok,
            "tests_output": tests_output, "summary": summary, "exit_code": res["code"],
            "log_path": str(res["log_path"]),
            "foreign_files": "\n".join(foreign) or None,
            "collision": "\n".join(collision) if collision else None,
            "branch": branche, "base_branch": base,
            "worktree": work if branche else None,
            "preview_url": apercu, "preview_port": port,
        })
        log(f"■ #{task_id} → {status} (tests : {tests_ok})")


def plan_and_launch(state: dict) -> None:
    """Décide ce qui part maintenant — **une tâche, un worktree, une branche**.

    Chaque agent travaille dans sa propre copie, sur sa propre branche. Deux
    agents ne partagent donc plus aucun fichier, même sur le même projet : c'est
    ce qui permet d'en faire tourner plusieurs de front sans juge et sans risque
    de collision. `MAX_PARALLEL` redevient la seule limite.

    Ça ne supprime pas les désaccords, ça les **déplace** : deux branches qui
    touchent les mêmes lignes ne s'en apercevront qu'à la fusion. C'est un bien
    meilleur endroit pour s'en apercevoir — git sait le dire exactement, alors
    que deux agents dans une même copie s'écrasent en silence.

    Une tâche `allow_dirty` reste à part : son objet est le désordre de la copie
    principale, donc elle y travaille, et elle exige d'être seule sur le projet.
    """
    running = state.get("running_all") or []
    libre = max(0, int(state.get("max_parallel", 3)) - len(running))
    if libre <= 0:
        return
    occupes = {r["project_id"] for r in running}

    for task in state.get("pending", []):
        if libre <= 0:
            break
        slug, path, pid = task["project_slug"], task.get("project_path"), task["project_id"]
        if not path or not Path(path).is_dir():
            continue

        # Une tâche de rangement travaille dans la copie principale : elle ne
        # supporte donc personne d'autre sur le projet, ni comme voisin ni comme
        # suivant. C'est la seule exception au parallélisme.
        rangement = bool(task.get("allow_dirty"))
        with _batch_lock:
            if task["id"] in _batches:
                continue
            solitaires = {s for s, seul in _solo.items() if seul}
            if rangement and (pid in occupes or slug in solitaires):
                continue
            if not rangement and slug in solitaires:
                continue

        sale = is_git_repo(path) and not tree_is_clean(path)
        if sale and not rangement:
            note_once(slug, f"{slug} : dépôt non propre, la file attend une relecture")
            continue
        _last_note.pop(slug, None)

        libre -= 1
        with _batch_lock:
            _batches.add(task["id"])
            if rangement:
                _solo[slug] = True
        project = {"slug": slug, "path": path, "id": pid}
        threading.Thread(target=_batch_thread, args=(project, [task], state, ""),
                         daemon=True).start()


def _batch_thread(project: dict, lot: list[dict], state: dict, raison: str) -> None:
    try:
        run_batch(project, lot, state, raison)
    except Exception as error:  # un agent qui casse ne doit pas emporter le démon
        log(f"agent sur {project['slug']} en erreur : {error}")
    finally:
        with _batch_lock:
            for tache in lot:
                _batches.discard(tache["id"])
            _solo.pop(project["slug"], None)


def tidy_worktrees() -> None:
    """Ménage : un worktree dont la tâche est close n'a plus de raison d'être.

    On arrête l'aperçu, on retire la copie de travail, et on supprime la branche
    si elle a été fusionnée. Une branche non fusionnée est conservée : elle
    contient du travail que personne n'a validé.
    """
    answer = api("/api/worktrees")
    for item in answer.get("worktrees", []):
        if item.get("task_status") not in ("done", "cancelled"):
            continue
        if item.get("merge_state") == "requested":
            continue  # la fusion doit passer avant le ménage
        worktree, base = item.get("worktree"), item.get("project_path")
        if worktree and Path(worktree).is_dir():
            project = {"slug": item.get("project_slug"), "path": base}
            stop_preview(project, item.get("commands") or [], worktree,
                         item.get("preview_port"), item["task_id"])
            drop_worktree(base, worktree)
            log(f"⌫ worktree de #{item['task_id']} retiré")
        if item.get("merge_state") == "merged" and item.get("branch"):
            subprocess.run(["git", "-C", base, "branch", "-d", item["branch"]],
                           capture_output=True, timeout=60)
        api(f"/api/worktrees/{item['id']}/forget", {})


def process_merges() -> None:
    """Exécute les fusions demandées depuis l'interface.

    On ne fusionne que sur un arbre net et sans agent en cours sur le projet :
    changer de branche sous les pieds d'un agent le ferait travailler ailleurs
    qu'il ne croit.
    """
    answer = api("/api/merges/pending")
    for demande in answer.get("merges", []):
        path, branche = demande.get("project_path"), demande.get("branch")
        base = demande.get("base_branch") or "master"
        if not path or not branche:
            api(f"/api/merges/{demande['id']}/result",
                {"state": "conflict", "detail": "aucune branche enregistrée"})
            continue
        if not tree_is_clean(path):
            note_once(f"merge-{demande['id']}",
                      f"fusion de {branche} en attente : dépôt non propre")
            continue

        subprocess.run(["git", "-C", path, "checkout", base],
                       capture_output=True, text=True, timeout=60)
        message = f"Fusion de la tâche #{demande['task_id']} — {demande.get('title', '')}"
        out = subprocess.run(["git", "-C", path, "merge", "--no-ff", branche, "-m", message],
                             capture_output=True, text=True, timeout=180)
        if out.returncode == 0:
            # **On teste le résultat de la fusion, pas seulement le worktree.**
            # Les tests de l'agent tournent dans sa copie, où tout est présent ;
            # si son commit n'emporte qu'une partie du travail, la base reçoit du
            # code incomplet et personne ne le voit. C'est arrivé trois fois sur
            # mare le 10/08 — 403 lignes de test fusionnées sans l'implémentation
            # qu'elles décrivent, et un master rouge pendant des heures.
            fusion = git(path, "rev-parse", "HEAD")
            projet = {"slug": demande.get("project_slug"), "path": path}
            _, verts, sortie = run_tests(projet, demande.get("commands") or [])

            if verts is False:
                # La branche n'a pas encore été supprimée : c'est voulu. La
                # supprimer avant de savoir si la base tient reviendrait à jeter
                # le travail au moment précis où l'on en a besoin.
                if git(path, "rev-parse", "HEAD") == fusion:
                    subprocess.run(["git", "-C", path, "reset", "--hard", "HEAD~1"],
                                   capture_output=True, timeout=60)
                    etat, mot = "reverted", "défaite"
                else:
                    # Quelqu'un a commité par-dessus : défaire d'autorité
                    # emporterait son travail. On alerte et on laisse la main.
                    etat, mot = "broken", "laissée en place (base avancée depuis)"
                log(f"⚠ fusion de {branche} {mot} : tests rouges sur {base}")
                api(f"/api/merges/{demande['id']}/result",
                    {"state": etat,
                     "detail": f"Tests en échec sur {base} après fusion — "
                               f"fusion {mot}. La branche {branche} est conservée.\n\n"
                               f"{sortie[-2000:]}"})
                continue

            subprocess.run(["git", "-C", path, "branch", "-d", branche],
                           capture_output=True, timeout=60)
            etat_tests = "tests verts sur la base" if verts else "aucun test déclaré"
            log(f"⇢ {branche} fusionnée dans {base} — {etat_tests}")
            api(f"/api/merges/{demande['id']}/result",
                {"state": "merged",
                 "detail": f"{out.stdout.strip()[:600]}\n\n{etat_tests}."})
        else:
            # On ne laisse jamais la copie principale à moitié fusionnée : elle
            # est celle de Kevin, et un dépôt en conflit y bloquerait tout.
            subprocess.run(["git", "-C", path, "merge", "--abort"],
                           capture_output=True, timeout=60)
            detail = (out.stdout + out.stderr).strip()[:600]
            premiere = detail.splitlines()[0][:80] if detail else "conflit"
            log(f"⚠ conflit en fusionnant {branche} : {premiere}")

            if resolve_conflict(demande, path, base, branche):
                # La branche porte maintenant la base : on retente, et cette
                # fois la fusion ne peut plus buter sur les mêmes passages.
                api(f"/api/merges/{demande['id']}/result",
                    {"state": "requested",
                     "detail": "Conflits résolus par un agent, fusion relancée."})
                continue

            api(f"/api/merges/{demande['id']}/result",
                {"state": "conflict", "detail": detail})


def apply_doc_edits() -> None:
    """Applique les modifications de documentation demandées depuis l'interface.

    C'est ici que ça s'écrit, et nulle part ailleurs : le conteneur monte les
    dépôts en lecture seule, et le démon tourne avec les droits de `dev` — les
    mêmes que les agents et que Kevin. Un fichier écrit par le conteneur
    appartiendrait à root et deviendrait inéditable ensuite.

    Trois refus, tous pour la même raison — ne jamais écraser un travail :
    un agent en cours sur le projet, le fichier déjà modifié dans l'arbre, ou
    la branche qui a changé depuis la demande.
    """
    answer = api("/api/doc-edits/pending")
    demandes = answer.get("edits", [])
    if not demandes:
        return
    # Une seule fois pour tout le lot : cette route déclenche aussi la détection
    # des tâches orphelines, inutile de la solliciter par demande.
    occupes = {r.get("project_id")
               for r in (api("/api/queue/state").get("running_all") or [])}
    for demande in demandes:
        edit_id, chemin = demande["id"], demande["path"]
        racine = demande.get("project_path") or ""

        def refuse(raison: str) -> None:
            log(f"⚠ modification de {chemin} refusée : {raison}")
            api(f"/api/doc-edits/{edit_id}/result",
                {"state": "failed", "detail": raison})

        if not racine or not Path(racine).is_dir():
            refuse("chemin du projet introuvable"); continue
        cible = (Path(racine) / chemin).resolve()
        try:
            cible.relative_to(Path(racine).resolve())
        except ValueError:
            refuse("chemin hors du projet"); continue
        if cible.suffix.lower() != ".md":
            refuse("seuls les fichiers .md sont modifiables ici"); continue

        if demande["project_id"] in occupes:
            refuse("un agent travaille sur ce projet"); continue

        # Le fichier a-t-il bougé depuis la demande ? On ne écrase pas.
        if git(racine, "status", "--porcelain", "--", chemin).strip():
            refuse("ce fichier a des modifications non commitées dans le dépôt")
            continue
        courante = current_branch(racine)
        if demande.get("branch") and demande["branch"] != courante:
            refuse(f"la branche a changé depuis la demande "
                   f"({demande['branch']} → {courante})")
            continue

        try:
            cible.parent.mkdir(parents=True, exist_ok=True)
            cible.write_text(demande["content"], encoding="utf-8")
        except OSError as erreur:
            refuse(f"écriture impossible : {erreur}"); continue

        if not git(racine, "status", "--porcelain", "--", chemin).strip():
            api(f"/api/doc-edits/{edit_id}/result",
                {"state": "applied", "detail": "aucun changement — texte identique"})
            log(f"⌁ {chemin} inchangé, rien à commiter")
            continue

        # **Ce chemin et lui seul.** Un `commit -a` emporterait le travail en
        # cours de quelqu'un d'autre dans le même commit.
        message = f"docs : {chemin} modifié depuis le manager"
        out = subprocess.run(["git", "-C", racine, "commit", "-m", message, "--", chemin],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            detail = (out.stdout + out.stderr).strip()[:400]
            refuse(f"commit refusé : {detail}"); continue
        commit = git(racine, "rev-parse", "--short", "HEAD")
        log(f"✎ {chemin} commité sur {courante} ({commit})")
        api(f"/api/doc-edits/{edit_id}/result",
            {"state": "applied", "commit": commit,
             "detail": f"commité sur {courante}"})


def resolve_conflict(demande: dict, path: str, base: str, branche: str) -> bool:
    """Envoie un agent résoudre les conflits, **dans le worktree de la tâche**.

    On ne résout jamais dans la copie principale. On fusionne la base DANS la
    branche, là-bas : si l'agent s'égare, le dégât reste confiné à une copie
    jetable, et la copie principale n'a jamais quitté un état propre. Une fois la
    base absorbée, la fusion vers la base n'a plus de passage litigieux.

    Renvoie True si la branche porte désormais la base **et** que les tests y
    passent. Dans tous les autres cas on rend la main : un conflit qu'une machine
    ne sait pas trancher est un conflit qu'il faut regarder.
    """
    # **Une seule tentative par exécution.** La résolution remet la fusion en
    # « demandée » ; si elle reconflictait, on repartirait pour un tour, et le
    # démon sonde toutes les dix secondes. Une boucle d'agents coûte cher et ne
    # converge pas : ce qui a résisté deux fois demande un humain.
    with _batch_lock:
        if demande["id"] in _conflits_tentes:
            log(f"conflit sur {branche} : déjà tenté une fois, on rend la main")
            return False
        _conflits_tentes.add(demande["id"])

    work = demande.get("worktree")
    if not work or not Path(work).is_dir():
        log("conflit : plus de worktree pour cette tâche, résolution impossible")
        return False
    if not tree_is_clean(work):
        log("conflit : le worktree porte du travail non commité, on n'y touche pas")
        return False

    out = subprocess.run(["git", "-C", work, "merge", base, "-m",
                          f"Absorber {base} dans {branche} avant fusion"],
                         capture_output=True, text=True, timeout=180)
    if out.returncode == 0:
        log(f"⇢ {base} absorbée dans {branche} sans conflit")
        return _tests_verts_apres_resolution(demande, work, branche)

    fichiers = git(work, "diff", "--name-only", "--diff-filter=U")
    log(f"⚑ agent de résolution sur {branche} — {len(fichiers.splitlines())} fichier(s)")
    prompt = f"""Une fusion de `{base}` dans `{branche}` s'est arrêtée sur des conflits.
Tu es dans le dépôt, la fusion est en cours, les marqueurs sont en place.

Fichiers en conflit :
{fichiers}

Résous chaque conflit en gardant **les deux intentions** : la branche apporte le \
travail de la tâche #{demande.get('task_id')} — « {demande.get('title', '')} » — et \
`{base}` a avancé pendant ce temps. Ne choisis pas un côté par facilité ; si les \
deux modifient la même fonction, il faut le plus souvent les combiner.

Ensuite :
1. `git add` les fichiers résolus, puis `git commit --no-edit`.
2. Lance les tests du projet et corrige ce qui casse.

Ne touche à rien d'autre que ce qu'exige la résolution. Si un conflit demande un \
arbitrage que tu ne peux pas trancher — deux comportements incompatibles voulus \
par deux tâches — arrête-toi, laisse `git merge --abort`, et explique pourquoi."""

    journal = LOG_DIR / f"conflit-{demande['id']}.log"
    try:
        with journal.open("a", encoding="utf-8") as sortie:
            sortie.write(f"\n=== résolution de conflit {branche} → {base} ===\n")
            subprocess.run(["claude", "-p", prompt, "--permission-mode", "auto",
                            "--output-format", "stream-json", "--verbose"],
                           cwd=work, stdout=sortie, stderr=subprocess.STDOUT,
                           timeout=CONFLIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as erreur:
        log(f"agent de résolution indisponible ({erreur})")
        subprocess.run(["git", "-C", work, "merge", "--abort"],
                       capture_output=True, timeout=60)
        return False

    if git(work, "diff", "--name-only", "--diff-filter=U"):
        log("l'agent a laissé des conflits non résolus — on rend la main")
        subprocess.run(["git", "-C", work, "merge", "--abort"],
                       capture_output=True, timeout=60)
        return False
    # `--verify --quiet` est indispensable : sans lui, `rev-parse MERGE_HEAD`
    # RÉAFFICHE son argument quand la référence n'existe pas. La chaîne
    # « MERGE_HEAD » étant vraie, une fusion proprement conclue passait pour une
    # fusion en cours, et une résolution correcte était jetée.
    if git(work, "rev-parse", "--verify", "--quiet", "MERGE_HEAD") or not tree_is_clean(work):
        log("l'agent n'a pas conclu la fusion — on rend la main")
        subprocess.run(["git", "-C", work, "merge", "--abort"],
                       capture_output=True, timeout=60)
        return False
    return _tests_verts_apres_resolution(demande, work, branche)


def _tests_verts_apres_resolution(demande: dict, work: str, branche: str) -> bool:
    """Les tests passent-ils sur la branche une fois la base absorbée ?

    C'est la même exigence qu'après une fusion vers la base : une résolution qui
    compile mais casse les tests n'est pas une résolution.
    """
    projet = {"slug": demande.get("project_slug"), "path": work}
    _, verts, _ = run_tests(projet, demande.get("commands") or [])
    if verts is False:
        log(f"⚠ tests rouges sur {branche} après résolution — on rend la main")
        return False
    return True


def reconcile(state: dict) -> None:
    """Au démarrage, une exécution encore marquée « running » est un vestige :
    le démon a été tué pendant qu'un agent travaillait.

    Si le dépôt est resté propre, l'agent n'avait rien écrit — la tâche n'a rien
    appris de l'interruption et retourne simplement en file. S'il a laissé des
    modifications, la décision revient à l'humain : on ne relance pas un agent
    par-dessus un travail à moitié fait.
    """
    running = state.get("running")
    if not running:
        return
    path = (running.get("project_path")
            or _project_path(running.get("project_slug"), state))
    dirty = bool(path) and is_git_repo(path) and not tree_is_clean(path)
    if dirty:
        task_status = "needs_input"
        summary = ("Le démon a été interrompu pendant l'exécution et l'agent a laissé "
                   "des modifications non commitées. À examiner avant de relancer.")
    else:
        task_status = "queued"
        summary = ("Le démon a été interrompu avant que l'agent n'écrive quoi que ce "
                   "soit — la tâche retourne en file, sans conséquence.")
    log(f"exécution orpheline #{running['id']} → tâche remise en « {task_status} »")
    api(f"/api/runs/{running['id']}/finish", {
        "status": "stopped", "task_status": task_status, "summary": summary,
    })


def _project_path(slug: str | None, state: dict) -> str | None:
    for task in state.get("pending", []):
        if task.get("project_slug") == slug:
            return task.get("project_path")
    return None


def shutdown(signum, _frame) -> None:
    """Arrêt propre : on tue l'agent, on le dit, et la reprise fera le ménage."""
    log(f"signal {signum} reçu — arrêt")
    for process in list(_processes.values()):
        if process.poll() is None:
            process.kill()
    raise SystemExit(0)


def main() -> None:
    log(f"démon de file démarré — {BASE_URL}, sondage toutes les {POLL_SECONDS}s")
    first = True
    while True:
        state = api("/api/queue/state")
        if "_error" not in state:
            state["used_ports"] = api("/api/worktrees").get("used_ports", [])
        if "_error" in state:
            log(f"manager injoignable ({state['_error']}), nouvelle tentative")
            time.sleep(POLL_SECONDS)
            continue

        if first:
            reconcile(state)
            first = False
        elif state.get("stop_requested"):
            api("/api/queue/ack-stop", {})

        if not state.get("paused"):
            plan_and_launch(state)
        process_merges()
        apply_doc_edits()
        tidy_worktrees()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        for process in list(_processes.values()):
            if process.poll() is None:
                process.kill()
        sys.exit(0)
