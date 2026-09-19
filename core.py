# -*- coding: utf-8 -*-
"""
GeoPhoto Tri & Atlas — logique métier.

1. Lecture des balises EXIF GPS (via QgsExifTools, natif QGIS, aucune
   dépendance externe) et création d'une couche de points en EPSG:4326
   (WGS84 = référentiel natif des coordonnées GPS EXIF). QGIS reprojette
   ensuite à la volée vers le SCR du projet (ex. EPSG:2154).

2. Jointure spatiale point-dans-polygone avec transformation de
   coordonnées explicite entre le SCR des points et celui des emprises,
   puis copie/déplacement des photos dans des sous-dossiers nommés
   d'après le champ choisi de chaque entité polygonale.
"""

import math
import os
import re
import shutil

from qgis.PyQt.QtCore import QVariant, QDateTime
from qgis.core import (
    QgsVectorLayer, QgsField, QgsFeature, QgsGeometry, QgsPointXY,
    QgsProject, QgsCoordinateTransform, QgsSpatialIndex,
    QgsVectorFileWriter, QgsDistanceArea, QgsUnitTypes,
    QgsMessageLog, Qgis,
)

try:
    from qgis.core import QgsExifTools
    HAS_EXIF = True
except ImportError:          # très vieilles versions de QGIS
    HAS_EXIF = False

from .exif_fallback import read_gps_exif, describe_raw_gps

_LOG_TAG = 'GeoPhoto Tri & Atlas'


def _log_warn(message):
    """Journalise un avertissement (utilise dans les blocs defensifs ou
    une fonctionnalite optionnelle/dependante de la version de QGIS
    peut echouer sans devoir interrompre l'import/le tri)."""
    QgsMessageLog.logMessage(str(message), _LOG_TAG, level=Qgis.Warning)


# Extensions d'images gérées (l'EXIF GPS est surtout présent en JPEG/TIFF)
PHOTO_EXTS = ('.jpg', '.jpeg', '.jpe', '.tif', '.tiff', '.png', '.webp',
              '.heic', '.heif')

# Champs de la couche de points créée par le plugin
FIELD_DEFS = [
    ('photo',      QVariant.String),   # nom du fichier
    ('chemin',     QVariant.String),   # chemin absolu d'origine
    ('dossier',    QVariant.String),   # dossier d'origine
    ('date_prise', QVariant.String),   # date EXIF de prise de vue
    ('altitude',   QVariant.Double),
    ('direction',  QVariant.Double),   # azimut de prise de vue si présent
    ('emprise',    QVariant.String),   # rempli lors du tri (';' si multiple)
    ('chemin_tri', QVariant.String),   # chemin après rangement
]


# --------------------------------------------------------------------------
# Utilitaires
# --------------------------------------------------------------------------

def sanitize_name(value):
    """Transforme une valeur d'attribut en nom de dossier valide."""
    name = str(value).strip() if value is not None else ''
    name = re.sub(r'[\\/:*?"<>|]+', '_', name)
    name = re.sub(r'\s+', ' ', name).strip(' .')
    return name[:120] if name else '_sans_nom'


def list_photos(folder, recursive=True, exts=PHOTO_EXTS):
    """Liste les fichiers image d'un dossier (récursif ou non)."""
    photos = []
    if recursive:
        for root, _dirs, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(exts):
                    photos.append(os.path.join(root, f))
    else:
        for f in sorted(os.listdir(folder)):
            p = os.path.join(folder, f)
            if os.path.isfile(p) and f.lower().endswith(exts):
                photos.append(p)
    return sorted(photos)


def _read_tag(path, key):
    """Lit une balise EXIF si l'API le permet (QGIS >= 3.22)."""
    if not HAS_EXIF or not hasattr(QgsExifTools, 'readTag'):
        return None
    try:
        val = QgsExifTools.readTag(path, key)
    except Exception:
        return None
    if val is None:
        return None
    if isinstance(val, QDateTime):
        return val.toString('yyyy-MM-dd HH:mm:ss')
    return val


