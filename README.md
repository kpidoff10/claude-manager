# claude-manager

L'état persistant de tes projets, partagé entre **Claude** (par MCP) et **toi**
(par une interface web).

Le problème résolu n'est pas « suivre des tâches » : c'est **repartir de zéro à
chaque session**. Claude relit le code, redécouvre les décisions, oublie ce qui
était prévu. Ici, un seul appel — `get_briefing` — rend l'état complet d'un
projet : avancement, jalons, tâches par priorité, décisions passées, stack,
commandes, services, variables d'environnement, journal.

Et comme un hook `SessionStart` fait cet appel automatiquement, la session
démarre déjà informée.

- **Interface web** : `https://<CM_DOMAIN>`
- **Point de terminaison MCP** : `https://<CM_DOMAIN>/mcp/`
  (la barre finale est obligatoire)

## Ce qu'on peut y gérer

| Domaine | Contenu |
|---|---|
| **Projets** | slug, chemin disque, dépôt, statut, avancement calculé |
| **Jalons** | phases nommées, date cible, avancement par phase |
| **Tâches** | priorité (someday → urgent), statut, propriétaire, tags, sous-tâches, ordre manuel |
| **Stack** | technologies avec version, rôle, statut (en usage / envisagée / abandonnée) et la raison du choix |
| **Préférences** | goûts techniques généraux, valables pour tous les projets |
| **Fiche projet** | commandes, services et URLs, variables d'environnement, ressources |
| **Mémoire** | décisions, conventions, pièges, contexte — épinglables |
| **Journal** | trace horodatée de ce qui a été fait |
| **Recherche** | plein texte sur tout, via SQLite FTS5 |

Le catalogue de technologies est global : on peut donc demander l'inverse —
*quels projets utilisent Docker ?*

## Architecture

Une base, deux faces. FastAPI sert l'interface web et le serveur MCP depuis le
même processus, sur la même base SQLite.

```
        SQLite (WAL + FTS5)
               │
          FastAPI
        ┌──────┴──────┐
   MCP (HTTP)      Interface web
   ← Claude        ← navigateur
```

- `app/schema.sql` — 11 tables plus l'index de recherche
- `app/repo.py` — **toutes** les requêtes ; seule voie d'écriture, y compris pour l'index
- `app/scanner.py` — détection de stack depuis les manifestes
- `app/briefing.py` — composition du briefing
- `app/mcp_server.py` — 31 outils MCP
- `app/web/` — routes, gabarits Jinja2, CSS et JavaScript sans dépendance
- `hooks/session_start.py` — hook Claude Code injectant le briefing

## Détection automatique de la stack

`scan_stack` lit `package.json`, `pyproject.toml`, `requirements.txt`,
`go.mod`, `Cargo.toml`, `composer.json`, `Dockerfile`, `docker-compose.yml` et
`.env.example`, à la racine et dans les sous-dossiers de premier niveau
(workspaces d'un monorepo). Il en déduit les technologies et leurs versions,
les commandes (scripts npm), les services (compose) et les variables
d'environnement attendues.

**Un scan n'écrase jamais une saisie manuelle** — sauf les numéros de version,
qui doivent suivre les manifestes.

## Exploitation

```bash
docker compose up -d --build     # construire et démarrer
docker logs -f claude-manager    # journaux
python3 scripts/mcp_call.py get_briefing '{"project":"mare"}'   # tester un outil
```

Les secrets vivent dans `.env`, hors de git — voir `.env.example`.
`/home/dev/projects` est monté en lecture seule dans le conteneur **au même
chemin que sur l'hôte**, pour que les chemins en base soient valides des deux
côtés.

## Sécurité

Le site est public. Deux voies d'authentification :

- `/mcp/` et `/api/` : token bearer (`CM_API_TOKEN`)
- l'interface web : mot de passe (`CM_WEB_PASSWORD`) et cookie signé

La table `env_vars` ne contient que des **noms** de variables, jamais de
valeurs.
