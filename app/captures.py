"""Captures d'écran des signalements : contrôle, rangement, service.

Un fichier envoyé par un inconnu est traité comme hostile :
- le type est reconnu **au contenu** (signature des premiers octets), jamais à
  l'extension ni au type déclaré par le navigateur ;
- seuls PNG, JPEG, GIF et WebP passent. Pas de SVG : c'est du XML qui peut
  porter du script, et il s'afficherait sur notre domaine ;
- le nom d'origine n'est gardé que pour l'affichage ; sur le disque, le fichier
  porte un nom tiré au hasard ;
- taille et nombre sont plafonnés.
"""
import secrets
from pathlib import Path

from . import config, repo

RACINE = config.DATA_DIR / "support-files"
TAILLE_MAX = 8 * 1024 * 1024
PAR_MESSAGE = 5
PAR_TICKET = 30

SIGNATURES = [
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
]


def reconnait(debut: bytes) -> tuple[str, str] | None:
    """(type, extension) d'après les premiers octets, ou None."""
    for signature, mime, ext in SIGNATURES:
        if debut.startswith(signature):
            return mime, ext
    if debut[:4] == b"RIFF" and debut[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


async def lis(upload) -> tuple[bytes, str, str] | None:
    """Lit un fichier envoyé. None s'il est vide, trop gros ou pas une image."""
    donnees = await upload.read(TAILLE_MAX + 1)
    if not donnees or len(donnees) > TAILLE_MAX:
        return None
    type_ = reconnait(donnees[:16])
    if type_ is None:
        return None
    return donnees, type_[0], type_[1]


def range_(donnees: bytes, ext: str, dossier: str) -> str:
    """Écrit le fichier et renvoie son chemin relatif à RACINE."""
    relatif = Path(dossier) / f"{secrets.token_hex(12)}.{ext}"
    chemin = RACINE / relatif
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_bytes(donnees)
    return str(relatif)


def chemin(fichier: dict) -> Path | None:
    """Chemin sur le disque, en refusant tout ce qui sortirait de RACINE."""
    p = (RACINE / fichier["stored"]).resolve()
    if RACINE.resolve() not in p.parents or not p.is_file():
        return None
    return p


def efface(stored: str | None) -> None:
    if stored:
        p = (RACINE / stored).resolve()
        if RACINE.resolve() in p.parents:
            p.unlink(missing_ok=True)


def nom_affiche(nom: str | None) -> str:
    nom = Path(nom or "capture").name
    return "".join(c for c in nom if c.isprintable())[:120] or "capture"


async def lis_tous(uploads) -> list[tuple[bytes, str, str, str]]:
    """Les images valides parmi les fichiers envoyés : (données, type, ext, nom)."""
    lues = []
    for upload in uploads[:PAR_MESSAGE]:
        if not getattr(upload, "filename", None):
            continue
        lu = await lis(upload)
        if lu is not None:
            lues.append((*lu, nom_affiche(upload.filename)))
    return lues


def joins(lues, ticket: dict, message_id: int) -> int:
    """Range les captures déjà lues d'un message de signaleur."""
    deja = sum(1 for f in repo.ticket_files(ticket["id"]) if f["source"] == "user")
    gardees = 0
    for donnees, mime, ext, nom in lues:
        if deja + gardees >= PAR_TICKET:
            break
        stored = range_(donnees, ext, f"tickets/{ticket['id']}")
        repo.add_support_file("user", ticket["project_id"], nom, mime, len(donnees), stored,
                              ticket_id=ticket["id"], message_id=message_id)
        gardees += 1
    return gardees