def _invalid_coord(x, y):
    """
    Détecte une coordonnée GPS non exploitable : NaN/Infini, ou un
    point (0°, 0°) exact (« Null Island », au large du Golfe de
    Guinée). Une vraie prise de vue de terrain ne tombe jamais
    précisément sur ce point : c'est la signature d'une balise GPS
    EXIF présente mais vide/placeholder (constaté sur un Samsung
    Galaxy A26 5G, qui écrit systématiquement la structure GPS avec
    des composants à 0/0 quand la localisation n'a pas pu être acquise
    au moment de la prise de vue). Partagé par les trois lecteurs EXIF
    (QgsExifTools, analyseur interne, Pillow) pour un comportement
    cohérent quel que soit celui qui traite le fichier.
    """
    try:
        if math.isnan(x) or math.isnan(y) or math.isinf(x) or math.isinf(y):
            return True
    except TypeError:
        return True
    return x == 0.0 and y == 0.0


def _read_geotag_qgis(path):
    """Lecture GPS via QgsExifTools (natif QGIS). Retourne dict ou None."""
    if not HAS_EXIF:
        return None
    try:
        point, ok = QgsExifTools.getGeoTag(path)
    except Exception:
        return None
    if not ok or point is None:
        return None
    try:
        if point.isEmpty():
            return None
    except Exception as exc:
        # isEmpty() indisponible sur cette version de QGIS : on
        # poursuit avec le point tel quel (validation plus loin).
        _log_warn(f"Verification isEmpty() du geotag QgsExifTools "
                  f"impossible : {exc}")

    x, y = point.x(), point.y()
    if _invalid_coord(x, y):
        return None

    info = {
        'x': x,
        'y': y,
        'z': point.z() if point.is3D() else None,
        'date': _read_tag(path, 'Exif.Photo.DateTimeOriginal'),
        'direction': None,
    }
    d = _read_tag(path, 'Exif.GPSInfo.GPSImgDirection')
    try:
        if d is not None:
            info['direction'] = float(d)
    except (TypeError, ValueError):
        pass
    return info


