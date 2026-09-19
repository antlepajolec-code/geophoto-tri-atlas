# -*- coding: utf-8 -*-
"""
GeoPhoto Tri & Atlas — analyseur EXIF GPS de secours (pur Python).

Utilisé si QgsExifTools échoue sur un fichier. Lit directement la
structure TIFF de l'APP1 Exif d'un JPEG : IFD0 -> GPS IFD (0x8825) et
Exif IFD (0x8769) pour la date de prise de vue.

Gère explicitement :
  - les références N/S/E/W (longitude Ouest => valeur négative) ;
  - les composantes à 0° (proximité du méridien de Greenwich), qui ne
    signifient PAS « absence de GPS » ;
  - les deux boutismes TIFF ('II' little-endian, 'MM' big-endian) ;
  - les balises GPS PRÉSENTES MAIS VIDES/PLACEHOLDER (rationnels
    0/0, référence absente) : certains téléphones (constaté sur un
    Samsung Galaxy A26 5G) écrivent systématiquement la structure GPS
    dans l'EXIF, même quand la localisation n'a pas pu être acquise au
    moment de la prise de vue. Sans validation, ces valeurs se
    traduisaient en un point (0°, 0°) exact (« Null Island », au large
    du Golfe de Guinée) au lieu d'être traitées comme une absence de
    GPS — d'où des photos géolocalisées loin de leur emplacement réel.
"""

import struct

_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


def _find_exif_tiff(path):
    """Retourne (données TIFF, 0) de l'APP1 Exif d'un JPEG, ou None."""
    with open(path, 'rb') as f:
        data = f.read()
    if data[:2] != b'\xff\xd8':          # pas un JPEG : TIFF direct ?
        if data[:2] in (b'II', b'MM'):
            return data
        return None
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xDA:               # début des données image
            break
        length = struct.unpack('>H', data[i + 2:i + 4])[0]
        if marker == 0xE1 and data[i + 4:i + 10] == b'Exif\x00\x00':
            return data[i + 10:i + 2 + length]
        i += 2 + length
    return None


def _read_ifd(tiff, endian, offset):
    """Lit une IFD et retourne {tag: (type, count, valeur_ou_offset_bytes)}."""
    entries = {}
    if offset + 2 > len(tiff):
        return entries
    n = struct.unpack(endian + 'H', tiff[offset:offset + 2])[0]
    for k in range(n):
        base = offset + 2 + 12 * k
        if base + 12 > len(tiff):
            break
        tag, typ, count = struct.unpack(endian + 'HHL', tiff[base:base + 8])
        entries[tag] = (typ, count, tiff[base + 8:base + 12])
    return entries


def _values(tiff, endian, entry):
    """
    Décode les valeurs d'une entrée IFD (liste).

    Pour un rationnel (types 5/10) dont le dénominateur est nul, la
    valeur décodée est None (et NON 0.0) : un dénominateur nul signale
    un composant illisible/placeholder, à distinguer d'une vraie valeur
    nulle (0.0 est une coordonnée parfaitement valide, ex. proche du
    méridien de Greenwich). Voir _dms_to_dd et read_gps_exif, qui
    rejettent une balise GPS dès qu'un composant vaut None.
    """
    typ, count, raw = entry
    size = _TYPE_SIZES.get(typ)
    if size is None:
        return None
    total = size * count
    if total <= 4:
        buf = raw[:total]
    else:
        off = struct.unpack(endian + 'L', raw)[0]
        if off + total > len(tiff):
            return None
        buf = tiff[off:off + total]

    if typ == 2:                                    # ASCII
        return [buf.split(b'\x00')[0].decode('ascii', 'replace')]
    if typ == 1 or typ == 7:                        # BYTE / UNDEFINED
        return list(buf)
    if typ == 3:                                    # SHORT
        return list(struct.unpack(endian + f'{count}H', buf))
    if typ in (4, 9):                               # LONG / SLONG
        fmt = 'L' if typ == 4 else 'l'
        return list(struct.unpack(endian + f'{count}{fmt}', buf))
    if typ in (5, 10):                              # RATIONAL / SRATIONAL
        fmt = 'LL' if typ == 5 else 'll'
        out = []
        for k in range(count):
            num, den = struct.unpack(endian + fmt, buf[8 * k:8 * k + 8])
            out.append(num / den if den else None)
        return out
    return None


