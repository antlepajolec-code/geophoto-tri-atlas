# GeoPhoto Tri & Atlas — plugin QGIS

Dépôt : https://github.com/antlepajolec-code/geophoto-tri-atlas
Auteur : Antoine Le Pajolec (ant.lepajolec@gmail.com)

Plugin QGIS (3.16+) qui enchaîne trois étapes :

1. **Importer** : lecture des coordonnées GPS EXIF des photos et création
   d'une couche de points sur la carte, avec le nom du fichier en attribut
   (visible dans la table attributaire, qui sert de table des matières).
2. **Trier** : jointure spatiale entre les points-photos et une couche de
   polygones (emprises). Les photos sont copiées ou déplacées dans des
   sous-dossiers nommés d'après l'entité polygonale qui les contient.
3. **Atlas** : génération d'une mise en page à 2 pages pilotée par l'Atlas
   (page 1 : carte de l'emprise ; page 2 : photos de l'emprise + table).

## Installation

1. Compresser le dossier `geophoto_tri_atlas` en ZIP (ou utiliser le ZIP
   fourni).
2. Dans QGIS : **Extensions ▸ Installer/Gérer les extensions ▸ Installer
   depuis un ZIP**, choisir le fichier, puis activer l'extension.
3. L'icône « GeoPhoto – Tri & Atlas » apparaît dans la barre d'outils et
   dans le menu Extensions.

Aucune dépendance externe : la lecture EXIF utilise `QgsExifTools`,
intégré à QGIS.

## Utilisation

### Étape 1 — Importer les photos
- Choisir le dossier contenant les photos (option récursive).
- La couche de points est créée en **EPSG:4326 (WGS84)**, le référentiel
  natif des coordonnées GPS EXIF. QGIS la reprojette à la volée vers le
  SCR du projet (par ex. Lambert-93 / EPSG:2154) : les points tombent donc
  au bon endroit quel que soit le référentiel du projet.
- Attributs créés : `photo` (nom du fichier), `chemin`, `dossier`,
  `date_prise`, `altitude`, `direction`, plus `emprise` et `chemin_tri`
  remplis à l'étape 2.
