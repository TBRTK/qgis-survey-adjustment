# SPDX-License-Identifier: GPL-3.0-only
"""Create separate QGIS result layers, retaining the actual imported XYZ."""
from qgis.core import (QgsVectorLayer,QgsFeature,QgsGeometry,QgsPoint,QgsLineString,
                       QgsField,QgsCategorizedSymbolRenderer,QgsRendererCategory,
                       QgsMarkerSymbol,QgsPalLayerSettings,QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtCore import QMetaType


def make_layer(kind,name,crs,fields,rows):
    layer = QgsVectorLayer(f'{kind}?crs={crs}',name,'memory')
    layer.dataProvider().addAttributes([QgsField(n,t) for n,t in fields])
    layer.updateFields()
    features = []
    for attrs,geometry in rows:
        feature = QgsFeature(layer.fields())
        feature.setAttributes(attrs)
        feature.setGeometry(geometry)
        features.append(feature)
    if not layer.dataProvider().addFeatures(features)[0]:
        raise ValueError('Kunne ikke opprette resultatlag i QGIS.')
    layer.updateExtents()
    layer.setReadOnly(True)
    layer.setCustomProperty('survey_adjustment/derived',True)
    return layer


def result_layers(report):
    text,number,integer = QMetaType.Type.QString,QMetaType.Type.Double,QMetaType.Type.Int
    point = lambda xyz: QgsGeometry(QgsPoint(*xyz))
    crs = report['options']['crs']
    prefix = 'SYNTESE – ' if report.get('synthetic') else 'Gjentak – '
    obs = make_layer('PointZ',prefix+'målinger (faktisk XYZ)',crs,
        [('punkt',text),('kontroll_id',text),('rad',integer),('rtk',text),('tid',text),('dE_m',number),('dN_m',number),('dZ_m',number),('status',text)],
        [([o['name'],o['group'],o['row'],o['status'],o['time'],*o['delta'],'AVVIK' if o['exceed'] else 'INNENFOR'],point((o['e'],o['n'],o['z']))) for o in report['observations']])
    means = make_layer('PointZ',prefix+'middelverdier (uvektet)',crs,
        [('kontroll_id',text),('antall',integer),('sE_m',number),('sN_m',number),('sZ_m',number),('status',text)],
        [([g['group'],g['count'],*g['sd'],g['status']],point(g['mean'])) for g in report['groups']])
    controls = make_layer('PointZ',prefix+'referansepunkter',crs,[('kontroll_id',text)],
        [([g['group']],point(g['reference'])) for g in report['groups'] if g['reference']])
    vectors = make_layer('LineStringZ',prefix+'restavvik (faktisk lengde)',crs,
        [('punkt',text),('kontroll_id',text),('dE_m',number),('dN_m',number),('dZ_m',number)],
        [([o['name'],o['group'],*o['delta']],QgsGeometry(QgsLineString([QgsPoint(*o['mean']),QgsPoint(o['e'],o['n'],o['z'])]))) for o in report['observations']])
    for layer in (obs,means):
        categories = [QgsRendererCategory(value,QgsMarkerSymbol.createSimple({'name':'circle','color':color,'size':'3'}),value)
                      for value,color in [('INNENFOR','#258454'),('AVVIK','#db4141'),('UAVKLART','#d89423')]]
        layer.setRenderer(QgsCategorizedSymbolRenderer('status',categories))
    labels = QgsPalLayerSettings()
    labels.fieldName = 'kontroll_id'
    means.setLabeling(QgsVectorLayerSimpleLabeling(labels))
    means.setLabelsEnabled(True)
    return [obs,means,controls,vectors]
