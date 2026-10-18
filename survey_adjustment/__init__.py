# SPDX-License-Identifier: GPL-3.0-only
def classFactory(iface):
    from .plugin import SurveyAdjustmentPlugin
    return SurveyAdjustmentPlugin(iface)
