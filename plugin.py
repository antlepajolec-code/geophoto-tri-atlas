# -*- coding: utf-8 -*-
"""
GeoPhoto Tri & Atlas — classe principale du plugin.
Ajoute l'action dans le menu Extensions et la barre d'outils.
"""

import os

from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction


class GeoPhotoTriAtlasPlugin:

    MENU = '&GeoPhoto Tri && Atlas'

    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dlg = None

    def initGui(self):
        icon = QIcon(os.path.join(os.path.dirname(__file__), 'icon.svg'))
        self.action = QAction(icon, 'GeoPhoto – Tri && Atlas…',
                              self.iface.mainWindow())
        self.action.setWhatsThis(
            'Importer des photos géoréférencées, les trier par emprises '
            'et préparer un Atlas.')
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu(self.MENU, self.action)

    def unload(self):
        if self.action:
            self.iface.removePluginMenu(self.MENU, self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None
        if self.dlg:
            self.dlg.close()
            self.dlg = None

    def run(self):
        # Dialogue non modal pour pouvoir sélectionner des entités
        # sur la carte pendant que la fenêtre est ouverte.
        if self.dlg is None:
            from .dialog import GeoPhotoDialog
            self.dlg = GeoPhotoDialog(self.iface, self.iface.mainWindow())
        self.dlg.show()
        self.dlg.raise_()
        self.dlg.activateWindow()