def _read_geotag_pil(path):
    """
    Lecture GPS de secours via Pillow, utilisée si QgsExifTools ne trouve
    rien (formats ou balises non gérés selon les versions de QGIS).
    Retourne dict ou None.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(path)
        exif = img.getexif()
        gps = exif.get_ifd(0x8825)          # IFD GPSInfo
    except Exception:
        return None
    if not gps:
        return None

    def _dms(value, ref, valid_refs, negative_refs):
        try:
            deg = float(value[0]) + float(value[1]) / 60.0 \
                + float(value[2]) / 3600.0
        except (TypeError, ValueError, IndexError, ZeroDivisionError):
            # ZeroDivisionError : Pillow représente un rationnel EXIF
            # 0/0 (balise GPS placeholder, dénominateur nul) comme un
            # IFDRational qui lève cette exception lors de sa
            # conversion en float, plutôt que de renvoyer NaN.
            return None
        if math.isnan(deg) or math.isinf(deg):
            return None
        ref_s = str(ref).strip('\x00 \t\r\n').upper()
        if ref_s not in valid_refs:
            # Référence absente/illisible (ex. octet nul écrit par
            # certains téléphones dans une balise GPS placeholder) :
            # balise non exploitable, plutôt que de supposer un
            # hémisphère par défaut.
            return None
        return -deg if ref_s in negative_refs else deg

    lat = _dms(gps.get(2), gps.get(1, 'N'), ('N', 'S'), ('S',))
    lon = _dms(gps.get(4), gps.get(3, 'E'), ('E', 'W'), ('W',))
    if lat is None or lon is None or _invalid_coord(lon, lat):
        return None

    alt = None
    try:
        if gps.get(6) is not None:
            alt = float(gps.get(6))
            if int(gps.get(5) or 0) == 1:   # réf. 1 = sous le niveau mer
                alt = -alt
    except (TypeError, ValueError):
        pass

    date = None
    try:
        ifd = exif.get_ifd(0x8769)          # IFD Exif
        d = ifd.get(36867)                  # DateTimeOriginal
        if d:
            d = str(d)
            date = d.replace(':', '-', 2) if len(d) >= 10 else d
    except Exception as exc:
        # Date de prise de vue absente ou balise EXIF illisible : ce
        # n'est pas bloquant, la photo est importee sans date.
        _log_warn(f"Date de prise de vue (EXIF) illisible pour une "
                  f"photo : {exc}")

    return {'x': lon, 'y': lat, 'z': alt, 'date': date, 'direction': None}


def read_geotag(path):
    """
    Retourne un dict {x, y, z, date, direction} ou None si pas de GPS.
    Chaîne de lecture : QgsExifTools (natif QGIS), puis analyseur EXIF
    interne en pur Python (aucune dépendance, gère les longitudes Ouest
    et les composantes à 0° près du méridien de Greenwich), puis Pillow.
    """
    info = _read_geotag_qgis(path)
    if info is None:
        info = read_gps_exif(path)
    if info is None:
        info = _read_geotag_pil(path)
    return info


def diagnose_photo(path):
    """
    Rapport texte détaillant ce que chaque lecteur EXIF trouve dans un
    fichier : permet d'identifier si un problème vient de la photo
    (balises GPS absentes, ex. fichier passé par une messagerie) ou de
    l'outil de lecture.
    """
    lines = [f'=== Diagnostic : {os.path.basename(path)} ===']
    if not os.path.isfile(path):
        lines.append('Fichier introuvable.')
        return '\n'.join(lines)
    lines.append(f'Taille : {os.path.getsize(path) / 1024:.0f} Ko — '
                 f'chemin : {path}')

    if HAS_EXIF:
        try:
            point, ok = QgsExifTools.getGeoTag(path)
            if ok and point is not None and not point.isEmpty() \
                    and not _invalid_coord(point.x(), point.y()):
                alt = f', alt {point.z():.1f} m' if point.is3D() else ''
                lines.append(f'· QgsExifTools : GPS trouvé -> '
                             f'lat {point.y():.7f}, lon {point.x():.7f}'
                             f'{alt} (WGS84).')
            elif ok and point is not None and not point.isEmpty():
                lines.append(f'· QgsExifTools : balise lue mais valeur '
                             f'invalide -> lat {point.y()!r}, '
                             f'lon {point.x()!r} — rejetée (voir « analyse '
                             f'brute » ci-dessous), traitée comme « sans '
                             f'GPS ».')
            else:
                lines.append('· QgsExifTools : AUCUNE balise GPS détectée.')
        except Exception as e:
            lines.append(f'· QgsExifTools : erreur de lecture ({e}).')
    else:
        lines.append('· QgsExifTools : indisponible (QGIS trop ancien).')

    interne = read_gps_exif(path)
    if interne is not None:
        alt = f", alt {interne['z']:.1f} m" if interne['z'] is not None else ''
        date = f" — date : {interne['date']}" if interne['date'] else ''
        lines.append(f"· Analyseur interne : GPS trouvé -> "
                     f"lat {interne['y']:.7f}, lon {interne['x']:.7f}"
                     f"{alt} (WGS84){date}.")
        if interne['x'] < 0:
            lines.append('  (longitude OUEST : réf. EXIF « W », valeur '
                         'négative — certains outils ignorent cette '
                         'référence et placent le point à l\'Est par '
                         'erreur.)')
    else:
        lines.append('· Analyseur interne : aucune balise GPS lisible ou '
                     'balise rejetée comme invalide.')
        raw_desc = describe_raw_gps(path)
        if raw_desc:
            lines.append(f'  · analyse brute de la balise : {raw_desc}')

    pil = _read_geotag_pil(path)
    if pil is not None:
        alt = f", alt {pil['z']:.1f} m" if pil['z'] is not None else ''
        date = f" — date : {pil['date']}" if pil['date'] else ''
        lines.append(f"· Pillow : GPS trouvé -> lat {pil['y']:.7f}, "
                     f"lon {pil['x']:.7f}{alt} (WGS84){date}.")
    else:
        try:
            import PIL  # noqa: F401
            lines.append('· Pillow : aucune balise GPS lisible.')
        except ImportError:
            lines.append('· Pillow : non installé dans ce QGIS.')

    if read_geotag(path) is None:
        lines.append('Conclusion : ce fichier ne contient pas de '
                     'coordonnées GPS exploitables. Causes fréquentes : '
                     'localisation désactivée sur le téléphone au moment '
                     'de la prise de vue (certains appareils écrivent '
                     'alors une balise GPS VIDE plutôt que de l\'omettre — '
                     'voir « analyse brute » ci-dessus si c\'est le cas), '
                     'ou photo transférée via une messagerie/un mail qui a '
                     'supprimé les métadonnées (WhatsApp, Messenger…). '
                     'Utilisez le fichier d\'origine du téléphone '
                     '(transfert par câble USB, carte SD ou service cloud '
                     'sans recompression) ; si la balise est vide, seule '
                     'une activation de la localisation avant la prise de '
                     'vue permettra d\'obtenir des coordonnées, quelle que '
                     'soit la méthode de transfert.')
    else:
        lines.append('Conclusion : coordonnées GPS présentes, ce fichier '
                     'sera importé normalement par l\'étape 1.')
    return '\n'.join(lines)


# --------------------------------------------------------------------------
# 1) Import des photos -> couche de points
# --------------------------------------------------------------------------

def create_photo_layer(name='Photos géolocalisées'):
    """Crée une couche mémoire de points en EPSG:4326 (WGS84)."""
    layer = QgsVectorLayer('Point?crs=EPSG:4326', name, 'memory')
    layer.dataProvider().addAttributes(
        [QgsField(n, t) for n, t in FIELD_DEFS])
    layer.updateFields()
    return layer


def import_photos(folder, recursive=True, layer_name='Photos géolocalisées',
                  progress_cb=None, log_cb=None):
    """
    Parcourt un dossier, extrait les coordonnées GPS EXIF et crée la
    couche de points. Retourne (layer, stats).
    """
    def log(msg):
        if log_cb:
            log_cb(msg)

    if not HAS_EXIF:
        try:
            import PIL  # noqa: F401
            log("QgsExifTools indisponible : lecture EXIF via Pillow.")
        except ImportError:
            raise RuntimeError(
                "Aucun lecteur EXIF disponible : cette version de QGIS ne "
                "fournit pas QgsExifTools (3.6+) et Pillow n'est pas "
                "installé.")

    photos = list_photos(folder, recursive)
    layer = create_photo_layer(layer_name)
    provider = layer.dataProvider()
    fields = layer.fields()

    feats, no_gps = [], []
    total = len(photos)
    for i, path in enumerate(photos):
        if progress_cb:
            progress_cb(i + 1, total)
        try:
            tag = read_geotag(path)
        except Exception as e:
            # Un fichier imprévu (EXIF corrompu, format inattendu…) ne
            # doit jamais interrompre l'import de tous les autres.
            no_gps.append(path)
            log(f"  · erreur de lecture EXIF, fichier ignoré : "
                f"{os.path.basename(path)} ({e})")
            continue
        if tag is None:
            no_gps.append(path)
            continue
        f = QgsFeature(fields)
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(tag['x'], tag['y'])))
        f['photo'] = os.path.basename(path)
        f['chemin'] = path
        f['dossier'] = os.path.dirname(path)
        f['date_prise'] = tag['date']
        f['altitude'] = tag['z']
        f['direction'] = tag['direction']
        feats.append(f)

    provider.addFeatures(feats)
    layer.updateExtents()

    log(f"{total} fichier(s) image analysé(s), "
        f"{len(feats)} géoréférencé(s), {len(no_gps)} sans GPS.")
    for p in no_gps[:20]:
        log(f"  · sans GPS : {os.path.basename(p)}")
    if len(no_gps) > 20:
        log(f"  · … et {len(no_gps) - 20} autres.")

    return layer, {'total': total, 'ok': len(feats), 'no_gps': len(no_gps)}


def save_as_gpkg(layer, gpkg_path):
    """Sauvegarde la couche mémoire en GeoPackage et retourne la couche OGR."""
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = 'GPKG'
    options.layerName = layer.name()
    ctx = QgsProject.instance().transformContext()
    if hasattr(QgsVectorFileWriter, 'writeAsVectorFormatV3'):
        res = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, gpkg_path, ctx, options)
    else:
        res = QgsVectorFileWriter.writeAsVectorFormatV2(
            layer, gpkg_path, ctx, options)
    if res[0] != QgsVectorFileWriter.NoError:
        raise RuntimeError(f"Échec d'écriture GeoPackage : {res[1]}")
    uri = f"{gpkg_path}|layername={layer.name()}"
    out = QgsVectorLayer(uri, layer.name(), 'ogr')
    if not out.isValid():
        raise RuntimeError(
            "GeoPackage écrit mais couche invalide au rechargement.")
    return out


# --------------------------------------------------------------------------
# 2) Tri des photos par emprises polygonales
# --------------------------------------------------------------------------

def _ensure_sort_fields(layer, log):
    """Ajoute les champs 'emprise', 'chemin_tri' et 'dist_emprise' si
    absents."""
    wanted = [('emprise', QVariant.String),
              ('chemin_tri', QVariant.String),
              ('dist_emprise', QVariant.Double),
              ('num_bloc', QVariant.Int)]
    missing = [QgsField(n, t) for n, t in wanted
               if layer.fields().indexOf(n) == -1]
    if not missing:
        return True
    ok = layer.dataProvider().addAttributes(missing)
    layer.updateFields()
    if not ok:
        log("Avertissement : impossible d'ajouter les champs 'emprise' / "
            "'chemin_tri' à la couche de points (fournisseur non éditable). "
            "Le tri des fichiers sera fait, sans mise à jour des attributs.")
    return ok


def _nearest_polygon(index, geoms, pt_geom, da):
    """
    Retourne (fid, distance_en_mètres) de l'emprise la plus proche du
    point, ou (None, None). Le classement se fait sur la distance
    cartésienne dans le SCR des emprises ; la distance retournée est
    mesurée sur l'ellipsoïde (mètres) quand c'est possible.
    """
    try:
        pt = pt_geom.asPoint()
    except Exception:
        return None, None
    candidates = index.nearestNeighbor(QgsPointXY(pt), min(len(geoms), 10))
    best_fid, best_cart = None, None
    for fid in candidates:
        d = geoms[fid].distance(pt_geom)
        if best_cart is None or d < best_cart:
            best_fid, best_cart = fid, d
    if best_fid is None:
        return None, None
    meters = best_cart
    try:
        near = geoms[best_fid].nearestPoint(pt_geom).asPoint()
        meters = da.measureLine(QgsPointXY(pt), QgsPointXY(near))
        meters = da.convertLengthMeasurement(
            meters, QgsUnitTypes.DistanceMeters)
    except Exception as exc:
        # Mesure ellipsoidale impossible : on garde la distance
        # cartesienne calculee plus haut (valeur approximative).
        _log_warn(f"Mesure de distance ellipsoidale a l'emprise la "
                  f"plus proche impossible, distance approximative "
                  f"utilisee : {exc}")
    return best_fid, meters


def _unique_dest(dest_dir, src_path, filename=None):
    """
    Chemin de destination sans collision. Si un fichier identique
    (même nom, même taille) existe déjà, il est réutilisé (skip).
    'filename' permet d'imposer un nom cible (ex. préfixé '001_…').
    Retourne (chemin, action) avec action in {'copy', 'skip'}.
    """
    filename = filename or os.path.basename(src_path)
    cand = os.path.join(dest_dir, filename)
    if os.path.exists(cand):
        try:
            if os.path.getsize(cand) == os.path.getsize(src_path):
                return cand, 'skip'
        except OSError:
            pass
        base, ext = os.path.splitext(filename)
        i = 1
        while os.path.exists(cand):
            cand = os.path.join(dest_dir, f'{base}_{i}{ext}')
            i += 1
    return cand, 'copy'


def sort_photos(point_layer, polygon_layer, name_field, dest_root,
                move=False, only_selected=False, duplicate_multi=True,
                handle_unmatched=True, unmatched_name='_hors_emprises',
                assign_nearest=False, max_distance_m=0.0,
                nearest_subfolder=None, number_files=True,
                progress_cb=None, log_cb=None):
    """
    Jointure spatiale points/polygones, numérotation des photos par bloc,
    puis rangement des fichiers.

    Numérotation : dans chaque bloc, les photos sont ordonnées par date
    de prise de vue EXIF ('date_prise', l'identifiant naturel
    jour/mois/année + heure/minutes/secondes) puis par nom de fichier,
    et reçoivent un numéro séquentiel 1, 2, 3… stocké dans le champ
    'num_bloc'. Ce numéro sert à étiqueter les points sur la carte et à
    afficher « n° X » dans l'Atlas.

    - only_selected=True  : mode manuel, seules les entités sélectionnées
                            de la couche d'emprises sont traitées.
    - only_selected=False : mode automatique, toutes les entités.
    - duplicate_multi     : si une photo tombe dans plusieurs emprises
                            (chevauchement), copie dans chacune (elle est
                            numérotée dans chaque bloc ; 'num_bloc' stocke
                            le numéro dans son premier bloc).
    - move                : déplace au lieu de copier (si la photo est dans
                            plusieurs emprises, elle est copiée partout puis
                            l'original est supprimé).
    - assign_nearest      : une photo hors de toute emprise est rattachée
                            à l'emprise la plus proche.
    - max_distance_m      : distance maximale de rattachement en mètres
                            (0 = illimitée) ; au-delà, la photo est traitée
                            comme « hors emprise ».
    - nearest_subfolder   : si renseigné, les photos rattachées par
                            proximité sont rangées dans ce sous-dossier au
                            sein du dossier du bloc (ex. bloc/_proximite/) ;
                            si None, elles vont directement dans le dossier
                            du bloc avec les autres.
    - number_files        : préfixe les fichiers copiés par leur numéro
                            dans le bloc ('001_IMG… .jpg'), pour que le
                            classement des dossiers suive celui de l'Atlas.
    La distance de rattachement est stockée dans le champ 'dist_emprise'
    (0 pour une photo contenue dans son emprise).
    Retourne un dict de statistiques.
    """
    def log(msg):
        if log_cb:
            log_cb(msg)

    field_idx = polygon_layer.fields().indexOf(name_field)
    if field_idx == -1:
        raise ValueError(f"Champ '{name_field}' introuvable dans la couche "
                         f"'{polygon_layer.name()}'.")

    # --- Entités polygonales à traiter (sélection ou toutes) --------------
    if only_selected:
        polys = list(polygon_layer.selectedFeatures())
        if not polys:
            raise ValueError("Aucune entité sélectionnée dans la couche "
                             "d'emprises (mode manuel).")
    else:
        polys = list(polygon_layer.getFeatures())
    if not polys:
        raise ValueError("La couche d'emprises ne contient aucune entité.")

    # --- Transformation de coordonnées points -> SCR des polygones --------
    # Garantit un test point-dans-polygone dans le même référentiel,
    # quel que soit le SCR de chaque couche (ex. 4326 -> 2154).
    transform = None
    if point_layer.crs() != polygon_layer.crs():
        transform = QgsCoordinateTransform(
            point_layer.crs(), polygon_layer.crs(), QgsProject.instance())

    # --- Index spatial sur les emprises retenues ---------------------------
    index = QgsSpatialIndex()
    # 'names'     : nom de dossier ASSAINI (sanitize_name), utilisé
    #               uniquement pour créer les sous-dossiers sur le disque.
    # 'raw_names' : valeur BRUTE (non assainie) du champ d'emprise,
    #               utilisée pour remplir l'attribut 'emprise' de la
    #               couche de points.
    # L'Atlas (atlas.py, _match_filter) compare ce champ 'emprise' à
    # attribute(@atlas_feature, name_field), qui renvoie la valeur BRUTE
    # de l'entité polygonale. Y stocker la version assainie créait un
    # décalage silencieux dès que le nom d'emprise contenait un
    # caractère modifié par sanitize_name (espace multiple, l'un de
    # \ / : * ? " < > |, ou une valeur tronquée au-delà de 120
    # caractères) : la page 2 de l'Atlas (photos, légendes, table)
    # restait alors vide pour ces emprises-là, bien que le tri sur le
    # disque ait correctement fonctionné.
    geoms, names, raw_names = {}, {}, {}
    for f in polys:
        g = f.geometry()
        if g is None or g.isEmpty():
            continue
        index.addFeature(f)
        geoms[f.id()] = g
        raw_value = f[field_idx]
        names[f.id()] = sanitize_name(raw_value)
        raw_names[f.id()] = '' if raw_value is None else str(raw_value)

    os.makedirs(dest_root, exist_ok=True)

    # Mesure des distances en mètres sur l'ellipsoïde (rattachement
    # au plus proche), quel que soit le SCR de la couche d'emprises.
    da = QgsDistanceArea()
    try:
        da.setSourceCrs(polygon_layer.crs(),
                        QgsProject.instance().transformContext())
        ell = QgsProject.instance().ellipsoid()
        da.setEllipsoid(ell if ell else 'WGS84')
    except Exception as exc:
        # Mesure ellipsoidale non configuree : les distances de
        # rattachement seront approximatives (SCR cartesien).
        _log_warn(f"Configuration de la mesure ellipsoidale (rattachement "
                  f"des photos hors emprise) impossible : {exc}")

    can_update = _ensure_sort_fields(point_layer, log)
    idx_emprise = point_layer.fields().indexOf('emprise')
    idx_chemin_tri = point_layer.fields().indexOf('chemin_tri')
    idx_dist = point_layer.fields().indexOf('dist_emprise')
    idx_num = point_layer.fields().indexOf('num_bloc')
    idx_chemin = point_layer.fields().indexOf('chemin')
    idx_photo = point_layer.fields().indexOf('photo')
    idx_date = point_layer.fields().indexOf('date_prise')

    stats = {'points': 0, 'classes': 0, 'rattachees': 0, 'hors_emprise': 0,
             'copies': 0, 'deplaces': 0, 'ignores': 0, 'introuvables': 0}
    attr_changes = {}

    points = list(point_layer.getFeatures())
    total = len(points)

    # ======================================================================
    # PASSE 1 — association spatiale de chaque photo à ses emprises
    # ======================================================================
    assignments = []   # dicts {feat, src, matches, nearest_m}

    for i, feat in enumerate(points):
        if progress_cb:
            progress_cb(i + 1, total)
        stats['points'] += 1

        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        try:
            pt = geom.asPoint()
        except Exception:
            pt = geom.centroid().asPoint()
        if transform:
            try:
                pt = transform.transform(pt)
            except Exception as e:
                ident = feat[idx_photo] if idx_photo != -1 else feat.id()
                log(f"  · reprojection impossible pour {ident} : {e}")
                continue
        pt_geom = QgsGeometry.fromPointXY(QgsPointXY(pt))

        # Candidats via l'index spatial puis test géométrique exact
        matches = []
        for fid in index.intersects(pt_geom.boundingBox()):
            if geoms[fid].intersects(pt_geom):
                matches.append(fid)

        # Chemin source de la photo
        src = feat[idx_chemin] if idx_chemin != -1 else None
        if not src or not os.path.isfile(str(src)):
            stats['introuvables'] += 1
            log(f"  · fichier introuvable : {src}")
            continue
        src = str(src)

        nearest_m = None
        if not matches:
            # Option : rattachement à l'emprise la plus proche
            if assign_nearest and geoms:
                fid, dist_m = _nearest_polygon(index, geoms, pt_geom, da)
                if fid is not None and (max_distance_m <= 0
                                        or dist_m <= max_distance_m):
                    matches = [fid]
                    nearest_m = dist_m
                    stats['rattachees'] += 1
                    log(f"  · {os.path.basename(src)} rattachée à "
                        f"« {names[fid]} » ({dist_m:.1f} m).")

        if not matches:
            stats['hors_emprise'] += 1
            if handle_unmatched:
                ddir = os.path.join(dest_root, sanitize_name(unmatched_name))
                os.makedirs(ddir, exist_ok=True)
                dest, action = _unique_dest(ddir, src)
                if action == 'copy':
                    shutil.copy2(src, dest)
                    stats['copies'] += 1
                else:
                    stats['ignores'] += 1
                if can_update:
                    attr_changes[feat.id()] = {idx_emprise: '',
                                               idx_chemin_tri: dest}
            continue

        if not duplicate_multi and len(matches) > 1:
            matches = matches[:1]

        assignments.append({'feat': feat, 'src': src,
                            'matches': matches, 'nearest_m': nearest_m})

    # ======================================================================
    # NUMÉROTATION — n° séquentiel 1, 2, 3… par bloc, dans l'ordre de la
    # date de prise de vue EXIF (jour/mois/année + heure/min/sec) puis du
    # nom de fichier. C'est cet ordre « de détection dans le bloc » qui
    # est affiché sur la carte et dans l'Atlas.
    # ======================================================================
    def _sort_key(entry):
        feat = entry['feat']
        d = feat[idx_date] if idx_date != -1 else None
        d = str(d) if d not in (None, '') else '9999-99-99 99:99:99'
        name = feat[idx_photo] if idx_photo != -1 else ''
        return (d, str(name))

    per_bloc = {}
    for entry in assignments:
        for fid in entry['matches']:
            per_bloc.setdefault(fid, []).append(entry)

    numbers = {}   # (feature_id, fid_bloc) -> numéro dans ce bloc
    for fid, entries in per_bloc.items():
        for n, entry in enumerate(sorted(entries, key=_sort_key), start=1):
            numbers[(entry['feat'].id(), fid)] = n

    # ======================================================================
    # PASSE 2 — copie/déplacement des fichiers et mise à jour des attributs
    # ======================================================================
    total2 = len(assignments)
    for i, entry in enumerate(assignments):
        if progress_cb:
            progress_cb(i + 1, max(total2, 1))
        feat, src = entry['feat'], entry['src']
        matches, nearest_m = entry['matches'], entry['nearest_m']

        dest_paths, matched_names = [], []
        for fid in matches:
            ename = names[fid]                    # dossier assaini
            # valeur brute -> attribut 'emprise'
            matched_names.append(raw_names[fid])
            ddir = os.path.join(dest_root, ename)
            # Photo rattachée par proximité -> sous-dossier dédié du bloc
            if nearest_m is not None and nearest_subfolder:
                ddir = os.path.join(ddir, sanitize_name(nearest_subfolder))
            os.makedirs(ddir, exist_ok=True)

            num = numbers[(feat.id(), fid)]
            filename = os.path.basename(src)
            if number_files:
                filename = f'{num:03d}_{filename}'
            dest, action = _unique_dest(ddir, src, filename)
            if action == 'copy':
                shutil.copy2(src, dest)
                stats['copies'] += 1
            else:
                stats['ignores'] += 1
            dest_paths.append(dest)

        if move:
            try:
                os.remove(src)
                stats['deplaces'] += 1
            except OSError as e:
                log(f"  · suppression impossible de {src} : {e}")

        stats['classes'] += 1
        if can_update:
            vals = {
                idx_emprise: ';'.join(matched_names),
                idx_chemin_tri: dest_paths[0],
            }
            if idx_dist != -1:
                vals[idx_dist] = round(nearest_m, 1) if nearest_m else 0.0
            if idx_num != -1:
                # numéro dans le premier bloc (bloc principal de la photo)
                vals[idx_num] = numbers[(feat.id(), matches[0])]
            attr_changes[feat.id()] = vals

    # --- Mise à jour des attributs de la couche de points ------------------
    if can_update and attr_changes:
        if not point_layer.dataProvider().changeAttributeValues(attr_changes):
            log("Avertissement : la mise à jour des attributs a échoué.")
        point_layer.triggerRepaint()

    log(f"Tri terminé : {stats['classes']} photo(s) associée(s) à une "
        f"emprise (dont {stats['rattachees']} rattachée(s) à la plus "
        f"proche), {stats['hors_emprise']} hors emprise, "
        f"{stats['copies']} copie(s), {stats['ignores']} déjà présente(s), "
        f"{stats['introuvables']} fichier(s) introuvable(s).")
    if move:
        log(f"{stats['deplaces']} original(aux) supprimé(s) "
            f"(mode déplacement).")

    return stats