- Option : enregistrer la couche en GeoPackage pour la rendre pérenne
  (recommandé avant de construire l'Atlas).

### Étape 2 — Trier par emprises
- Choisir la couche de points, la couche de polygones (emprises), le champ
  servant à nommer les dossiers, et le **dossier d'atterrissage**.
- Deux modes :
  - **Automatique** : toutes les entités de la couche d'emprises ;
  - **Manuel** : uniquement les entités sélectionnées sur la carte.
- Opération **Copier** (originaux conservés) ou **Déplacer**.
- Options : dupliquer une photo présente dans plusieurs emprises qui se
  chevauchent ; **rattacher les photos hors emprise à l'emprise la plus
  proche**, avec une distance maximale en mètres (mesurée sur
  l'ellipsoïde, 0 = illimitée) — utile quand la précision du GPS fait
  tomber un point juste à l'extérieur du polygone ; par défaut, ces
  photos rattachées sont rangées dans un **sous-dossier créé
  automatiquement dans le dossier du bloc** (nom paramétrable,
  `_proximite` par défaut, ex. `Bloc_A/_proximite/`), ce qui les
  distingue des photos réellement contenues dans le bloc — décochez
  l'option pour les mélanger directement ; ranger les photos restées
  hors emprise dans un dossier dédié (`_hors_emprises` par
  défaut). La distance de rattachement est stockée dans le champ
  `dist_emprise` (0 si la photo est contenue dans son emprise), ce qui
  permet de contrôler la qualité des associations.
- Résultat : un sous-dossier par entité, correctement nommé, contenant
  ses photos ; les champs `emprise` et `chemin_tri` de la couche de
  points sont mis à jour. Les tests point-dans-polygone utilisent une
  transformation de coordonnées explicite entre les SCR des deux couches.

### Étape 3 — Mise en page Atlas
- Crée une mise en page A4 paysage à **2 pages par emprise** :
  - page 1 : titre + carte centrée sur l'entité courante (marge 15 %) ;
  - page 2 : jusqu'à 4 photos de l'emprise (sources définies par
    expression) + table listant toutes les photos (nom, date, chemin).
- L'Atlas est configuré sur la couche d'emprises, trié et nommé selon le
  champ choisi. À l'export (PDF/images), chaque emprise génère ses 2 pages
  avant de passer à l'emprise suivante.
- La mise en page reste standard : vous pouvez la personnaliser (échelle,
  légende, nombre de photos, colonnes de la table…). Pour afficher plus de
  photos, dupliquez un item Image et changez l'indice final de son
  expression `array_get(..., N)`.
- La carte de la page 1 importe la vue courante du canevas au moment de la
  création (fond de plan actuellement affiché + étendue) et fige les
  couches utilisées (couche d'emprises et couche de points incluses même
  si elles sont décochées) : la mise en page ne change plus si vous
  modifiez ensuite la visibilité des couches dans votre projet.
  L'étiquetage numéroté des points (`num_bloc`) n'est appliqué que dans
  cette carte de mise en page, sans modifier le style de la couche dans
  le projet.

## Notes et limites

- L'EXIF GPS est surtout présent dans les JPEG/TIFF ; les photos sans
  balise GPS sont listées dans le journal et ignorées.
- Les caractères invalides dans les noms d'entités sont remplacés par `_`
  pour créer des noms de dossiers valides (sous-dossiers de tri
  uniquement — voir ci-dessous).
- En cas de doublon dans un dossier cible : fichier identique (même nom,
  même taille) → ignoré ; sinon suffixe `_1`, `_2`…
- Le champ `emprise` de la couche de points stocke la valeur **brute** du
  champ « nom d'emprise » (pas la version assainie utilisée pour les
  dossiers), afin de correspondre exactement à la valeur que l'Atlas lit
  sur la couche de polygones ; il peut contenir plusieurs noms séparés
  par `;` si la photo se trouve dans des emprises qui se chevauchent, les
  expressions de l'Atlas en tiennent compte.

## Historique

### 1.2.0
- **Correctif** : une balise GPS EXIF présente mais VIDE (composants
  rationnels `0/0`, référence N/S/E/W absente — observé sur un Samsung
  Galaxy A26 5G quand la localisation n'est pas acquise à la prise de
  vue) était convertie en un point `(0°, 0°)` exact (« Null Island »,
  loin de toute emprise réelle) au lieu d'être traitée comme une
  absence de GPS. Les trois lecteurs EXIF (`QgsExifTools`, analyseur
  interne, Pillow) valident désormais la référence et rejettent les
  coordonnées NaN/Infini/`(0, 0)` exactes ; le bouton « Diagnostiquer
  un fichier photo… » explique maintenant ce cas précis.
- **Correctif** : le repli Pillow levait une `ZeroDivisionError` non
  interceptée sur ce même type de balise GPS vide.
- Un fichier dont la lecture EXIF échoue de façon inattendue n'arrête
  plus l'import du reste du dossier (il est simplement journalisé et
  compté comme « sans GPS »).

### 1.1.0
- **Correctif** : la page 2 de l'Atlas (photos, légendes, table) restait
  vide pour toute emprise dont le nom contenait un caractère modifié par
  l'assainissement des noms de dossiers (espace multiple, l'un de
  `\ / : * ? " < > |`, ou un nom de plus de 120 caractères) — l'attribut
  `emprise` de la couche de points stockait la version assainie, alors
  que le filtre de l'Atlas compare à la valeur brute de la couche
  d'emprises. Corrigé : `emprise` stocke maintenant la valeur brute.
- La carte de l'Atlas importe la vue courante du canevas (fond de plan +
  étendue) et fige les couches utilisées, au lieu de suivre
  dynamiquement la visibilité des couches du projet.
- L'étiquetage numéroté des points n'est appliqué que dans la mise en
  page Atlas, sans modifier le style de la couche dans le projet.
