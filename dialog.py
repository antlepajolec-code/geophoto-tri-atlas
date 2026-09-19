# -*- coding: utf-8 -*-
"""
GeoPhoto Tri & Atlas — interface graphique.

Trois onglets :
  1. Importer les photos géoréférencées (EXIF GPS -> couche de points).
  2. Trier les photos par emprises polygonales (manuel ou automatique).
  3. Générer une mise en page Atlas 2 pages (carte + photos).
"""

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QTabWidget, QWidget,
    QLabel, QLineEdit, QCheckBox, QPushButton, QRadioButton, QButtonGroup,
    QPlainTextEdit, QProgressBar, QMessageBox, QGroupBox, QApplication,
    QDoubleSpinBox, QFileDialog,
)
from qgis.core import QgsProject, QgsMapLayerProxyModel, QgsFieldProxyModel
from qgis.gui import QgsMapLayerComboBox, QgsFieldComboBox, QgsFileWidget

from . import core
from . import atlas as atlas_mod


class GeoPhotoDialog(QDialog):

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle('GeoPhoto – Tri & Atlas')
        self.setMinimumWidth(620)
        self._build_ui()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        main = QVBoxLayout(self)
        self.tabs = QTabWidget()
        main.addWidget(self.tabs)

        self.tabs.addTab(self._tab_import(), '1. Importer les photos')
        self.tabs.addTab(self._tab_sort(), '2. Trier par emprises')
        self.tabs.addTab(self._tab_atlas(), '3. Mise en page Atlas')

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        main.addWidget(self.progress)

        main.addWidget(QLabel('Journal :'))
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(140)
        main.addWidget(self.log_box)

        close_row = QHBoxLayout()
        close_row.addStretch()
        btn_close = QPushButton('Fermer')
        btn_close.clicked.connect(self.close)
        close_row.addWidget(btn_close)
        main.addLayout(close_row)

    def _tab_import(self):
        w = QWidget()
        form = QFormLayout(w)

        self.src_folder = QgsFileWidget()
        self.src_folder.setStorageMode(QgsFileWidget.GetDirectory)
        self.src_folder.setDialogTitle('Dossier contenant les photos')
        form.addRow('Dossier des photos :', self.src_folder)

        self.chk_recursive = QCheckBox('Inclure les sous-dossiers')
        self.chk_recursive.setChecked(True)
        form.addRow('', self.chk_recursive)

        self.layer_name = QLineEdit('Photos géolocalisées')
        form.addRow('Nom de la couche :', self.layer_name)

        self.chk_gpkg = QCheckBox(
            'Enregistrer en GeoPackage (sinon couche mémoire temporaire)')
        form.addRow('', self.chk_gpkg)
        self.gpkg_path = QgsFileWidget()
        self.gpkg_path.setStorageMode(QgsFileWidget.SaveFile)
        self.gpkg_path.setFilter('GeoPackage (*.gpkg)')
        self.gpkg_path.setEnabled(False)
        self.chk_gpkg.toggled.connect(self.gpkg_path.setEnabled)
        form.addRow('Fichier GeoPackage :', self.gpkg_path)

        info = QLabel(
            'Les coordonnées GPS EXIF sont exprimées en WGS84 : la couche '
            'est donc créée en EPSG:4326 et reprojetée à la volée par QGIS '
            'vers le SCR du projet (ex. Lambert-93 / EPSG:2154). Le nom du '
            'fichier, son chemin, la date de prise de vue, l\'altitude et '
            'la direction sont stockés en attributs (visibles dans la '
            'table attributaire).')
        info.setWordWrap(True)
        form.addRow(info)

        btn = QPushButton('Importer et créer la couche de points')
        btn.clicked.connect(self.run_import)
        form.addRow(btn)

        btn_diag = QPushButton('Diagnostiquer un fichier photo… '
                               '(vérifier la présence du GPS)')
        btn_diag.clicked.connect(self.run_diagnose)
        form.addRow(btn_diag)
        return w

    def _tab_sort(self):
        w = QWidget()
        form = QFormLayout(w)

        self.cbo_points = QgsMapLayerComboBox()
        self.cbo_points.setFilters(QgsMapLayerProxyModel.PointLayer)
        form.addRow('Couche de points (photos) :', self.cbo_points)

        self.cbo_polys = QgsMapLayerComboBox()
        self.cbo_polys.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        form.addRow('Couche d\'emprises (polygones) :', self.cbo_polys)

        self.cbo_field = QgsFieldComboBox()
        self.cbo_field.setFilters(QgsFieldProxyModel.String |
                                  QgsFieldProxyModel.Numeric)
        self.cbo_polys.layerChanged.connect(self.cbo_field.setLayer)
        self.cbo_field.setLayer(self.cbo_polys.currentLayer())
        form.addRow('Champ « nom d\'emprise » :', self.cbo_field)

        self.dest_folder = QgsFileWidget()
        self.dest_folder.setStorageMode(QgsFileWidget.GetDirectory)
        self.dest_folder.setDialogTitle(
            'Dossier d\'atterrissage des photos triées')
        form.addRow('Dossier de rangement :', self.dest_folder)

        grp_mode = QGroupBox('Emprises à traiter')
        gm = QVBoxLayout(grp_mode)
        self.rad_auto = QRadioButton(
            'Automatique : toutes les entités de la couche')
        self.rad_sel = QRadioButton(
            'Manuel : uniquement les entités sélectionnées sur la carte')
        self.rad_auto.setChecked(True)
        bg = QButtonGroup(w)
        bg.addButton(self.rad_auto)
        bg.addButton(self.rad_sel)
        gm.addWidget(self.rad_auto)
        gm.addWidget(self.rad_sel)
        form.addRow(grp_mode)

        grp_op = QGroupBox('Opération sur les fichiers')
        go = QVBoxLayout(grp_op)
        self.rad_copy = QRadioButton('Copier (les originaux sont conservés)')
        self.rad_move = QRadioButton('Déplacer (les originaux sont supprimés)')
        self.rad_copy.setChecked(True)
        bg2 = QButtonGroup(w)
        bg2.addButton(self.rad_copy)
        bg2.addButton(self.rad_move)
        go.addWidget(self.rad_copy)
        go.addWidget(self.rad_move)
        form.addRow(grp_op)

        self.chk_multi = QCheckBox(
            'Si une photo tombe dans plusieurs emprises, la copier dans chacune')
        self.chk_multi.setChecked(True)
        form.addRow('', self.chk_multi)

        self.chk_nearest = QCheckBox(
            'Associer les photos hors emprise à l\'emprise la plus proche')
        self.spin_maxdist = QDoubleSpinBox()
        self.spin_maxdist.setRange(0.0, 1e9)
        self.spin_maxdist.setDecimals(1)
        self.spin_maxdist.setValue(50.0)
        self.spin_maxdist.setSuffix(' m')
        self.spin_maxdist.setToolTip(
            'Distance maximale de rattachement, mesurée en mètres sur '
            'l\'ellipsoïde. 0 = illimitée : chaque photo hors emprise est '
            'toujours rattachée à la plus proche.')
        self.spin_maxdist.setEnabled(False)
        self.chk_nearest.toggled.connect(self.spin_maxdist.setEnabled)
        lbl_maxdist = QLabel('distance max. (0 = illimitée) :')
        row_near = QHBoxLayout()
        row_near.addWidget(self.chk_nearest)
        row_near.addStretch()
        row_near.addWidget(lbl_maxdist)
        row_near.addWidget(self.spin_maxdist)
        form.addRow(row_near)

        self.chk_near_sub = QCheckBox(
            'Ranger ces photos rattachées dans un sous-dossier du bloc :')
        self.chk_near_sub.setChecked(True)
        self.chk_near_sub.setEnabled(False)
        self.near_sub_name = QLineEdit('_proximite')
        self.near_sub_name.setEnabled(False)
        self.near_sub_name.setToolTip(
            'Nom du sous-dossier créé automatiquement dans le dossier de '
            'chaque bloc pour les photos en limite ou à proximité '
            '(ex. Bloc_A/_proximite/).')

        def _nearest_toggled(on):
            self.chk_near_sub.setEnabled(on)
            self.near_sub_name.setEnabled(on and self.chk_near_sub.isChecked())
        self.chk_nearest.toggled.connect(_nearest_toggled)
        self.chk_near_sub.toggled.connect(
            lambda on: self.near_sub_name.setEnabled(
                on and self.chk_nearest.isChecked()))

        row_sub = QHBoxLayout()
        row_sub.addSpacing(24)
        row_sub.addWidget(self.chk_near_sub)
        row_sub.addWidget(self.near_sub_name)
        form.addRow(row_sub)

        self.chk_number = QCheckBox(
            'Préfixer les fichiers copiés par leur n° dans le bloc '
            '(001_, 002_, …)')
        self.chk_number.setChecked(True)
        self.chk_number.setToolTip(
            'Les photos de chaque bloc sont numérotées 1, 2, 3… dans '
            'l\'ordre de leur date de prise de vue EXIF. Ce numéro est '
            'stocké dans le champ « num_bloc », affiché sur la carte et '
            'dans l\'Atlas ; cette option l\'ajoute aussi en préfixe du '
            'nom de fichier pour que le tri des dossiers suive le même '
            'ordre.')
        form.addRow('', self.chk_number)

        self.chk_unmatched = QCheckBox('Ranger aussi les photos hors emprise dans :')
        self.chk_unmatched.setChecked(True)
        self.unmatched_name = QLineEdit('_hors_emprises')
        row = QHBoxLayout()
        row.addWidget(self.chk_unmatched)
        row.addWidget(self.unmatched_name)
        form.addRow(row)

        info = QLabel(
            'Dans chaque bloc, les photos sont numérotées 1, 2, 3… selon '
            'leur date de prise de vue EXIF (champ « num_bloc »). Les '
            'champs « emprise », « chemin_tri », « dist_emprise » et '
            '« num_bloc » sont mis à jour : ils alimentent la carte '
            '(étiquettes), la table et l\'Atlas.')
        info.setWordWrap(True)
        form.addRow(info)

        btn = QPushButton('Lancer le tri des photos')
        btn.clicked.connect(self.run_sort)
        form.addRow(btn)
        return w

    def _tab_atlas(self):
        w = QWidget()
        form = QFormLayout(w)

        self.cbo_polys_a = QgsMapLayerComboBox()
        self.cbo_polys_a.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        form.addRow('Couche d\'emprises :', self.cbo_polys_a)

        self.cbo_field_a = QgsFieldComboBox()
        self.cbo_polys_a.layerChanged.connect(self.cbo_field_a.setLayer)
        self.cbo_field_a.setLayer(self.cbo_polys_a.currentLayer())
        form.addRow('Champ « nom d\'emprise » :', self.cbo_field_a)

        self.cbo_points_a = QgsMapLayerComboBox()
        self.cbo_points_a.setFilters(QgsMapLayerProxyModel.PointLayer)
        form.addRow('Couche de points (photos) :', self.cbo_points_a)

        info = QLabel(
            'Crée une mise en page A4 paysage à 2 pages pilotée par '
            'l\'Atlas : page 1 = carte de l\'emprise courante avec les '
            'points étiquetés par leur numéro (num_bloc) ; page 2 = '
            'jusqu\'à 4 photos dans l\'ordre des numéros, avec légende '
            '« n° X — fichier », + table triée des photos du bloc. '
            'La carte importe la vue courante du canevas (fond de plan '
            'affiché + étendue) et fige les couches utilisées, pour que '
            'la mise en page ne change plus si vous modifiez ensuite la '
            'visibilité des couches dans le projet ; l\'étiquetage '
            'numéroté n\'est appliqué que dans la mise en page, sans '
            'toucher au style de la couche dans le projet. '
            'Lancer d\'abord l\'étape 2 (qui calcule la numérotation), '
            'puis dans la mise en page cocher « Aperçu de l\'Atlas » '
            'pour naviguer d\'emprise en emprise. À l\'export, chaque '
            'emprise produit ses 2 pages avant de passer à la suivante.')
        info.setWordWrap(True)
        form.addRow(info)

        btn = QPushButton('Créer la mise en page Atlas')
        btn.clicked.connect(self.run_atlas)
        form.addRow(btn)
        return w

    # ------------------------------------------------------------- helpers
    def log(self, msg):
        self.log_box.appendPlainText(msg)

    def _progress(self, current, total):
        self.progress.setVisible(True)
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(current)
        QApplication.processEvents()

    def _progress_done(self):
        self.progress.setVisible(False)

    # ------------------------------------------------------------- actions
    def run_diagnose(self):
        start_dir = self.src_folder.filePath() or ''
        path, _ = QFileDialog.getOpenFileName(
            self, 'Choisir une photo à diagnostiquer', start_dir,
            'Images (*.jpg *.jpeg *.jpe *.tif *.tiff *.png *.webp '
            '*.heic *.heif);;Tous les fichiers (*)')
        if not path:
            return
        self.log(core.diagnose_photo(path))

    def run_import(self):
        folder = self.src_folder.filePath()
        if not folder or not os.path.isdir(folder):
            QMessageBox.warning(self, 'GeoPhoto',
                                'Veuillez choisir un dossier de photos valide.')
            return
        try:
            layer, stats = core.import_photos(
                folder,
                recursive=self.chk_recursive.isChecked(),
                layer_name=self.layer_name.text().strip() or 'Photos géolocalisées',
                progress_cb=self._progress,
                log_cb=self.log)
        except Exception as e:
            self._progress_done()
            QMessageBox.critical(self, 'GeoPhoto', str(e))
            return
        self._progress_done()

        if stats['ok'] == 0:
            QMessageBox.information(
                self, 'GeoPhoto',
                'Aucune photo géoréférencée trouvée dans ce dossier.')
            return

        if self.chk_gpkg.isChecked():
            gpkg = self.gpkg_path.filePath()
            if not gpkg:
                QMessageBox.warning(self, 'GeoPhoto',
                                    'Veuillez indiquer le fichier GeoPackage.')
                return
            if not gpkg.lower().endswith('.gpkg'):
                gpkg += '.gpkg'
            try:
                layer = core.save_as_gpkg(layer, gpkg)
                self.log(f'Couche enregistrée : {gpkg}')
            except Exception as e:
                QMessageBox.critical(self, 'GeoPhoto', str(e))
                return

        QgsProject.instance().addMapLayer(layer)
        self.cbo_points.setLayer(layer)
        self.cbo_points_a.setLayer(layer)
        self.log(f"Couche « {layer.name()} » ajoutée au projet "
                 f"({stats['ok']} point(s), EPSG:4326).")

    def run_sort(self):
        pt_layer = self.cbo_points.currentLayer()
        poly_layer = self.cbo_polys.currentLayer()
        field = self.cbo_field.currentField()
        dest = self.dest_folder.filePath()

        if pt_layer is None or poly_layer is None:
            QMessageBox.warning(self, 'GeoPhoto',
                                'Veuillez choisir les couches de points et '
                                'de polygones.')
            return
        if not field:
            QMessageBox.warning(self, 'GeoPhoto',
                                'Veuillez choisir le champ contenant le nom '
                                'des emprises.')
            return
        if not dest:
            QMessageBox.warning(self, 'GeoPhoto',
                                'Veuillez choisir le dossier de rangement.')
            return
        if self.rad_sel.isChecked() and poly_layer.selectedFeatureCount() == 0:
            QMessageBox.warning(
                self, 'GeoPhoto',
                'Mode manuel : sélectionnez d\'abord une ou plusieurs '
                'entités dans la couche d\'emprises.')
            return
        if self.rad_move.isChecked():
            rep = QMessageBox.question(
                self, 'GeoPhoto',
                'Mode « Déplacer » : les fichiers originaux seront '
                'supprimés après copie. Continuer ?',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if rep != QMessageBox.Yes:
                return

        near_sub = None
        if self.chk_nearest.isChecked() and self.chk_near_sub.isChecked():
            near_sub = self.near_sub_name.text().strip() or '_proximite'

        try:
            core.sort_photos(
                pt_layer, poly_layer, field, dest,
                move=self.rad_move.isChecked(),
                only_selected=self.rad_sel.isChecked(),
                duplicate_multi=self.chk_multi.isChecked(),
                handle_unmatched=self.chk_unmatched.isChecked(),
                unmatched_name=self.unmatched_name.text().strip()
                or '_hors_emprises',
                assign_nearest=self.chk_nearest.isChecked(),
                max_distance_m=self.spin_maxdist.value(),
                nearest_subfolder=near_sub,
                number_files=self.chk_number.isChecked(),
                progress_cb=self._progress,
                log_cb=self.log)
        except Exception as e:
            self._progress_done()
            QMessageBox.critical(self, 'GeoPhoto', str(e))
            return
        self._progress_done()
        self.log(f'Dossier de rangement : {dest}')

    def run_atlas(self):
        poly_layer = self.cbo_polys_a.currentLayer()
        field = self.cbo_field_a.currentField()
        pt_layer = self.cbo_points_a.currentLayer()
        if poly_layer is None or pt_layer is None or not field:
            QMessageBox.warning(self, 'GeoPhoto',
                                'Veuillez renseigner les couches et le champ.')
            return
        if (pt_layer.fields().indexOf('emprise') == -1
                or pt_layer.fields().indexOf('num_bloc') == -1):
            QMessageBox.warning(
                self, 'GeoPhoto',
                'La couche de points ne possède pas les champs « emprise » '
                'et « num_bloc ». Lancez d\'abord l\'étape 2 (tri par '
                'emprises), qui calcule aussi la numérotation des photos.')
            return
        # Vue courante du canevas (fond de plan affiché + étendue) :
        # importée dans la carte de l'Atlas pour figer un aperçu propre,
        # indépendant des changements de visibilité ultérieurs.
        canvas = self.iface.mapCanvas() if self.iface else None
        canvas_layers = canvas.layers() if canvas else None
        canvas_extent = canvas.extent() if canvas else None
        canvas_crs = (canvas.mapSettings().destinationCrs()
                     if canvas else None)

        try:
            layout = atlas_mod.create_atlas_layout(
                QgsProject.instance(), poly_layer, field, pt_layer,
                canvas_layers=canvas_layers, canvas_extent=canvas_extent,
                canvas_crs=canvas_crs)
        except Exception as e:
            QMessageBox.critical(self, 'GeoPhoto', str(e))
            return
        self.log(f'Mise en page « {layout.name()} » créée '
                 '(Projet ▸ Mises en page).')
        self.iface.openLayoutDesigner(layout)
