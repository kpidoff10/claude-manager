# claude-manager — notes pour Claude

Cet outil sert à ne pas perdre le fil entre les sessions. Le tenir à jour fait
partie du travail, au même titre qu'écrire du code.

## Pendant une session sur n'importe quel projet

Le hook `SessionStart` injecte déjà le briefing : inutile d'appeler
`get_briefing` en début de session. En revanche :

- `update_task(status='in_progress')` **au moment** où on attaque une tâche, pas après
- `log_work` après chaque étape notable — résumé compréhensible hors contexte, dans trois semaines
- `add_memory(kind='decision')` **dès qu'une décision est prise**, avec le pourquoi ; rédigée en fin de session, elle est déjà oubliée
- `set_technology(status='deprecated', notes='...')` quand une piste est abandonnée : c'est ce qui évite de la reproposer
- `search` avant de créer une tâche ou une mémoire, pour ne pas dupliquer

## Conventions du code

- **Toutes** les requêtes SQL passent par `repo.py`. L'index de recherche FTS5
  est alimenté là et nulle part ailleurs — pas de triggers, une seule voie
  d'écriture.
- Les fonctions de `repo.py` acceptent un `conn=` optionnel : sans lui elles
  ouvrent leur propre transaction, avec lui elles s'inscrivent dans une
  transaction existante. C'est ce qui permet au briefing de tout lire d'un coup.
- L'interface web fonctionne **sans JavaScript** : chaque formulaire est un POST
  classique suivi d'une redirection. Le JavaScript ne fait qu'intercepter pour
  ne réactualiser que le panneau. Ne pas casser ce repli.
- Aucune dépendance externe côté navigateur : pas de CDN, pas de bibliothèque.

## Pièges

- L'URL du MCP se termine par une barre oblique : `/mcp/`. Sans elle, 307.
- Les chemins de projets sont des chemins **hôte**, valides dans le conteneur
  parce que `/home/dev/projects` y est monté au même endroit. Ne pas les
  réécrire.
- Ne jamais stocker de valeur secrète en base : `env_vars` ne prend que des noms.

## Après une modification du code

```bash
docker compose up -d --build
curl -s http://127.0.0.1:8099/health
```
