"""Rendu d'un markdown minimal, pour ce qu'écrivent les agents.

Le projet n'a aucune dépendance côté navigateur : pas de CDN, pas de
bibliothèque. Un agent structure pourtant ses réponses en markdown, et l'afficher
brut rend le journal illisible dès qu'il y a un tableau ou une liste.

**On échappe d'abord, on met en forme ensuite.** Le texte vient d'un agent, donc
d'un modèle, donc en pratique de n'importe où — un dépôt qu'il a lu, une page
qu'il a ouverte. Le traiter comme du HTML de confiance ouvrirait une injection
par le chemin le plus discret qui soit. Toutes les balises produites ici sont
posées **après** l'échappement, et forment une liste close.

Volontairement incomplet : titres, gras, italique, code, blocs de code, listes,
citations, tableaux, liens et traits. Le reste passe en texte, ce qui est le
mauvais cas acceptable — on affiche des astérisques, on ne casse rien.
"""
import hashlib
import html
import re

# Un lien n'est rendu que vers ces schémas. Sans ce filtre, `[clic](javascript:…)`
# deviendrait un lien exécutable posé par le texte d'un agent.
SCHEMAS = ("http://", "https://", "/", "#", "mailto:")


def _lien(m: re.Match) -> str:
    texte, cible = m.group(1), m.group(2).strip()
    if not cible.startswith(SCHEMAS):
        return texte
    externe = ' target="_blank" rel="noopener"' if cible.startswith("http") else ""
    return f'<a href="{cible}"{externe}>{texte}</a>'


def _en_ligne(texte: str) -> str:
    """Mise en forme à l'intérieur d'une ligne, sur du texte DÉJÀ échappé."""
    # Le code d'abord : ce qu'il contient ne doit plus être interprété ensuite.
    morceaux = re.split(r"(`[^`]+`)", texte)
    rendu = []
    for i, morceau in enumerate(morceaux):
        if i % 2:
            rendu.append(f"<code>{morceau[1:-1]}</code>")
            continue
        morceau = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", morceau)
        morceau = re.sub(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", morceau)
        morceau = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _lien, morceau)
        rendu.append(morceau)
    return "".join(rendu)


def en_ligne(texte: str) -> str:
    """Mise en forme d'un texte d'**une seule ligne**, sans paragraphe autour.

    Pour les résumés : un titre de journal n'a pas à se voir enveloppé d'un `<p>`
    qui lui donnerait des marges au milieu d'une ligne. Même échappement, mêmes
    balises closes que {@link rendu}.
    """
    return _en_ligne(html.escape(texte or ""))


