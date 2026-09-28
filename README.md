# claude-manager

L'état persistant de tes projets, partagé entre **Claude Code** (par MCP et par
des hooks) et **toi** (par une interface web), plus une file d'agents qui
travaille pendant que tu fais autre chose.

Le problème résolu n'est pas « suivre des tâches » : c'est **repartir de zéro à
chaque session**. Claude relit le code, redécouvre les décisions, oublie ce qui
était prévu. Ici, un hook `SessionStart` injecte l'état complet du projet dès
l'ouverture de la session : avancement, tâches par priorité, décisions, pièges
déjà rencontrés, stack, commandes, services, journal.

> **Tu es une IA et on t'a confié ce dépôt pour l'installer ?** Va directement à
> [Installation](#installation). Chaque étape dit quoi faire, comment vérifier,
> et ce qui exige un humain. Lis d'abord [Règles pour une IA qui installe](#règles-pour-une-ia-qui-installe).

---

## Ce qu'il fait

| Domaine | Contenu |
|---|---|
| **Projets** | slug, chemin disque, dépôt, statut, avancement calculé |
| **Tâches** | priorité, statut, propriétaire, tags, sous-tâches, cases à cocher, tests manuels, rappels |
| **Mémoire** | décisions, conventions, pièges, **erreurs à ne pas reproduire** — injectées dans chaque briefing |
| **Stack** | technologies, versions, rôle, pistes abandonnées et pourquoi |
| **Journal** | trace horodatée de ce qui a été fait |
| **File d'agents** | une tâche mise en file est traitée par `claude -p` dans un worktree dédié, testée, commitée sur une branche ; tu relis puis tu fusionnes |
| **Retours d'expérience** | un agent qui échoue ou dont tu refuses le travail laisse une mémoire « erreur » pour les suivants |
| **Signalements** | des utilisateurs extérieurs décrivent un problème à une IA (qui lit le code en lecture seule) ; rien ne devient une tâche sans ta validation |
| **Notifications** | Telegram : question d'un agent, tests rouges, signalement reçu, rappel échu… |
| **Recherche** | plein texte sur tout (SQLite FTS5) |

## Architecture

```
                         ┌──────────── conteneur Docker ─────────────┐
 Claude Code ── MCP ───► │ FastAPI : /mcp/  /api/  interface web     │
 (hooks)    ── HTTP ───► │           /support (signaleurs)           │
 navigateur ───────────► │           /mcp-support/ (IA support)      │
                         │        SQLite (WAL + FTS5) dans ./data    │
                         └───────────────────▲───────────────────────┘
                                             │ HTTP (jeton)
                         ┌───────────────────┴───────────────────────┐
                         │ démon hôte : worker/agent_worker.py       │
                         │  lance `claude -p` (agents, leçons, IA    │
                         │  des signalements), git, tests, fusions   │
                         └───────────────────────────────────────────┘
```

Le conteneur ne peut rien lancer sur la machine : c'est le **démon**, sur
l'hôte, sous le compte qui a Claude Code, qui exécute les agents.

- `app/repo.py` — **toutes** les requêtes SQL ; seule voie d'écriture
- `app/briefing.py` — composition du briefing
- `app/mcp_server.py` — outils MCP de Claude
- `app/support_mcp.py` — MCP en lecture seule de l'IA des signalements
- `app/web/` — interface (sans dépendance JavaScript externe)
- `worker/agent_worker.py` — le démon
- `hooks/` — hooks Claude Code (`SessionStart` : briefing ; `Stop` : rappel de consigner)
- `skills/suivi-projet/` — skill Claude Code : quoi consigner et où

---

## Installation

### Prérequis

| Outil | Pourquoi | Vérifier |
|---|---|---|
| Linux | l'hôte | — |
| Docker + Compose v2 | le serveur | `docker compose version` |
| Python ≥ 3.10 sur l'hôte | hooks et démon (bibliothèque standard seulement) | `python3 --version` |
| git | démon, worktrees | `git --version` |
| Claude Code, **connecté** | le démon lance `claude -p` | `claude --version` puis `claude -p "dis ok"` |
| Traefik (facultatif) | exposition HTTPS publique | — |

L'utilisateur qui lance le démon doit pouvoir utiliser Docker
(`docker ps` sans sudo) et avoir Claude Code connecté.

### 1. Cloner

Cloner **dans le dossier qui contient tes projets** (le futur
`CM_PROJECTS_ROOT`) : le conteneur relit les journaux des agents à travers ce
montage.

```bash
cd ~/projects            # ton dossier de projets
git clone git@github.com:kpidoff10/claude-manager.git
cd claude-manager
mkdir -p data logs/runs
```

Ailleurs, ça marche aussi, à condition de poser `CM_RUN_LOG_DIR` (étape 2) sur
un chemin visible du conteneur.

### 2. Configurer `.env`

```bash
cp .env.example .env
python3 - <<'PY'
import secrets, re, pathlib
p = pathlib.Path(".env"); s = p.read_text()
for cle in ("CM_API_TOKEN", "CM_SESSION_SECRET"):
    s = re.sub(rf"^{cle}=.*$", f"{cle}={secrets.token_urlsafe(32)}", s, flags=re.M)
# Mot de passe web sans caractères ambigus (I/l, O/0).
alpha = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
mdp = "".join(secrets.choice(alpha) for _ in range(20))
s = re.sub(r"^CM_WEB_PASSWORD=.*$", f"CM_WEB_PASSWORD={mdp}", s, flags=re.M)
p.write_text(s)
PY
chmod 600 .env
```

Puis renseigner à la main :

| Variable | Valeur |
|---|---|
| `CM_PUBLIC_URL` | l'adresse publique, ex. `https://manager.mondomaine.fr` — ou `http://localhost:8099` sans exposition |
| `CM_DOMAIN` | le domaine seul, pour Traefik (`manager.mondomaine.fr`) ; `localhost` si pas de Traefik |
| `CM_PROJECTS_ROOT` | le dossier qui contient tes projets, ex. `/home/moi/projects`. Monté **au même chemin** dans le conteneur, en lecture seule |
| `CM_PREVIEW_HOST` | IP ou nom de la machine, pour les liens d'aperçu des agents |
| `CM_TELEGRAM_TOKEN` | facultatif (voir [Telegram](#telegram-facultatif)) |
| `CM_RUN_LOG_DIR` | chemin absolu de `logs/runs` dans ce dépôt, ex. `/home/moi/projects/claude-manager/logs/runs` |

Facultatives : `CM_MAX_PARALLEL` (agents simultanés, 3), `CM_AGENT_TIMEOUT`
(secondes par agent, 2700), `CM_MAX_RETRIES` (3), `CM_TZ` (`Europe/Paris`),
`CM_WORKTREES` (copies de travail des agents, `~/worktrees` — **hors** du
dossier des projets, pour qu'une copie ne soit jamais prise pour un projet).

### 3. Démarrer le serveur

```bash
docker compose up -d --build
curl -s http://127.0.0.1:8099/health      # → {"status":"ok",...}
```

Le serveur écoute sur `127.0.0.1:8099` (hooks et démon). Avec Traefik, les
labels de `docker-compose.yml` l'exposent en HTTPS sur `CM_DOMAIN` ; le
conteneur doit alors partager un réseau avec Traefik (ajouter ce réseau au
service si ton Traefik n'utilise pas le réseau par défaut).

**Le code est copié dans l'image** : après toute modification de `app/`,
`docker compose up -d --build`. Un simple `up -d` ne suffit que pour `.env`.

### 4. Brancher Claude Code

**MCP** (portée utilisateur : disponible dans tous les projets) :

```bash
TOKEN=$(grep ^CM_API_TOKEN= .env | cut -d= -f2)
claude mcp add --scope user --transport http claude-manager \
  "http://127.0.0.1:8099/mcp/" --header "Authorization: Bearer $TOKEN"
claude mcp list          # claude-manager doit apparaître connecté
```

La barre oblique finale de `/mcp/` est obligatoire. Sur une autre machine que
le serveur, utiliser `CM_PUBLIC_URL` + `/mcp/`.

**Hooks** — à **fusionner** dans `~/.claude/settings.json` (ne jamais écraser
le fichier : il contient d'autres réglages). Remplacer `CHEMIN` par le chemin
absolu du dépôt :

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "python3",
        "args": ["CHEMIN/hooks/session_start.py"], "timeout": 10,
        "statusMessage": "Chargement de l'état du projet…" } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command", "command": "python3",
        "args": ["CHEMIN/hooks/stop_check.py"], "timeout": 20,
        "statusMessage": "Vérification de l'état consigné…" } ] }
    ]
  }
}
```

Si `SessionStart` ou `Stop` existent déjà, **ajouter** un élément à leur
liste plutôt que de les remplacer.

**Skill** :

```bash
mkdir -p ~/.claude/skills
cp -r skills/suivi-projet ~/.claude/skills/
```

### 5. Lancer le démon (et qu'il survive aux redémarrages)

Avec systemd (recommandé) :

```bash
mkdir -p ~/.config/systemd/user
sed "s|__CHEMIN__|$PWD|g" deploy/claude-manager-worker.service \
  > ~/.config/systemd/user/claude-manager-worker.service
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now claude-manager-worker
tail -f logs/worker.log        # → « démon de file démarré »
```

Sans systemd utilisateur (conteneur, session sans bus D-Bus), au démarrage
de la machine via cron :

```bash
( crontab -l 2>/dev/null; echo "@reboot cd $PWD && python3 -u worker/agent_worker.py >> logs/worker.log 2>&1" ) | crontab -
cd "$PWD" && setsid nohup python3 -u worker/agent_worker.py >> logs/worker.log 2>&1 < /dev/null &
```

**Un seul démon à la fois.** Pour l'arrêter : `systemctl --user stop
claude-manager-worker`, ou `kill <pid>` — attention, `pkill -f agent_worker`
tue aussi le shell qui lance la commande, puisque sa ligne contient le motif.

### 6. Vérifier

```bash
curl -s http://127.0.0.1:8099/health
TOKEN=$(grep ^CM_API_TOKEN= .env | cut -d= -f2)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8099/api/queue/state
python3 scripts/mcp_call.py list_projects '{}'
grep "démon de file démarré" logs/worker.log | tail -1
```

Puis ouvrir `CM_PUBLIC_URL` dans un navigateur, se connecter avec
`CM_WEB_PASSWORD`, et ouvrir une session Claude Code dans un projet enregistré :
le briefing doit apparaître au démarrage.

**Enregistrer un premier projet** : depuis une session Claude Code dans le
dossier du projet, demander « enregistre ce projet dans claude-manager et
scanne sa stack » (outils `upsert_project` puis `scan_stack`).

---

## Règles pour une IA qui installe

- **Ne jamais afficher ni recopier** les valeurs de `.env` (jeton, mot de
  passe, secret) dans une réponse, un commit ou un journal. Donner le mot de
  passe web à l'humain une seule fois, à la fin, en lui disant de le noter.
- **Ne jamais écraser** `~/.claude/settings.json` ni `~/.claude.json` :
  fusionner. Faire une copie `.bak` avant d'y toucher.
- `.env`, `data/` et `logs/` ne vont **jamais** dans git (déjà dans `.gitignore`).
- Demander à l'humain, sans deviner : le domaine public, le dossier des
  projets, s'il y a un Traefik. Tout le reste a une valeur par défaut.
- Ce qui exige un humain, à lui signaler en fin d'installation :
  1. le DNS du domaine (si exposition publique) ;
  2. le bot Telegram (facultatif) ;
  3. les clés de déploiement GitHub pour l'IA des signalements (facultatif).
- Vérifier chaque étape par sa commande de contrôle avant de passer à la suivante.

---

## Telegram (facultatif)

1. Sur Telegram, écrire à **@BotFather**, `/newbot` → un jeton `1234:AA…`.
2. Le mettre dans `CM_TELEGRAM_TOKEN`, puis `docker compose up -d`.
3. **Écrire un premier message au bot** (sans ça il n'a pas le droit d'écrire).
4. Interface web › Notifications : coller l'identifiant de conversation, brancher le webhook, cocher les événements.

## Signalements (facultatif)

Des utilisateurs extérieurs (ex. des agences) déposent des tickets en
discutant avec une IA sur `/support`. Tu gères tout depuis **Signalements**
dans l'interface :

- **Comptes** : nom, identifiant, projets autorisés ; mot de passe généré, affiché une fois.
- **Fiche support** par projet : ce que l'IA sait du logiciel, écrit pour un utilisateur. Et, pour qu'elle lise le code, le dépôt SSH et la branche.
- **Validation** : chaque ticket envoyé attend ta décision. Valider crée une tâche (en file ou non) ; refuser demande une raison, visible par l'utilisateur.

**Lecture du code** : le démon tient une copie du dépôt dans
`~/.cache/claude-manager/support-code/` avec une clé dédiée, **sans droit
d'écriture** :

```bash
ssh-keygen -t ed25519 -N "" -C "claude-manager support (lecture seule)" -f ~/.ssh/cm_support_deploy
cat ~/.ssh/cm_support_deploy.pub
```

L'ajouter sur GitHub › dépôt › Settings › Deploy keys, **sans** « Allow write
access ». Une clé de déploiement ne vaut que pour un dépôt : pour plusieurs
dépôts, utiliser plutôt un compte machine en lecture seule.

**Ce que l'IA des signalements peut faire** : lire le code de la copie
(`Read`, `Grep`, `Glob`, fichiers de secrets interdits) et appeler le MCP
`/mcp-support/` (fiche support, signalements connus). Rien d'autre : ni
écriture, ni commande, ni MCP de claude-manager, ni mémoire du projet. Son
seul pouvoir est de proposer un texte de ticket.

## Sécurité

- `/mcp/` et `/api/` : jeton bearer (`CM_API_TOKEN`) ; interface web : mot de
  passe (`CM_WEB_PASSWORD`) et cookie signé.
- `/support` : comptes des signaleurs, session distincte (autre cookie, autre
  sel) qui n'ouvre ni l'interface, ni l'API, ni le MCP.
- `/mcp-support/` : jeton HMAC propre à chaque signalement, dérivé de
  `CM_SESSION_SECRET` ; il ne voit que le projet de ce signalement.
- La table `env_vars` ne contient que des **noms** de variables, jamais de valeurs.
- Changer `CM_SESSION_SECRET` déconnecte tout le monde et invalide le webhook
  Telegram (à rebrancher depuis Notifications).

## Exploitation

```bash
docker compose up -d --build            # après une modification du code
docker logs -f claude-manager           # journal du serveur
tail -f logs/worker.log                 # journal du démon
python3 scripts/mcp_call.py get_briefing '{"project":"mon-projet"}'
```

La base est `data/manager.db` (SQLite) : c'est le seul état à sauvegarder.
