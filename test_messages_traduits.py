"""Aucun message en français écrit en dur hors des tables de traduction.

Issue #121 : la page était traduite, mais une centaine de messages construits
par le serveur (« module occupé », « session Blink occupée »...) restaient en
français pour tout le monde, et justement quand quelque chose allait mal.
Ils passent tous par une table bilingue {"fr": ..., "en": ...} et msg() ;
ce test empêche qu'un nouveau s'écrive de nouveau en dur.

La détection est volontairement simple : une lettre accentuée, ou un petit
mot très français dans une phrase. Elle ne voit pas tout (« Fusion » passe),
mais elle attrape l'immense majorité des cas réels, qui sont des phrases.
"""
import ast
import re
import string
import unittest
from pathlib import Path

RACINE = Path(__file__).resolve().parent

# Outils de développement et de CI : leur sortie ne s'adresse qu'au mainteneur.
OUTILS = {
    "build.py", "build_blinkpy_win7.py", "build_support.py", "build_xr_tester.py",
    "deploy.py", "docs.py", "smoketest.py", "tests.py", "verify_win7_bundle.py",
    "webrtc_probe.py",
}

# Constantes déjà bilingues par construction, ou traduites ailleurs : la page
# web a son propre dictionnaire (I18N), et VERBES porte une paire fr/en par
# verbe (Verbe(groupe, fr, en)).
CONSTANTES_EXCLUES = {"PAGE", "VERBES"}

# Chaînes françaises légitimes : ce ne sont pas des messages.
EXCEPTIONS = {
    # Reconnaît un message d'erreur de blinkpy dans l'une ou l'autre langue.
    ("diagnostic_xr.py", "pas compatible"),
    ("diagnostic_xr.py", "pas activ"),
    ("diagnostic_xr.py", "pas actif"),
    # Contenu d'un fichier marqueur que personne ne lit, seule sa présence compte.
    ("maj.py", "Répertoire temporaire de mise à jour.\n"),
}

# Les guillemets « » ne s'écrivent qu'en français (l'anglais prend “ ”).
ACCENTS = re.compile(r"[éèêëàâùûüçôöîïœÉÈÊÀÂÇÔÎ«»]")
MOTS = re.compile(r"\b(le|la|les|de|des|du|une|est|pas|aucun|aucune|sans|avec|pour|dans|"
                  r"inconnu|inconnue|invalide|introuvable|absent|absente)\b",
                  re.IGNORECASE)


def _modules():
    return sorted(chemin for chemin in RACINE.glob("*.py")
                  if not chemin.name.startswith("test_") and chemin.name not in OUTILS)


def _tables_bilingues(arbre):
    """Dictionnaires littéraux dont les clés sont exactement "fr" et "en"."""
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.Dict):
            cles = {cle.value for cle in noeud.keys if isinstance(cle, ast.Constant)}
            if cles == {"fr", "en"} and len(noeud.keys) == 2:
                yield noeud


def _a_ignorer(arbre):
    ignores = set()
    for noeud in _tables_bilingues(arbre):
        ignores.update(id(sous) for sous in ast.walk(noeud))
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.Assign) and any(
                isinstance(cible, ast.Name) and cible.id in CONSTANTES_EXCLUES
                for cible in noeud.targets):
            ignores.update(id(sous) for sous in ast.walk(noeud))
        # Docstrings et chaînes isolées (commentaires en forme de chaîne).
        if isinstance(noeud, ast.Expr) and isinstance(noeud.value, ast.Constant):
            ignores.add(id(noeud.value))
    return ignores


def _francais(texte: str) -> bool:
    # Un caractère isolé (table de translittération des noms de caméra,
    # issue #55) n'est jamais un message.
    if len(texte.strip()) < 3:
        return False
    return bool(ACCENTS.search(texte)) or (" " in texte.strip() and bool(MOTS.search(texte)))


def _champs(modele: str) -> set:
    return {nom for _, nom, _, _ in string.Formatter().parse(modele) if nom}


class MessagesTraduits(unittest.TestCase):
    def test_aucun_message_francais_en_dur(self):
        trouves = []
        for chemin in _modules():
            arbre = ast.parse(chemin.read_text(encoding="utf-8"))
            ignores = _a_ignorer(arbre)
            for noeud in ast.walk(arbre):
                if (isinstance(noeud, ast.Constant) and isinstance(noeud.value, str)
                        and id(noeud) not in ignores and _francais(noeud.value)
                        and (chemin.name, noeud.value) not in EXCEPTIONS):
                    trouves.append(f"{chemin.name}:{noeud.lineno}: {noeud.value!r}")
        self.assertEqual(trouves, [], "\nMessages à passer par une table fr/en et msg() :\n"
                         + "\n".join(trouves))

    def test_tables_completes_dans_les_deux_langues(self):
        """Chaque clé existe en français et en anglais, avec les mêmes
        {valeurs} : une traduction qui en oublie une lèverait KeyError au
        moment précis où le message doit s'afficher."""
        ecarts = []
        for chemin in _modules():
            arbre = ast.parse(chemin.read_text(encoding="utf-8"))
            for table in _tables_bilingues(arbre):
                langues = {}
                for cle, valeur in zip(table.keys, table.values):
                    if not isinstance(valeur, ast.Dict):
                        continue
                    langues[cle.value] = {
                        k.value: v.value for k, v in zip(valeur.keys, valeur.values)
                        if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)
                        and isinstance(v.value, str)}
                if set(langues) != {"fr", "en"}:
                    continue
                fr, en = langues["fr"], langues["en"]
                for cle in sorted(set(fr) ^ set(en)):
                    ecarts.append(f"{chemin.name}:{table.lineno}: « {cle} » dans une seule langue")
                for cle in sorted(set(fr) & set(en)):
                    if _champs(fr[cle]) != _champs(en[cle]):
                        ecarts.append(f"{chemin.name}:{table.lineno}: « {cle} » "
                                      f"{sorted(_champs(fr[cle]))} != {sorted(_champs(en[cle]))}")
        self.assertEqual(ecarts, [], "\n" + "\n".join(ecarts))


if __name__ == "__main__":
    unittest.main()
