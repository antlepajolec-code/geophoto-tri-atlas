# -*- coding: utf-8 -*-
"""
GeoPhoto Tri & Atlas — corrélation temporelle photo <-> trace GPX.

Beaucoup d'écologues de terrain photographient avec un appareil SANS GPS
(reflex, compact) tout en portant un traceur GPS de randonnée (Garmin,
etc.) qui enregistre une trace GPX horodatée. Ce module géolocalise ces
photos a posteriori en interpolant la position sur la trace au moment
de la prise de vue (date EXIF), avec une correction de décalage
d'horloge : l'horloge de l'appareil photo n'est en général pas
synchronisée avec l'heure GPS/UTC réelle (fuseau horaire + dérive),
exactement comme dans GeoSetter/digiKam.

Aucune dépendance externe : la trace GPX (XML) est lue avec
xml.etree.ElementTree (bibliothèque standard), dans l'esprit du reste
du plugin (lecture EXIF native/pure-Python, sans bibliothèque tierce
obligatoire).
"""

import bisect
import datetime
import os
import re
# Analyse XML de la trace GPX : voir _reject_xxe() ci-dessous, qui
# écarte toute déclaration DOCTYPE/ENTITY (protection XXE) avant
# d'utiliser ce module, en l'absence de dépendance externe (defusedxml)
# dans ce plugin.
import xml.etree.ElementTree as ET  # nosec B405

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY

from .core import _read_datetime, list_photos, create_photo_layer

# 'AAAA-MM-JJTHH:MM:SS[.fraction]Z' ou '...+HH:MM' / '...-HHMM' (GPX 1.0/1.1)
_ISO_RE = re.compile(
    r'^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?'
    r'(Z|[+-]\d{2}:?\d{2})?$')


def _local(tag):
    """Nom de balise XML sans l'espace de noms (ex. '{...}trkpt' ->
    'trkpt')."""
    return tag.rsplit('}', 1)[-1]