def _tableau(lignes: list[str]) -> str:
    """Un tableau markdown, en-tête compris. Les lignes sont déjà échappées."""
    cellules = [[c.strip() for c in ligne.strip().strip("|").split("|")]
                for ligne in lignes]
    if len(cellules) < 2:
        return ""
    entete, corps = cellules[0], cellules[2:]   # [1] est la ligne de séparation
    out = ["<table><thead><tr>"]
    out += [f"<th>{_en_ligne(c)}</th>" for c in entete]
    out.append("</tr></thead><tbody>")
    for rangee in corps:
        out.append("<tr>" + "".join(f"<td>{_en_ligne(c)}</td>" for c in rangee) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


SEPARATEUR = re.compile(r"^\s*\|?[\s:|-]*\|[\s:|-]*$")

# Une case de checklist : `- [ ] à faire`, `- [x] fait`.
CASE = re.compile(r"^(\s*[-*+]\s+)\[([ xX])\](?=\s|$)")


def _lignes_de_cases(texte: str) -> list[int]:
    """Numéros des lignes portant une case, hors blocs de code.

    Le rendu et la bascule comptent les cases de la même façon : c'est ce qui
    permet de désigner la n-ième case par son rang.
    """
    rangs, bloc = [], False
    for i, ligne in enumerate(texte.splitlines()):
        if ligne.strip().startswith("```"):
            bloc = not bloc
        elif not bloc and CASE.match(ligne):
            rangs.append(i)
    return rangs


def compter_cases(texte: str) -> tuple[int, int]:
    """(cochées, total) des cases d'un texte."""
    lignes = (texte or "").splitlines()
    rangs = _lignes_de_cases(texte or "")
    faites = sum(1 for i in rangs if CASE.match(lignes[i]).group(2) != " ")
    return faites, len(rangs)


def empreinte(ligne: str) -> str:
    return hashlib.sha1(ligne.strip().encode()).hexdigest()[:10]


def basculer_case(texte: str, rang: int, attendue: str | None = None) -> str | None:
    """Coche ou décoche la case de rang `rang`. None si elle n'existe plus.

    `attendue` est l'empreinte de la ligne telle qu'affichée : si le texte a
    changé entre l'affichage et le clic, on refuse plutôt que de cocher la
    mauvaise ligne.
    """
    lignes = texte.splitlines()
    rangs = _lignes_de_cases(texte)
    if not 0 <= rang < len(rangs):
        return None
    i = rangs[rang]
    if attendue and empreinte(lignes[i]) != attendue:
        return None
    m = CASE.match(lignes[i])
    nouvelle = " " if m.group(2) != " " else "x"
    lignes[i] = f"{m.group(1)}[{nouvelle}]" + lignes[i][m.end():]
    return "\n".join(lignes) + ("\n" if texte.endswith("\n") else "")


def rendu(texte: str, cases: dict | None = None) -> str:
    """Markdown → HTML. L'entrée est du texte brut, jamais du HTML.

    `cases` rend les cases cliquables : {"action": url du POST, "champs":
    {nom: valeur} à renvoyer, "cible": id du bloc à réafficher (facultatif —
    la fiche en boîte de dialogue se recharge elle-même, pas le panneau)}. Ces valeurs viennent de l'application, pas du
    texte ; elles sont échappées quand même. Sans `cases`, ☐ / ☑ en lecture.
    """
    if not texte:
        return ""
    brutes = texte.splitlines()
    rangs = {i: n for n, i in enumerate(_lignes_de_cases(texte))}
    lignes = html.escape(texte).splitlines()
    out: list[str] = []
    liste: str | None = None      # 'ul' ou 'ol' quand une liste est ouverte
    bloc = False                  # dans un ``` … ```
    tampon: list[str] = []        # lignes d'un tableau en cours

    def ferme_liste() -> None:
        nonlocal liste
        if liste:
            out.append(f"</{liste}>")
            liste = None

    def ferme_tableau() -> None:
        """Vide le tampon : en tableau si c'en est un, en paragraphes sinon.

        Le « sinon » n'est pas un détail : une ligne à barres verticales sans
        ligne de séparation n'est pas un tableau, et la rendre par un tableau
        vide **effaçait le texte** au lieu de l'afficher.
        """
        if not tampon:
            return
        if len(tampon) >= 2 and SEPARATEUR.match(tampon[1]):
            out.append(_tableau(tampon))
        else:
            out.extend(f"<p>{_en_ligne(l)}</p>" for l in tampon if l.strip())
        tampon.clear()

    def case(i: int, contenu: str) -> str:
        coche = CASE.match(brutes[i]).group(2) != " "
        reste = _en_ligne(contenu[3:].lstrip())
        classe = "md-check done" if coche else "md-check"
        if not cases:
            return f'<li class="{classe}"><span class="md-box">{"☑" if coche else "☐"}</span> {reste}</li>'
        champs = dict(cases.get("champs") or {}, rang=rangs[i], empreinte=empreinte(brutes[i]))
        caches = "".join(f'<input type="hidden" name="{html.escape(str(k))}" value="{html.escape(str(v))}">'
                         for k, v in champs.items())
        titre = "Décocher" if coche else "Cocher"
        cible = (f' data-target="{html.escape(cases["cible"])}"' if cases.get("cible") else "")
        return (f'<li class="{classe}"><form class="panel-form inline" method="post"{cible}'
                f' action="{html.escape(cases["action"])}">{caches}'
                f'<button type="submit" class="md-box" title="{titre}" aria-label="{titre}">'
                f'{"☑" if coche else "☐"}</button></form> {reste}</li>')

    for i, ligne in enumerate(lignes):
        if ligne.strip().startswith("```"):
            ferme_liste(); ferme_tableau()
            out.append("</pre>" if bloc else "<pre class=\"md-code\">")
            bloc = not bloc
            continue
        if bloc:
            out.append(ligne)
            continue

        # Un tableau se reconnaît à sa deuxième ligne, faite de tirets. Sans
        # elle, une ligne à barres verticales n'est qu'une ligne de texte.
        if ligne.strip().startswith("|"):
            tampon.append(ligne)
            continue
        ferme_tableau()

        titre = re.match(r"^(#{1,6})\s+(.*)$", ligne)
        if titre:
            ferme_liste()
            niveau = min(len(titre.group(1)) + 2, 6)   # un h1 d'agent reste un h3
            out.append(f"<h{niveau}>{_en_ligne(titre.group(2))}</h{niveau}>")
            continue
        if re.match(r"^\s*(---|\*\*\*|___)\s*$", ligne):
            ferme_liste(); out.append("<hr>"); continue

        puce = re.match(r"^\s*[-*+]\s+(.*)$", ligne)
        numero = re.match(r"^\s*\d+[.)]\s+(.*)$", ligne)
        if puce or numero:
            voulue = "ul" if puce else "ol"
            if liste != voulue:
                ferme_liste()
                out.append(f"<{voulue}>")
                liste = voulue
            if i in rangs:
                out.append(case(i, puce.group(1)))
            else:
                out.append(f"<li>{_en_ligne((puce or numero).group(1))}</li>")
            continue
        ferme_liste()

        citation = re.match(r"^\s*&gt;\s?(.*)$", ligne)   # `>` déjà échappé
        if citation:
            out.append(f"<blockquote>{_en_ligne(citation.group(1))}</blockquote>")
            continue
        if ligne.strip():
            out.append(f"<p>{_en_ligne(ligne)}</p>")

    ferme_liste(); ferme_tableau()
    if bloc:
        out.append("</pre>")      # bloc de code jamais refermé par l'agent
    return "\n".join(out)
