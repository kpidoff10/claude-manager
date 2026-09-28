"""Rappels sur les tâches : « pas le temps de tester, rappelle-moi demain ».

Un rappel est une date posée sur une tâche. Quand elle arrive, un message part
sur Telegram avec trois boutons — une heure de plus, demain matin, c'est bon —
et la tâche porte un badge tant que le rappel n'est pas retiré.

**Tout est stocké en UTC, tout est dit à l'heure locale.** « Demain 9 h » se
comprend à l'heure de Kevin (`CM_TZ`, Paris par défaut), pas à celle du
serveur ; la base, elle, ne connaît que l'UTC, qui se compare sans surprise.
"""
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config

TZ = ZoneInfo(config.TIMEZONE)

HEURE_MATIN = 9
HEURE_SOIR = 18

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
JOURS_COURTS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]

# Les raccourcis proposés dans l'interface, dans l'ordre d'affichage.
RACCOURCIS = [("+1h", "Dans 1 h"), ("ce soir", "Ce soir 18 h"),
              ("demain", "Demain 9 h"), ("lundi", "Lundi 9 h")]


class DateIncomprise(ValueError):
    pass


def maintenant() -> datetime:
    return datetime.now(TZ)


def _a(jour: datetime, heure: int, minute: int = 0) -> datetime:
    return jour.replace(hour=heure, minute=minute, second=0, microsecond=0)


def interprete(texte: str, depuis: datetime | None = None) -> datetime:
    """Une expression de date → instant local. Lève DateIncomprise sinon.

    Comprend : « +2h », « +30min », « +3j », « ce soir », « demain »,
    « demain 14h », « lundi » (le prochain), « lundi 14:30 », une date
    `2026-09-27 14:00` ou `2026-09-27T14:00` (champ datetime-local), et
    `27/09 14h`. Sans heure précisée : 9 h.
    """
    now = (depuis or maintenant()).astimezone(TZ)
    brut = " ".join((texte or "").strip().lower().split())
    if not brut:
        raise DateIncomprise("date vide")

    relatif = re.fullmatch(r"(?:dans\s+)?\+?\s*(\d+)\s*(min|minutes?|h|heures?|j|jours?)", brut)
    if relatif:
        n, unite = int(relatif.group(1)), relatif.group(2)
        delta = (timedelta(minutes=n) if unite.startswith("min")
                 else timedelta(hours=n) if unite.startswith("h")
                 else timedelta(days=n))
        return (now + delta).replace(second=0, microsecond=0)

    heure = re.search(r"(?:à\s*)?(\d{1,2})\s*(?:h|:)\s*(\d{2})?$", brut)
    h, m = (int(heure.group(1)), int(heure.group(2) or 0)) if heure else (None, 0)
    if h is not None and not (0 <= h <= 23 and 0 <= m <= 59):
        raise DateIncomprise(f"heure invalide : {texte!r}")
    reste = brut[:heure.start()].strip() if heure else brut

    if reste in ("ce soir", "soir"):
        cible = _a(now, h if h is not None else HEURE_SOIR, m)
        return cible if cible > now else cible + timedelta(days=1)
    if reste in ("aujourd'hui", "aujourdhui", "tout à l'heure", ""):
        if h is None:
            raise DateIncomprise(f"heure manquante : {texte!r}")
        cible = _a(now, h, m)
        return cible if cible > now else cible + timedelta(days=1)
    if reste == "demain":
        return _a(now + timedelta(days=1), h if h is not None else HEURE_MATIN, m)
    if reste in ("après-demain", "apres-demain", "après demain"):
        return _a(now + timedelta(days=2), h if h is not None else HEURE_MATIN, m)
    jour = reste.removeprefix("le ").removesuffix(" prochain").strip()
    if jour in JOURS:
        ecart = (JOURS.index(jour) - now.weekday()) % 7 or 7
        return _a(now + timedelta(days=ecart), h if h is not None else HEURE_MATIN, m)

    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[ t](\d{1,2}):(\d{2}))?", brut)
    if iso:
        a, mo, j, hh, mm = iso.groups()
        return datetime(int(a), int(mo), int(j), int(hh or (h if h is not None else HEURE_MATIN)),
                        int(mm or m), tzinfo=TZ)
    court = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", reste)
    if court:
        j, mo, a = int(court.group(1)), int(court.group(2)), court.group(3)
        annee = int(a) + (2000 if a and len(a) == 2 else 0) if a else now.year
        cible = datetime(annee, mo, j, h if h is not None else HEURE_MATIN, m, tzinfo=TZ)
        if not a and cible < now:
            cible = cible.replace(year=annee + 1)
        return cible
    raise DateIncomprise(
        f"date incomprise : {texte!r} — essayer « demain », « ce soir », « lundi 14h », "
        "« +2h » ou « 2026-09-27 14:00 »")


def en_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def depuis_utc(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(TZ)


def libelle(stamp: str | None) -> str:
    """« aujourd'hui 18:00 », « demain 09:00 », « lun. 29/09 09:00 »."""
    moment = depuis_utc(stamp)
    if moment is None:
        return ""
    jours = (moment.date() - maintenant().date()).days
    heure = moment.strftime("%H:%M")
    if jours == 0:
        return f"aujourd'hui {heure}"
    if jours == 1:
        return f"demain {heure}"
    if jours == -1:
        return f"hier {heure}"
    return f"{JOURS_COURTS[moment.weekday()]} {moment.strftime('%d/%m')} {heure}"


def echu(stamp: str | None) -> bool:
    moment = depuis_utc(stamp)
    return bool(moment and moment <= maintenant())


GROUPES = [("echus", "Échus"), ("aujourdhui", "Aujourd'hui"), ("demain", "Demain"),
           ("semaine", "Cette semaine"), ("plus_tard", "Plus tard")]


def groupe(stamp: str | None) -> str:
    """Dans quelle colonne ranger un rappel, vu d'aujourd'hui."""
    moment = depuis_utc(stamp)
    now = maintenant()
    if moment is None or moment <= now:
        return "echus"
    jours = (moment.date() - now.date()).days
    if jours == 0:
        return "aujourdhui"
    if jours == 1:
        return "demain"
    return "semaine" if jours < 7 else "plus_tard"


def valeur_champ(stamp: str | None) -> str:
    """Pour pré-remplir un <input type=datetime-local>."""
    moment = depuis_utc(stamp)
    return moment.strftime("%Y-%m-%dT%H:%M") if moment else ""
