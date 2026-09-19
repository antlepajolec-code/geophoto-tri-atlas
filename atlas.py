# -*- coding: utf-8 -*-
"""
GeoPhoto Tri & Atlas — génération d'une mise en page Atlas.

Structure produite (A4 paysage, contrôlée par l'Atlas sur la couche
d'emprises) :
  - Page 1 : titre (nom de l'emprise) + carte pilotée par l'Atlas,
             centrée sur l'entité courante ; les points-photos y sont
             étiquetés avec leur numéro dans le bloc ('num_bloc').
  - Page 2 : jusqu'à 4 photos de l'emprise courante, dans l'ordre de
             leur numéro, avec légende « n° X — nom du fichier », +
             table triée listant toutes les photos du bloc.

À l'export, l'Atlas parcourt les emprises : chaque entité génère donc
ses 2 pages (carte puis photos), jusqu'à passer à l'emprise suivante.
Les numéros affichés sur la carte correspondent aux numéros des photos.

La carte de l'Atlas fige, au moment de la création, les couches
actuellement affichées dans le canevas QGIS (fond de plan) ainsi que
la couche d'emprises et la couche de points — même si l'une d'elles
est décochée dans le panneau des couches — pour que la mise en page
reste stable si l'utilisateur modifie ensuite la visibilité des
couches dans son projet.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QFont, QColor
from qgis.core import (
    QgsPrintLayout, QgsLayoutItemMap, QgsLayoutItemLabel,
    QgsLayoutItemPage, QgsLayoutItemPicture, QgsLayoutItemAttributeTable,
    QgsLayoutFrame, QgsLayoutPoint, QgsLayoutSize, QgsUnitTypes,
    QgsProperty, QgsLayoutObject, QgsCoordinateTransform,
    QgsPalLayerSettings, QgsVectorLayerSimpleLabeling, QgsTextFormat,
    QgsTextBufferSettings, QgsMapLayerStyle, QgsMessageLog, Qgis,
)

_LOG_TAG = 'GeoPhoto Tri & Atlas'


def _log_warn(message):
    """Journalise un avertissement (utilisé dans les blocs defensifs
    where une fonctionnalite optionnelle/dependante de la version de
    QGIS peut echouer sans devoir interrompre la generation de l'Atlas)."""
    QgsMessageLog.logMessage(str(message), _LOG_TAG, level=Qgis.Warning)


def _match_filter(name_field):
    """
    Expression de filtre : le point appartient à l'emprise courante de
    l'Atlas. Le champ 'emprise' peut contenir plusieurs noms séparés
    par ';' (photo dans des emprises qui se chevauchent).

    to_string(...) sur la valeur de l'Atlas évite un défaut de
    correspondance silencieux si le champ choisi comme nom d'emprise
    est numérique (le champ 'emprise' de la couche de points est
    toujours du texte).
    """
    return ("array_contains(string_to_array(\"emprise\", ';'), "
            f"to_string(attribute(@atlas_feature, '{name_field}')))")


def _agg_expr(point_layer, name_field, value_expr, idx):
    """
    Expression retournant la (idx+1)-ième valeur de value_expr parmi les
    photos de l'emprise courante, ordonnées par leur numéro dans le bloc.
    """
    return (
        "array_get("
        f"aggregate(layer:='{point_layer.id()}', "
        "aggregate:='array_agg', "
        f"expression:={value_expr}, "
        f"filter:={_match_filter(name_field)}, "
        "order_by:=\"num_bloc\""
        f"), {idx})"
    )


def _photo_path_expr(point_layer, name_field, idx):
    """Chemin de la (idx+1)-ième photo du bloc (source de l'item Image)."""
    return _agg_expr(point_layer, name_field,
                     'coalesce("chemin_tri", "chemin")', idx)


def _caption_expr(point_layer, name_field, idx):
    """Légende « n° X — nom_fichier » sous la (idx+1)-ième photo."""
    num = _agg_expr(point_layer, name_field, '"num_bloc"', idx)
    name = _agg_expr(point_layer, name_field, '"photo"', idx)
    return (f"if({num} IS NOT NULL, "
            f"'n° ' || {num} || ' — ' || coalesce({name}, ''), '')")


def _unique_layout_name(project, base):
    manager = project.layoutManager()
    name, i = base, 1
    while manager.layoutByName(name):
        i += 1
        name = f'{base} {i}'
    return name


def _numbered_label_style_override(point_layer):
    """
    Construit un style de couche (XML) qui étiquette les points avec
    leur numéro dans le bloc ('num_bloc'), SANS modifier le style de la
    couche dans le projet : le style et l'étiquetage affichés dans le
    canevas principal restent intacts, seule la carte de la mise en
    page Atlas utilise ce style numéroté (via
    QgsLayoutItemMap.setLayerStyleOverrides).

    Retourne le XML du style, ou None si l'opération n'est pas possible
    (champ 'num_bloc' absent, ou API de style indisponible selon la
    version de QGIS — dans ce cas l'Atlas s'affiche simplement sans les
    numéros sur la carte).
    """
    if point_layer.fields().indexOf('num_bloc') == -1:
        return None

    original = QgsMapLayerStyle()
    original.readFromLayer(point_layer)
    xml = None
    try:
        settings = QgsPalLayerSettings()
        settings.fieldName = 'num_bloc'
        settings.isExpression = False

        fmt = QgsTextFormat()
        font = QFont('Arial', 9)
        font.setBold(True)
        fmt.setFont(font)
        fmt.setSize(9)
        fmt.setColor(QColor(0, 0, 0))
        buf = QgsTextBufferSettings()
        buf.setEnabled(True)
        buf.setSize(1.2)
        buf.setColor(QColor(255, 255, 255))
        fmt.setBuffer(buf)
        settings.setFormat(fmt)

        point_layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
        point_layer.setLabelsEnabled(True)

        numbered = QgsMapLayerStyle()
        numbered.readFromLayer(point_layer)
        if numbered.isValid():
            xml = numbered.xmlData()
    except Exception:
        xml = None
    finally:
        # Toujours restaurer le style d'origine sur la couche du projet,
        # que la capture ait réussi ou non.
        original.writeToLayer(point_layer)
        point_layer.triggerRepaint()

    return xml


def _build_locked_layers(canvas_layers, polygon_layer, point_layer):
    """
    Construit la liste des couches à figer dans la carte de l'Atlas, à
    partir de la vue courante du canevas QGIS (fond de plan
    actuellement affiché), en garantissant que la couche d'emprises et
    la couche de points en font partie — même si l'une d'elles est
    décochée dans le panneau des couches au moment de la création,
    puisqu'elles sont indispensables à la lecture de l'atlas.
    """
    layers = list(canvas_layers) if canvas_layers else []
    for must_have in (point_layer, polygon_layer):
        if must_have is not None and must_have not in layers:
            layers.insert(0, must_have)
    return layers


def create_atlas_layout(project, polygon_layer, name_field, point_layer,
                        base_name='Atlas photos par emprise',
                        canvas_layers=None, canvas_extent=None,
                        canvas_crs=None):
    """Crée la mise en page et la retourne.

    canvas_layers / canvas_extent / canvas_crs : état courant du
    canevas QGIS (iface.mapCanvas()), à passer pour que la carte de
    l'Atlas « importe la vue courante » — fond de plan affiché et
    étendue de départ — au lieu de systématiquement repartir de
    l'étendue totale de la couche d'emprises avec un fond de plan qui
    suit ensuite les changements de visibilité du projet. Facultatif :
    si omis (ex. appel depuis la console), le comportement retombe sur
    la couche d'emprises + la couche de points uniquement.
    """
    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    layout.setName(_unique_layout_name(project, base_name))

    # --- Pages : 2 x A4 paysage -------------------------------------------
    pages = layout.pageCollection()
    pages.page(0).setPageSize(
        'A4', QgsLayoutItemPage.Orientation.Landscape)
    page2 = QgsLayoutItemPage(layout)
    page2.setPageSize('A4', QgsLayoutItemPage.Orientation.Landscape)
    pages.addPage(page2)

    mm = QgsUnitTypes.LayoutUnit.LayoutMillimeters

    # ======================= PAGE 1 : CARTE =================================
    title = QgsLayoutItemLabel(layout)
    title.setText(f'Emprise : [% "{name_field}" %]')
    title.setFont(QFont('Arial', 20, QFont.Weight.Bold))
    layout.addLayoutItem(title)
    title.attemptResize(QgsLayoutSize(277, 10, mm))
    title.attemptMove(QgsLayoutPoint(10, 5, mm), page=0)

    map_item = QgsLayoutItemMap(layout)
    map_item.setFrameEnabled(True)
    layout.addLayoutItem(map_item)
    map_item.attemptResize(QgsLayoutSize(277, 185, mm))
    map_item.attemptMove(QgsLayoutPoint(10, 18, mm), page=0)

    # --- Vue courante : fond de plan figé -----------------------------------
    # Sans setLayers()/setKeepLayerSet(True), l'item carte suit
    # dynamiquement les couches cochées dans le projet : un changement
    # de visibilité après coup modifierait silencieusement l'atlas déjà
    # créé. On importe donc l'état courant du canevas, en s'assurant que
    # la couche d'emprises et la couche de points sont bien présentes.
    locked_layers = _build_locked_layers(canvas_layers, polygon_layer,
                                         point_layer)
    if locked_layers:
        map_item.setLayers(locked_layers)
        map_item.setKeepLayerSet(True)

    # --- Étendue initiale : vue courante du canevas si fournie, sinon
    # étendue totale des emprises (reprojetées dans le SCR du projet).
    extent = None
    if canvas_extent is not None and not canvas_extent.isEmpty():
        extent = canvas_extent
        if canvas_crs is not None and canvas_crs != project.crs():
            try:
                tr = QgsCoordinateTransform(canvas_crs, project.crs(), project)
                extent = tr.transformBoundingBox(extent)
            except Exception:
                extent = canvas_extent
    if extent is None or extent.isEmpty():
        extent = polygon_layer.extent()
        if polygon_layer.crs() != project.crs():
            try:
                tr = QgsCoordinateTransform(polygon_layer.crs(),
                                            project.crs(), project)
                extent = tr.transformBoundingBox(extent)
            except Exception as exc:
                _log_warn(f"Reprojection de l'emprise pour la carte de "
                          f"l'Atlas impossible, etendue non recadree : {exc}")
    if not extent.isEmpty():
        map_item.setExtent(extent)

    # La carte suit l'entité courante de l'Atlas, avec 15 % de marge
    map_item.setAtlasDriven(True)
    map_item.setAtlasScalingMode(QgsLayoutItemMap.AtlasScalingMode.Auto)
    map_item.setAtlasMargin(0.15)

    # Étiquetage numéroté des points, appliqué uniquement à cette carte
    # de mise en page (le style de la couche dans le projet n'est pas
    # modifié).
    override_xml = _numbered_label_style_override(point_layer)
    if override_xml:
        try:
            map_item.setLayerStyleOverrides({point_layer.id(): override_xml})
        except Exception as exc:
            _log_warn(f"Style d'etiquetage numerote non applique a la "
                      f"carte de l'Atlas : {exc}")

    # ======================= PAGE 2 : PHOTOS ================================
    title2 = QgsLayoutItemLabel(layout)
    title2.setText(f'Photos — emprise : [% "{name_field}" %]')
    title2.setFont(QFont('Arial', 16, QFont.Weight.Bold))
    layout.addLayoutItem(title2)
    title2.attemptResize(QgsLayoutSize(277, 9, mm))
    title2.attemptMove(QgsLayoutPoint(10, 5, mm), page=1)

    # Grille de 4 photos numérotées (emplacements vides si moins de photos)
    positions = [(10, 17), (98, 17), (10, 112), (98, 112)]
    for i, (x, y) in enumerate(positions):
        pic = QgsLayoutItemPicture(layout)
        pic.setResizeMode(QgsLayoutItemPicture.ResizeMode.Zoom)
        pic.setFrameEnabled(False)
        pic.dataDefinedProperties().setProperty(
            QgsLayoutObject.DataDefinedProperty.PictureSource,
            QgsProperty.fromExpression(
                _photo_path_expr(point_layer, name_field, i)))
        layout.addLayoutItem(pic)
        pic.attemptResize(QgsLayoutSize(84, 84, mm))
        pic.attemptMove(QgsLayoutPoint(x, y, mm), page=1)

        caption = QgsLayoutItemLabel(layout)
        caption.setText(
            f'[% {_caption_expr(point_layer, name_field, i)} %]')
        caption.setFont(QFont('Arial', 9))
        caption.setHAlign(Qt.AlignmentFlag.AlignHCenter)
        layout.addLayoutItem(caption)
        caption.attemptResize(QgsLayoutSize(84, 6, mm))
        caption.attemptMove(QgsLayoutPoint(x, y + 85, mm), page=1)

    # Table des matières des photos de l'emprise courante, triée par n°
    table = QgsLayoutItemAttributeTable(layout)
    layout.addMultiFrame(table)
    table.setVectorLayer(point_layer)
    table.setFilterFeatures(True)
    table.setFeatureFilter(_match_filter(name_field))
    try:
        wanted = ('num_bloc', 'photo', 'date_prise', 'dist_emprise')
        cols = {c.attribute(): c for c in table.columns()}
        keep = [cols[a] for a in wanted if a in cols]
        if keep:
            try:
                keep[0].setSortByRank(1)          # tri par num_bloc
                keep[0].setSortOrder(Qt.SortOrder.AscendingOrder)
            except AttributeError:
                pass
            table.setColumns(keep)
    except Exception as exc:
        # selon versions de QGIS : on garde alors toutes les colonnes
        _log_warn(f"Selection des colonnes de la table de photos "
                  f"impossible, toutes les colonnes seront affichees : "
                  f"{exc}")
    try:
        # API de tri moderne (QGIS >= 3.14)
        from qgis.core import QgsLayoutTableColumn
        sort_col = QgsLayoutTableColumn()
        sort_col.setAttribute('num_bloc')
        sort_col.setSortOrder(Qt.SortOrder.AscendingOrder)
        table.setSortColumns([sort_col])
    except Exception as exc:
        _log_warn(f"Tri de la table de photos par numero de bloc "
                  f"impossible (API indisponible sur cette version de "
                  f"QGIS) : {exc}")
    try:
        table.setEmptyTableBehavior(
            QgsLayoutItemAttributeTable.EmptyTableMode.ShowMessage)
        table.setEmptyTableMessage('Aucune photo dans cette emprise.')
    except Exception as exc:
        _log_warn(f"Message de table vide non configure : {exc}")

    frame = QgsLayoutFrame(layout, table)
    frame.attemptResize(QgsLayoutSize(97, 185, mm))
    table.addFrame(frame)
    frame.attemptMove(QgsLayoutPoint(190, 17, mm), page=1)

    # ======================= CONFIGURATION ATLAS ============================
    atlas = layout.atlas()
    atlas.setCoverageLayer(polygon_layer)
    atlas.setEnabled(True)
    atlas.setPageNameExpression(f'"{name_field}"')
    atlas.setFilenameExpression(f"'atlas_' || \"{name_field}\"")
    try:
        atlas.setSortFeatures(True)
        atlas.setSortExpression(f'"{name_field}"')
    except Exception as exc:
        _log_warn(f"Tri de l'Atlas par '{name_field}' impossible : {exc}")

    project.layoutManager().addLayout(layout)
    return layout
