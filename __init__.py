# -*- coding: utf-8 -*-
"""
GeoPhoto Tri & Atlas — plugin QGIS
Point d'entrée du plugin.
"""


def classFactory(iface):
    from .plugin import GeoPhotoTriAtlasPlugin
    return GeoPhotoTriAtlasPlugin(iface)