def _parse_gpx_time(text):
    """
    Convertit un horodatage GPX ISO 8601 (ex. '2026-05-12T08:00:00Z',
    '...+02:00', ou avec fraction de seconde '...,50Z'/'...500Z') en
    secondes UTC (epoch), ou None si illisible. Implémentation par
    expression régulière (plutôt que datetime.fromisoformat) pour rester
    robuste face aux variations de format des traceurs GPS et aux
    versions de Python embarquées par QGIS.
    """
    if not text:
        return None
    text = text.strip()
    m = _ISO_RE.match(text)
    if not m:
        return None
    year, month, day, hour, minute, second, tz = m.groups()
    try:
        dt = datetime.datetime(int(year), int(month), int(day),
                               int(hour), int(minute), int(second))
    except ValueError:
        return None
    offset = datetime.timedelta(0)
    if tz and tz != 'Z':
        sign = 1 if tz[0] == '+' else -1
        tz_body = tz[1:].replace(':', '')
        try:
            tz_h, tz_m = int(tz_body[:2]), int(tz_body[2:4])
        except (ValueError, IndexError):
            return None
        offset = sign * datetime.timedelta(hours=tz_h, minutes=tz_m)
    dt = (dt - offset).replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def _parse_exif_time(text):
    """
    Convertit une date EXIF normalisée par le plugin ('AAAA-MM-JJ
    HH:MM:SS', voir core.py/exif_fallback.py) en secondes « epoch »,
    SANS tenir compte d'un fuseau horaire : l'horloge de l'appareil
    photo n'a pas de fuseau/UTC fiable, c'est justement ce que le
    décalage d'horloge (time_offset_seconds) corrige. Retourne None si
    illisible.
    """
    if not text:
        return None
    text = text.strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            dt = datetime.datetime.strptime(text[:19], fmt)
            return dt.replace(tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _reject_xxe(gpx_path):
    """
    Refuse tout fichier GPX contenant une déclaration <!DOCTYPE ...>
    ou <!ENTITY ...>, utilisées par les attaques XXE (une entité
    externe peut faire lire un fichier local par le parseur XML, ex.
    SYSTEM "file:///etc/passwd") ou « billion laughs » (explosion par
    entités imbriquées). Une trace GPX normale, produite par un
    traceur GPS, n'en contient jamais : cette vérification légère
    (lecture des premiers octets, avant toute analyse XML) sert
    d'équivalent minimal à defusedxml pour ce cas d'usage précis, sans
    ajouter de dépendance externe au plugin — le module natif
    xml.etree.ElementTree de QGIS/Python n'expose plus, dans ses
    versions récentes accélérées en C, de point d'extension bas niveau
    permettant de désactiver ces déclarations pendant l'analyse.
    """
    try:
        with open(gpx_path, 'rb') as f:
            head = f.read(65536)
    except OSError as e:
        raise RuntimeError(f"Impossible d'ouvrir le fichier GPX : {e}")
    low = head.lower()
    if b'<!doctype' in low or b'<!entity' in low:
        raise RuntimeError(
            "Fichier GPX refusé : il contient une déclaration DOCTYPE "
            "ou ENTITY, absente des traces GPX normales et associée "
            "aux attaques XXE. Utilisez le fichier .gpx d'origine, tel "
            "qu'exporté directement par votre traceur GPS.")


def parse_gpx_track(gpx_path):
    """
    Lit un fichier GPX et retourne la trace triée par temps croissant :
    [(t_epoch_utc, lat, lon, ele_ou_None), ...].

    Cherche des points horodatés (balise <time> obligatoire) parmi les
    <trkpt> de tous les <trkseg>/<trk> (trace, cas normal d'un traceur
    de randonnée) ; à défaut, se replie sur les <rtept> (itinéraire)
    puis les <wpt> (points de cheminement), au cas où l'appareil les
    aurait enregistrés autrement. Lève RuntimeError si le fichier est
    illisible, jugé dangereux (voir _reject_xxe) ou ne contient aucun
    point exploitable.
    """
    _reject_xxe(gpx_path)
    try:
        points_by_tag = {'trkpt': [], 'rtept': [], 'wpt': []}
        # _reject_xxe() ci-dessus a déjà écarté toute déclaration
        # DOCTYPE/ENTITY (protection XXE) avant d'atteindre cette
        # analyse XML standard.
        for _event, elem in ET.iterparse(  # nosec B314
                gpx_path, events=('end',)):
            tag = _local(elem.tag)
            if tag in points_by_tag:
                lat, lon = elem.get('lat'), elem.get('lon')
                if lat is not None and lon is not None:
                    t_text, ele_text = None, None
                    for child in elem:
                        ctag = _local(child.tag)
                        if ctag == 'time' and t_text is None:
                            t_text = child.text
                        elif ctag == 'ele' and ele_text is None:
                            ele_text = child.text
                    t = _parse_gpx_time(t_text)
                    if t is not None:
                        try:
                            lat_f, lon_f = float(lat), float(lon)
                        except (TypeError, ValueError):
                            lat_f = lon_f = None
                        if lat_f is not None:
                            ele_f = None
                            if ele_text:
                                try:
                                    ele_f = float(ele_text)
                                except (TypeError, ValueError):
                                    ele_f = None
                            points_by_tag[tag].append(
                                (t, lat_f, lon_f, ele_f))
                # Libère la mémoire du point une fois ses enfants lus
                # (traces de plusieurs dizaines de milliers de points) ;
                # ne PAS clear() les autres éléments : 'end' se déclenche
                # aussi pour <time>/<ele> avant leur parent <trkpt>, les
                # vider effacerait leur texte avant qu'on ait pu le lire.
                elem.clear()
    except ET.ParseError as e:
        raise RuntimeError(f"Fichier GPX illisible (XML invalide) : {e}")
    except OSError as e:
        raise RuntimeError(f"Impossible d'ouvrir le fichier GPX : {e}")

    for tag in ('trkpt', 'rtept', 'wpt'):
        pts = points_by_tag[tag]
        if pts:
            pts.sort(key=lambda p: p[0])
            return pts

    raise RuntimeError(
        "Aucun point horodaté trouvé dans ce fichier GPX (balises "
        "<trkpt>/<rtept>/<wpt> avec <time>). Vérifiez que "
        "l'enregistrement de la trace inclut l'horodatage (réglage "
        "courant, activé par défaut sur les traceurs Garmin et "
        "assimilés).")


def _interpolate(track, times, t):
    """
    Position interpolée sur la trace au temps 't' (secondes epoch UTC).
    'track' est la liste triée (t, lat, lon, ele) de parse_gpx_track(),
    et 'times' la liste de ses temps, précalculée une fois par
    l'appelant pour une recherche par dichotomie rapide même sur une
    trace de plusieurs dizaines de milliers de points.

    Retourne (lat, lon, ele_ou_None, gap_secondes) ou None si 't' est
    hors de la plage temporelle couverte par la trace. 'gap_secondes'
    est l'écart entre les deux points de trace encadrant 't' (0 si 't'
    tombe exactement sur un point) : l'appelant s'en sert pour rejeter
    une interpolation entre deux points trop éloignés dans le temps
    (perte de signal du traceur, ex. sous couvert forestier dense).
    """
    if not track or t < times[0] or t > times[-1]:
        return None
    idx = bisect.bisect_left(times, t)
    if times[idx] == t:
        _, lat, lon, ele = track[idx]
        return lat, lon, ele, 0.0
    # Le test de plage ci-dessus garantit 0 < idx < len(track) ici.
    t0, lat0, lon0, ele0 = track[idx - 1]
    t1, lat1, lon1, ele1 = track[idx]
    span = t1 - t0
    if span <= 0:
        return lat0, lon0, ele0, 0.0
    frac = (t - t0) / span
    lat = lat0 + (lat1 - lat0) * frac
    lon = lon0 + (lon1 - lon0) * frac
    ele = None
    if ele0 is not None and ele1 is not None:
        ele = ele0 + (ele1 - ele0) * frac
    return lat, lon, ele, span


def _fmt_utc(t_epoch):
    return datetime.datetime.fromtimestamp(
        t_epoch, tz=datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


# --------------------------------------------------------------------------
# Point d'entrée : géolocalisation d'un dossier de photos par corrélation GPX
# --------------------------------------------------------------------------

def geotag_photos_from_gpx(folder, gpx_path, time_offset_seconds=0.0,
                           max_gap_seconds=120.0, recursive=True,
                           layer_name='Photos géolocalisées (GPX)',
                           progress_cb=None, log_cb=None):
    """
    Géolocalise les photos d'un dossier par corrélation temporelle avec
    une trace GPX, pour les appareils sans GPS (reflex, compact) utilisés
    avec un traceur GPS de randonnée. Retourne (layer, stats), avec la
    même structure de couche (core.FIELD_DEFS, EPSG:4326) que
    core.import_photos(), pour rester compatible avec le tri par
    emprises et l'Atlas.

    Pour chaque photo :
      1. lecture de la date de prise de vue EXIF (indépendante d'une
         balise GPS : l'appareil n'en a justement pas) ;
      2. application de 'time_offset_seconds', ajouté à l'heure de
         l'appareil photo pour la ramener à l'heure GPS/UTC de la trace
         (fuseau horaire + dérive de l'horloge — déterminé par ex. en
         photographiant l'écran du traceur au début de la sortie et en
         comparant avec l'heure EXIF de cette photo, méthode
         GeoSetter/digiKam) ;
      3. interpolation linéaire de la position (et de l'altitude si
         disponible) entre les deux points GPX qui encadrent cet
         instant.

    - max_gap_seconds : écart temporel maximal toléré entre les deux
      points de trace encadrant la photo ; au-delà (traceur éteint ou
      signal perdu un moment), l'interpolation est jugée trop imprécise
      et la photo n'est pas géolocalisée (comptée dans 'hors_plage').
      0 ou négatif = aucune limite.
    """
    def log(msg):
        if log_cb:
            log_cb(msg)

    track = parse_gpx_track(gpx_path)
    times = [p[0] for p in track]
    log(f"Trace GPX chargée : {len(track)} point(s) horodaté(s), de "
        f"{_fmt_utc(times[0])} à {_fmt_utc(times[-1])}.")

    photos = list_photos(folder, recursive)
    layer = create_photo_layer(layer_name)
    provider = layer.dataProvider()
    fields = layer.fields()

    feats = []
    no_date, hors_plage = [], []
    total = len(photos)
    for i, path in enumerate(photos):
        if progress_cb:
            progress_cb(i + 1, total)
        try:
            date_str = _read_datetime(path)
        except Exception as e:
            # Un fichier imprévu (EXIF corrompu, format inattendu…) ne
            # doit jamais interrompre le traitement des autres photos.
            no_date.append(path)
            log(f"  · date EXIF illisible, photo ignorée : "
                f"{os.path.basename(path)} ({e})")
            continue
        t = _parse_exif_time(date_str)
        if t is None:
            no_date.append(path)
            continue

        result = _interpolate(track, times, t + time_offset_seconds)
        if result is None:
            hors_plage.append(path)
            continue
        lat, lon, ele, gap = result
        if max_gap_seconds and max_gap_seconds > 0 and gap > max_gap_seconds:
            hors_plage.append(path)
            log(f"  · {os.path.basename(path)} : points de trace encadrants "
                f"trop espacés ({gap:.0f} s > {max_gap_seconds:.0f} s), "
                f"ignorée.")
            continue

        f = QgsFeature(fields)
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
        f['photo'] = os.path.basename(path)
        f['chemin'] = path
        f['dossier'] = os.path.dirname(path)
        f['date_prise'] = date_str
        f['altitude'] = ele
        f['direction'] = None
        feats.append(f)

    provider.addFeatures(feats)
    layer.updateExtents()

    log(f"{total} fichier(s) image analysé(s), {len(feats)} géolocalisé(s) "
        f"par corrélation GPX, {len(no_date)} sans date de prise de vue "
        f"EXIF exploitable, {len(hors_plage)} hors plage temporelle de la "
        f"trace (ou écart trop important entre deux points).")
    for p in no_date[:20]:
        log(f"  · sans date EXIF : {os.path.basename(p)}")
    if len(no_date) > 20:
        log(f"  · … et {len(no_date) - 20} autres.")

    return layer, {'total': total, 'ok': len(feats),
                   'no_date': len(no_date), 'hors_plage': len(hors_plage)}
