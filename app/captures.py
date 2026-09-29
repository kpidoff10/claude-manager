"""Pièces jointes des signalements : captures d'écran et fichiers.

Un fichier envoyé par un inconnu est traité comme hostile :
- le type est reconnu **au contenu** (signature des premiers octets) quand le
  format en a une, et l'extension doit être sur une liste fermée ;
- seules les images PNG, JPEG, GIF et WebP s'affichent dans la page. Pas de
  SVG : c'est du XML qui peut porter du script, et il s'afficherait sur notre
  domaine. Tout le reste est servi en **téléchargement**, jamais ouvert dans le
  navigateur ;
- une archive n'est **jamais décompressée ici**. On en lit seulement la table
  des matières (sans rien extraire), pour l'afficher ;
- le nom d'origine n'est gardé que pour l'affichage ; sur le disque, le fichier
  porte un nom tiré au hasard ;
- taille et nombre sont plafonnés.
"""
import secrets
import zipfile
from pathlib import Path

from . import config, repo

RACINE = config.DATA_DIR / "support-files"
IMAGE_MAX = 8 * 1024 * 1024
FICHIER_MAX = 25 * 1024 * 1024
TICKET_MAX = 100 * 1024 * 1024
PAR_MESSAGE = 5
PAR_TICKET = 30

IMAGES = [
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
]

ZIP = (b"PK\x03\x04", b"PK\x05\x06")
# extension → (type enregistré, signatures admises ; None = format sans
# signature fiable, accepté sur l'extension puisqu'il ne sera que téléchargé)
FICHIERS = {
    "zip": ("application/zip", ZIP),
    "pdf": ("application/pdf", (b"%PDF",)),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ZIP),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ZIP),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ZIP),
    "odt": ("application/vnd.oasis.opendocument.text", ZIP),
    "ods": ("application/vnd.oasis.opendocument.spreadsheet", ZIP),
    "odp": ("application/vnd.oasis.opendocument.presentation", ZIP),
    "7z": ("application/x-7z-compressed", (b"7z\xbc\xaf\x27\x1c",)),
    "rar": ("application/vnd.rar", (b"Rar!\x1a\x07",)),
    "psd": ("image/vnd.adobe.photoshop", (b"8BPS",)),
    "sketch": ("application/x-sketch", ZIP),
    "xd": ("application/vnd.adobe.xd", ZIP),
    "fig": ("application/x-figma", None),
    "ai": ("application/illustrator", None),
    "txt": ("text/plain", "texte"),
    "md": ("text/markdown", "texte"),
    "csv": ("text/csv", "texte"),
    "json": ("application/json", "texte"),
    "log": ("text/plain", "texte"),
}
ACCEPTE = ",".join(["image/png", "image/jpeg", "image/gif", "image/webp"]
                   + [f".{e}" for e in FICHIERS])


def est_image(fichier: dict) -> bool:
    return (fichier.get("mime") or "").startswith("image/") and \
        fichier["mime"] != "image/vnd.adobe.photoshop"


def reconnait_image(debut: bytes) -> tuple[str, str] | None:
    for signature, mime, ext in IMAGES:
        if debut.startswith(signature):
            return mime, ext
    if debut[:4] == b"RIFF" and debut[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def reconnait_fichier(donnees: bytes, nom: str) -> tuple[str, str] | None:
    ext = Path(nom).suffix.lower().lstrip(".")
    if ext not in FICHIERS:
        return None
    mime, signatures = FICHIERS[ext]
    if signatures == "texte":
        # Du texte, vraiment : UTF-8 lisible et sans octet nul.
        if b"\x00" in donnees[:65536]:
            return None
        try:
            donnees[:65536].decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif signatures and not any(donnees.startswith(s) for s in signatures):
        return None
    return mime, ext


async def lis(upload, images_seules: bool = False) -> tuple[bytes, str, str] | None:
    """Lit un fichier envoyé. None s'il est vide, trop gros ou refusé."""
    donnees = await upload.read(FICHIER_MAX + 1)
    if not donnees or len(donnees) > FICHIER_MAX:
        return None
    image = reconnait_image(donnees[:16])
    if image:
        return (donnees, *image) if len(donnees) <= IMAGE_MAX else None
    if images_seules:
        return None
    fichier = reconnait_fichier(donnees, upload.filename or "")
    return (donnees, *fichier) if fichier else None


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


def chemin_hote(fichier: dict) -> str | None:
    """Le même fichier vu depuis l'hôte, où tournent les agents."""
    if not config.HOST_DATA_DIR:
        return None
    return str(Path(config.HOST_DATA_DIR) / "support-files" / fichier["stored"])


def efface(stored: str | None) -> None:
    if stored:
        p = (RACINE / stored).resolve()
        if RACINE.resolve() in p.parents:
            p.unlink(missing_ok=True)


def nom_affiche(nom: str | None) -> str:
    nom = Path(nom or "fichier").name
    return "".join(c for c in nom if c.isprintable())[:120] or "fichier"


def table_zip(fichier: dict, limite: int = 200) -> list[dict] | None:
    """Table des matières d'une archive zip — lue, jamais extraite."""
    p = chemin(fichier)
    if p is None or fichier.get("mime") != "application/zip":
        return None
    try:
        with zipfile.ZipFile(p) as z:
            return [{"nom": i.filename, "taille": i.file_size}
                    for i in z.infolist()[:limite] if not i.is_dir()]
    except (zipfile.BadZipFile, OSError, ValueError):
        return None


def taille_lisible(octets: int) -> str:
    for unite in ("o", "Ko", "Mo"):
        if octets < 1024 or unite == "Mo":
            return f"{octets:.0f} {unite}" if unite == "o" else f"{octets:.1f} {unite}".replace(".", ",")
        octets /= 1024
    return f"{octets} o"


async def lis_tous(uploads, images_seules: bool = False) -> list[tuple[bytes, str, str, str]]:
    """Les fichiers acceptés parmi ceux envoyés : (données, type, ext, nom)."""
    lues = []
    for upload in uploads[:PAR_MESSAGE]:
        if not getattr(upload, "filename", None):
            continue
        lu = await lis(upload, images_seules=images_seules)
        if lu is not None:
            lues.append((*lu, nom_affiche(upload.filename)))
    return lues


def joins(lues, ticket: dict, message_id: int) -> int:
    """Range les pièces déjà lues d'un message de signaleur."""
    existantes = [f for f in repo.ticket_files(ticket["id"]) if f["source"] == "user"]
    nombre, volume = len(existantes), sum(f["size"] for f in existantes)
    gardees = 0
    for donnees, mime, ext, nom in lues:
        if nombre + gardees >= PAR_TICKET or volume + len(donnees) > TICKET_MAX:
            break
        stored = range_(donnees, ext, f"tickets/{ticket['id']}")
        repo.add_support_file("user", ticket["project_id"], nom, mime, len(donnees), stored,
                              ticket_id=ticket["id"], message_id=message_id)
        volume += len(donnees)
        gardees += 1
    return gardees
