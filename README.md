# Éditeur PDF local

Éditeur de PDF local : on dépose un PDF, on clique sur un texte pour le réécrire **avec la même police**,
on déplace / supprime des éléments, puis on télécharge le PDF modifié. Rien ne quitte le Mac.

## Lancer

Double-clique sur **`Lancer PDF Edit.command`** (la première fois, macOS peut demander une confirmation :
clic droit → Ouvrir). Le navigateur s'ouvre sur http://localhost:8765. Ferme la fenêtre du Terminal pour arrêter.

Ligne de commande équivalente :

```bash
.venv/bin/python -m uvicorn server.app:app --port 8765
```

## Utilisation

| Action | Comment |
|---|---|
| Modifier un texte | Clic dans le texte, puis taper. Le paragraphe se recompose (retour à la ligne automatique). |
| Mettre en forme | Sélectionner des mots puis police / taille / couleur / gras (⌘B) / italique (⌘I) dans la barre. Sans sélection, le style s'applique au bloc entier. |
| Déplacer un bloc | Poignée bleue ✥ en haut à gauche du bloc, ou flèches du clavier (⇧ = 10 pt). |
| Élargir un bloc | Poignées sur les côtés gauche / droit. |
| Supprimer | Sélectionner (clic puis Échap), puis Suppr ou le bouton « Supprimer ». |
| Images | Clic sur une image pour la déplacer / redimensionner ; outil « Image » pour en ajouter (logo, signature…). |
| Formes | Rectangle, ellipse, ligne, surligneur, rectangle blanc pour masquer. |
| Annuler / rétablir | ⌘Z / ⇧⌘Z |
| Télécharger | Bouton bleu ou ⌘S. |

Le travail en cours est gardé dans le navigateur : en rouvrant la page, « Reprendre » retrouve les modifications.

## Comment ça marche

- **Polices** (`server/fonts.py`) : pour chaque police du PDF, on cherche la police complète du même nom sur
  le Mac (y compris les polices Office), puis sur Google Fonts ; sinon on répare la police embarquée dans le PDF
  (souvent incomplète) et on complète les lettres manquantes avec une police aux mêmes proportions.
- **Analyse** (`server/extract.py`) : regroupe les lignes en paragraphes, détecte alignement, interligne,
  espacement des lettres et des mots, retours à la ligne voulus ou automatiques.
- **Pages scannées** (`server/ocr.py`) : reconnaissance du texte avec l'OCR intégré à macOS (Apple Vision),
  détection police avec/sans empattements, taille, gras, couleur du texte et du papier.
- **Export** (`server/export.py`) : le texte d'origine est réellement retiré du PDF (pas juste caché), puis
  chaque mot est réécrit à la position exacte affichée dans l'éditeur.
- **Métadonnées** : à chaque téléchargement, le PDF est réécrit entièrement comme un fichier neuf. Titre, auteur,
  logiciel créateur et producteur sont vidés, les métadonnées XMP et les données privées d'applications sont
  supprimées, les dates de création et de modification valent l'heure du téléchargement, et le PDF reçoit un
  nouvel identifiant, sans historique de révisions.

## Limites connues

- Comme dans Acrobat, un paragraphe qui s'allonge ne pousse pas le texte situé dessous : il faut le déplacer.
- Le texte transformé en dessin vectoriel (certains exports Canva / Illustrator) n'est pas du texte : il n'est
  pas modifiable.
- Sur un scan, la police est approchée (Arial ou Times, normal ou gras).

## Tests

```bash
.venv/bin/pip install playwright
.venv/bin/python tests/e2e.py tests/samples/chrome_devis.pdf /tmp/sortie basic
```

Scénarios : `basic`, `word`, `scan`, `styles`, `noop`, `rewrite_all` (réécrit tout le document et mesure
l'écart au pixel près avec l'original).