def _clean_ref(values, default):
    """
    Normalise une référence GPS (ex. GPSLatitudeRef) : chaîne ASCII en
    majuscule, sans octets nuls ni espaces. 'values' est le résultat de
    _values() pour la balise de référence, ou None si la balise est
    absente (auquel cas 'default' — 'N' ou 'E' — est renvoyé, pour les
    rares appareils qui omettent la référence tout en écrivant des
    coordonnées positives valides).

    Une balise PRÉSENTE mais vide après nettoyage (ex. l'octet nul
    '\\x00' constaté sur certains téléphones) renvoie une chaîne vide,
    délibérément PAS remplacée par 'default' : c'est ce qui permet à
    l'appelant de distinguer une référence réellement absente d'une
    référence présente mais invalide/placeholder, et de rejeter cette
    dernière plutôt que de supposer un hémisphère par défaut.
    """
    if values is None:
        return default
    raw = values[0] if values else ''
    return str(raw).strip('\x00 \t\r\n').upper()


def _dms_to_dd(dms, ref):
    """Degrés/minutes/secondes -> degrés décimaux signés selon la réf.

    Une composante à 0 (ex. longitude 0° 27' 49" près de Greenwich)
    est parfaitement valide : seule l'absence de balise, ou un
    composant non décodable (dénominateur nul dans le rationnel EXIF —
    voir _values), est un échec. 'ref' doit déjà être nettoyée (voir
    _clean_ref) : 'S' ou 'W' inverse le signe, toute autre valeur
    (dont 'N'/'E') le laisse positif.
    """
    if not dms or any(v is None for v in dms):
        return None
    d = dms[0] if len(dms) > 0 else 0.0
    m = dms[1] if len(dms) > 1 else 0.0
    s = dms[2] if len(dms) > 2 else 0.0
    dd = d + m / 60.0 + s / 3600.0
    return -dd if ref in ('S', 'W') else dd


def read_gps_exif(path):
    """
    Lecture EXIF de secours. Retourne un dict
    {x (lon), y (lat), z (alt|None), date (str|None), direction (float|None)}
    ou None si aucune coordonnée GPS exploitable n'est présente —
    balise absente, OU balise présente mais vide/placeholder (référence
    N/S/E/W absente ou invalide, composants non décodables, ou
    coordonnée (0°, 0°) exacte : une vraie prise de vue de terrain ne
    tombe jamais pile sur ce point, c'est la signature d'une balise
    GPS écrite sans localisation acquise).
    """
    try:
        tiff = _find_exif_tiff(path)
    except (OSError, struct.error):
        return None
    if not tiff or len(tiff) < 8:
        return None

    order = tiff[:2]
    endian = '<' if order == b'II' else '>' if order == b'MM' else None
    if endian is None:
        return None
    try:
        ifd0_off = struct.unpack(endian + 'L', tiff[4:8])[0]
        ifd0 = _read_ifd(tiff, endian, ifd0_off)

        gps_ifd, exif_ifd = {}, {}
        if 0x8825 in ifd0:                          # pointeur GPS IFD
            off = _values(tiff, endian, ifd0[0x8825])
            if off and off[0] is not None:
                gps_ifd = _read_ifd(tiff, endian, off[0])
        if 0x8769 in ifd0:                          # pointeur Exif IFD
            off = _values(tiff, endian, ifd0[0x8769])
            if off and off[0] is not None:
                exif_ifd = _read_ifd(tiff, endian, off[0])

        if 0x0002 not in gps_ifd or 0x0004 not in gps_ifd:
            return None

        lat_ref = _clean_ref(
            _values(tiff, endian, gps_ifd[0x0001]) if 0x0001 in gps_ifd else None,
            'N')
        lon_ref = _clean_ref(
            _values(tiff, endian, gps_ifd[0x0003]) if 0x0003 in gps_ifd else None,
            'E')
        if lat_ref not in ('N', 'S') or lon_ref not in ('E', 'W'):
            # Référence absente/illisible : balise GPS non exploitable
            # (une vraie balise contient toujours N/S et E/W).
            return None

        lat = _dms_to_dd(_values(tiff, endian, gps_ifd[0x0002]), lat_ref)
        lon = _dms_to_dd(_values(tiff, endian, gps_ifd[0x0004]), lon_ref)
        if lat is None or lon is None:
            return None
        if lat == 0.0 and lon == 0.0:
            # Filet de sécurité supplémentaire (voir docstring).
            return None

        alt = None
        if 0x0006 in gps_ifd:
            vals = _values(tiff, endian, gps_ifd[0x0006])
            if vals and vals[0] is not None:
                alt = float(vals[0])
                if 0x0005 in gps_ifd:               # 1 = sous le niveau mer
                    ref = _values(tiff, endian, gps_ifd[0x0005])
                    if ref and ref[0] == 1:
                        alt = -alt

        direction = None
        if 0x0011 in gps_ifd:
            vals = _values(tiff, endian, gps_ifd[0x0011])
            if vals and vals[0] is not None:
                direction = float(vals[0])

        date = None
        if 0x9003 in exif_ifd:                      # DateTimeOriginal
            vals = _values(tiff, endian, exif_ifd[0x9003])
            if vals and vals[0]:
                # 'AAAA:MM:JJ HH:MM:SS' -> 'AAAA-MM-JJ HH:MM:SS'
                d = vals[0]
                if len(d) >= 10:
                    d = d[:10].replace(':', '-') + d[10:]
                date = d

        return {'x': lon, 'y': lat, 'z': alt,
                'date': date, 'direction': direction}
    except (struct.error, IndexError, KeyError, ValueError, TypeError):
        return None


def describe_raw_gps(path):
    """
    Diagnostic bas niveau de la balise GPS EXIF brute d'un fichier,
    utilisé par core.diagnose_photo() pour distinguer une balise GPS
    absente d'une balise présente mais vide/placeholder — signature
    d'un appareil qui écrit systématiquement la structure GPS même
    sans localisation acquise (constaté sur un Samsung Galaxy A26 5G :
    composants à 0/0, référence à l'octet nul). Retourne une phrase
    courte, ou None si le fichier n'a pas pu être analysé.
    """
    try:
        tiff = _find_exif_tiff(path)
    except (OSError, struct.error):
        return None
    if not tiff or len(tiff) < 8:
        return None
    order = tiff[:2]
    endian = '<' if order == b'II' else '>' if order == b'MM' else None
    if endian is None:
        return None
    try:
        ifd0_off = struct.unpack(endian + 'L', tiff[4:8])[0]
        ifd0 = _read_ifd(tiff, endian, ifd0_off)
        if 0x8825 not in ifd0:
            return 'aucune balise GPS EXIF dans le fichier.'
        off = _values(tiff, endian, ifd0[0x8825])
        if not off or off[0] is None:
            return 'pointeur vers la balise GPS EXIF illisible.'
        gps_ifd = _read_ifd(tiff, endian, off[0])
        if 0x0002 not in gps_ifd or 0x0004 not in gps_ifd:
            return 'balise GPS EXIF incomplète (latitude/longitude absentes).'

        lat_ref = _clean_ref(
            _values(tiff, endian, gps_ifd[0x0001]) if 0x0001 in gps_ifd else None,
            'N')
        lon_ref = _clean_ref(
            _values(tiff, endian, gps_ifd[0x0003]) if 0x0003 in gps_ifd else None,
            'E')
        lat_vals = _values(tiff, endian, gps_ifd[0x0002]) or []
        lon_vals = _values(tiff, endian, gps_ifd[0x0004]) or []
        ref_invalid = lat_ref not in ('N', 'S') or lon_ref not in ('E', 'W')
        components_invalid = (
            any(v is None for v in lat_vals) or any(v is None for v in lon_vals)
            or (all(v == 0 for v in lat_vals) and all(v == 0 for v in lon_vals)))

        if ref_invalid or components_invalid:
            return ('balise GPS EXIF PRÉSENTE MAIS VIDE (coordonnées et/ou '
                    'référence N/S/E/W nulles) : l\'appareil écrit la '
                    'structure GPS même sans localisation acquise au moment '
                    'de la prise de vue. Traitée comme « sans GPS » par le '
                    'plugin — le fichier d\'origine n\'a pas de position '
                    'exploitable, il n\'y a rien à corriger côté import.')
        return 'balise GPS EXIF présente et exploitable.'
    except (struct.error, IndexError, KeyError, ValueError, TypeError):
        return None
