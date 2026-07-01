#!/usr/bin/env python3
"""
Camins Rals - Aplicació de Mapes GPX v7
PyQt5 + QWebEngineView + Leaflet.js + Matplotlib + Playwright

Dependencies:
    pip install PyQt5 PyQtWebEngine gpxpy matplotlib playwright
    playwright install chromium
"""

import sys
import os
import math
import json
import tempfile

import gpxpy
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QScrollArea, QFrame,
    QColorDialog, QDoubleSpinBox, QSplitter, QMessageBox,
    QGroupBox, QStatusBar, QSpinBox, QSizePolicy, QLineEdit,
    QDialog, QComboBox
)
from PyQt5.QtCore import Qt, QUrl, QObject, pyqtSlot, QThread, pyqtSignal, QPointF
from PyQt5.QtGui import (QColor, QFont, QPainter, QPixmap, QPen,
                          QBrush, QPainterPath, QLinearGradient)
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineSettings
from PyQt5.QtWebChannel import QWebChannel

import matplotlib
matplotlib.use('Qt5Agg')
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from playwright.sync_api import sync_playwright

# ============================================================
# CONSTANTS
# ============================================================

ICGC_TILE = (
    'https://geoserveis.icgc.cat/servei/catalunya/mapa-base/wmts/'
    'topografic-gris/MON3857NW/{z}/{x}/{y}.png'
)
ICGC_ATTR = '© <a href="https://www.icgc.cat/">ICGC</a>'
DEFAULT_COLORS = [
    '#1E90FF', '#FF6B35', '#32CD32', '#FF69B4',
    '#FFD700', '#FF4500', '#9B59B6', '#00CED1'
]


# ============================================================
# GPX DATA MODEL
# ============================================================

def _haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(max(0.0, a)))


class GPXData:
    """Parses and holds all data from a single GPX file."""

    def __init__(self, path: str):
        self.path = path
        self.name = os.path.splitext(os.path.basename(path))[0]
        self.tracks = []       # list of [(lat, lon, ele_or_None), ...]
        self.waypoints = []    # list of dicts
        self.total_km = 0.0
        self.ele_gain = 0.0
        self.ele_loss = 0.0
        self.min_ele = None
        self.max_ele = None
        self._parse()

    def _parse(self):
        with open(self.path, 'r', encoding='utf-8') as f:
            gpx = gpxpy.parse(f)

        for track in gpx.tracks:
            for seg in track.segments:
                pts = [(p.latitude, p.longitude, p.elevation)
                       for p in seg.points if p.latitude is not None]
                if pts:
                    self.tracks.append(pts)

        for wpt in gpx.waypoints:
            self.waypoints.append({
                'lat': wpt.latitude,
                'lon': wpt.longitude,
                'name': wpt.name or '',
                'desc': wpt.description or '',
            })

        self._calc_stats()

    def _calc_stats(self):
        eles, total, gain, loss = [], 0.0, 0.0, 0.0
        for track in self.tracks:
            prev = None
            for (lat, lon, ele) in track:
                if prev:
                    total += _haversine(prev[0], prev[1], lat, lon)
                    if ele is not None and prev[2] is not None:
                        d = ele - prev[2]
                        if d > 0:
                            gain += d
                        else:
                            loss += abs(d)
                if ele is not None:
                    eles.append(ele)
                prev = (lat, lon, ele)
        self.total_km = total
        self.ele_gain = gain
        self.ele_loss = loss
        if eles:
            self.min_ele, self.max_ele = min(eles), max(eles)

    def get_elevation_profile(self):
        """Returns (distances_km, elevations_m) lists for plotting."""
        dists, eles, total = [], [], 0.0
        for track in self.tracks:
            prev = None
            for (lat, lon, ele) in track:
                if prev:
                    total += _haversine(prev[0], prev[1], lat, lon)
                dists.append(total)
                eles.append(ele if ele is not None else 0.0)
                prev = (lat, lon, ele)
        return dists, eles

    def all_coords(self):
        return [(lat, lon) for t in self.tracks for (lat, lon, _) in t]


# ============================================================
# JAVASCRIPT ↔ PYTHON BRIDGE
# ============================================================

class MapBridge(QObject):
    """Registered with QWebChannel so JS can call Python."""

    @pyqtSlot(str)
    def log(self, msg: str):
        print(f"[JS] {msg}")


# ============================================================
# MAP HTML BUILDERS
# ============================================================

def _track_and_wpt_json(entries, wpts_visible=True):
    tracks, wpts = [], []
    for i, e in enumerate(entries):
        gpx = e['gpx_data']
        for seg in gpx.tracks:
            coords = [[pt[0], pt[1]] for pt in seg]
            tracks.append({
                'coords': coords,
                'color': e['color'],
                'weight': float(e['thickness']),
                'name': gpx.name,
            })
        if wpts_visible:
            for w in gpx.waypoints:
                wpts.append({
                    'lat': w['lat'], 'lon': w['lon'],
                    'name': w['name'], 'desc': w['desc'],
                })
    return json.dumps(tracks), json.dumps(wpts)


def build_interactive_html(entries):
    """HTML for QWebEngineView — includes QWebChannel bridge."""
    t_json, w_json = _track_and_wpt_json(entries, wpts_visible=True)

    all_coords = []
    for e in entries:
        all_coords.extend(e['gpx_data'].all_coords())

    if all_coords:
        lats = [c[0] for c in all_coords]
        lons = [c[1] for c in all_coords]
        fit_js = (f"map.fitBounds([[{min(lats)},{min(lons)}],"
                  f"[{max(lats)},{max(lons)}]], {{padding:[40,40]}});")
    else:
        fit_js = "map.setView([41.8, 2.7], 9);"

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
<style>
html,body{{margin:0;padding:0;height:100%;}}
#map{{width:100%;height:100%;}}
.wlbl.leaflet-tooltip{{
  background:transparent !important;
  border:none !important;
  box-shadow:none !important;
  padding:0 !important;
  font:bold 11px Arial,sans-serif;
  color:#1a1a1a;
  white-space:nowrap;
  text-shadow:
    1px 1px 0 #fff,-1px -1px 0 #fff,
    1px -1px 0 #fff,-1px 1px 0 #fff,
    0 1px 0 #fff, 0 -1px 0 #fff,
    1px 0 0 #fff,-1px 0 0 #fff;
}}
.wlbl.leaflet-tooltip::before{{ display:none !important; }}
</style>
</head>
<body>
<div id="map"></div>
<script>
// ---- MAP INIT ----
var map = L.map('map', {{zoomControl:true, preferCanvas:true}});
L.tileLayer('{ICGC_TILE}', {{attribution:'{ICGC_ATTR}', maxZoom:20}}).addTo(map);

// ---- TRACKS ----
var TRACKS = {t_json};
var allCoords = [];
TRACKS.forEach(function(t){{
  L.polyline(t.coords, {{color:'#000', weight:t.weight+2, opacity:0.55}}).addTo(map);
  L.polyline(t.coords, {{color:t.color, weight:t.weight, opacity:1.0}})
    .bindTooltip(t.name, {{sticky:true}}).addTo(map);
  allCoords = allCoords.concat(t.coords);
}});
{fit_js}

// ---- WAYPOINTS ----
var WPTS = {w_json};
var wptGroup = L.layerGroup().addTo(map);
var wIcon = L.divIcon({{
  html:'<div style="background:#E74C3C;width:12px;height:12px;border-radius:50%;'
      +'border:2px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,0.5);"></div>',
  className:'', iconSize:[16,16], iconAnchor:[8,8]
}});
WPTS.forEach(function(w){{
  var m = L.marker([w.lat,w.lon],{{icon:wIcon}});
  var pop = '<b>'+(w.name||'POI')+'</b>';
  if(w.desc) pop += '<br><small>'+w.desc+'</small>';
  m.bindPopup(pop);
  if(w.name) m.bindTooltip(w.name,{{
    permanent:true, direction:'top', offset:[0,-10], className:'wlbl'
  }});
  wptGroup.addLayer(m);
}});

// ---- PUBLIC API (called from Python via runJavaScript) ----
window.toggleWaypoints = function(show){{
  if(show) wptGroup.addTo(map); else map.removeLayer(wptGroup);
}};
window.fitAll = function(){{
  if(allCoords.length>0) map.fitBounds(L.latLngBounds(allCoords),{{padding:[40,40]}});
}};
window.getMapState = function(){{
  var b=map.getBounds(), c=map.getCenter();
  return JSON.stringify({{
    n:b.getNorth(), s:b.getSouth(), e:b.getEast(), w:b.getWest(),
    zoom:map.getZoom(), cx:c.lat, cy:c.lng
  }});
}};
window.setView = function(lat,lng,zoom){{ map.setView([lat,lng],zoom); }};

// ---- WEBCHANNEL ----
new QWebChannel(qt.webChannelTransport, function(ch){{
  window.pyBridge = ch.objects.bridge;
}});
</script>
</body>
</html>"""


def build_empty_html():
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
<style>html,body{{margin:0;height:100%;}}#map{{width:100%;height:100%;}}</style>
</head><body><div id="map"></div>
<script>
var map=L.map('map').setView([41.8,2.7],9);
L.tileLayer('{ICGC_TILE}',{{attribution:'{ICGC_ATTR}',maxZoom:20}}).addTo(map);
window.getMapState=function(){{
  var b=map.getBounds(),c=map.getCenter();
  return JSON.stringify({{n:b.getNorth(),s:b.getSouth(),e:b.getEast(),w:b.getWest(),
    zoom:map.getZoom(),cx:c.lat,cy:c.lng}});
}};
window.fitAll=function(){{}};
window.toggleWaypoints=function(){{}};
new QWebChannel(qt.webChannelTransport,function(ch){{window.pyBridge=ch.objects.bridge;}});
</script></body></html>"""


def build_export_html(entries, wpts_visible, cx, cy, zoom=None,
                       fit_bounds=None, corner="BL"):
    """Standalone HTML for Playwright.
    corner: 'BL' bottom-left | 'BR' bottom-right | 'TR' top-right | 'TL' top-left
    """
    t_json, w_json = _track_and_wpt_json(entries, wpts_visible)

    if fit_bounds:
        init_js = f"map.fitBounds({json.dumps(fit_bounds)}, {{padding:[30,30]}});"
    elif zoom is not None:
        init_js = f"map.setView([{cx},{cy}],{zoom});"
    else:
        init_js = f"map.setView([{cx},{cy}],12);"

    # CSS positions for the indicator panel and attribution
    _panel = {"BL": "bottom:28px;left:28px",
              "BR": "bottom:28px;right:28px",
              "TR": "top:28px;right:28px",
              "TL": "top:28px;left:28px"}.get(corner, "bottom:28px;left:28px")
    _attr  = {"BL": "bottom:28px;right:28px",
              "BR": "bottom:28px;left:28px",
              "TR": "bottom:28px;left:28px",
              "TL": "bottom:28px;right:28px"}.get(corner, "bottom:28px;right:28px")
    # Flex alignment (arrow+scalebar stack): left-anchored except right corners
    _align = "flex-end" if corner in ("BR", "TR") else "flex-start"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
html,body{{margin:0;padding:0;height:100%;width:100%;}}
#map{{width:100%;height:100vh;}}
.wlbl.leaflet-tooltip{{
  background:transparent !important; border:none !important;
  box-shadow:none !important; padding:0 !important;
  font:bold 11px Arial,sans-serif; color:#1a1a1a; white-space:nowrap;
  text-shadow:1px 1px 0 #fff,-1px -1px 0 #fff,1px -1px 0 #fff,-1px 1px 0 #fff,
              0 1px 0 #fff,0 -1px 0 #fff,1px 0 0 #fff,-1px 0 0 #fff;
}}
.wlbl.leaflet-tooltip::before{{display:none !important;}}
</style></head><body><div id="map"></div>
<script>
var map = L.map('map', {{zoomControl:false, attributionControl:false,
                         preferCanvas:true}});
L.tileLayer('{ICGC_TILE}', {{maxZoom:20}}).addTo(map);
{init_js}

var TRACKS = {t_json};
TRACKS.forEach(function(t) {{
  L.polyline(t.coords, {{color:'#000', weight:t.weight+2, opacity:0.55}}).addTo(map);
  L.polyline(t.coords, {{color:t.color, weight:t.weight, opacity:1.0}}).addTo(map);
}});
var WPTS = {w_json};
var wIcon = L.divIcon({{
  html: '<div style="background:#E74C3C;width:12px;height:12px;border-radius:50%;'
      + 'border:2px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,0.5);"></div>',
  className:'', iconSize:[16,16], iconAnchor:[8,8]
}});
WPTS.forEach(function(w) {{
  var m = L.marker([w.lat, w.lon], {{icon:wIcon}}).addTo(map);
  if (w.name) m.bindTooltip(w.name, {{permanent:true, direction:'top',
                                       offset:[0,-10], className:'wlbl'}});
}});

// ── Cartographic overlay ─────────────────────────────────────────────────────
function _calcScaleBar() {{
  var bounds  = map.getBounds();
  var W       = map.getSize().x;
  var nw      = bounds.getNorthWest();
  var ne      = bounds.getNorthEast();
  var totalM  = nw.distanceTo(ne);
  if (totalM <= 0) return {{px:100, label:'1 km'}};
  var pxPerM  = W / totalM;
  var targetM = (W * 0.14) / pxPerM;
  var nice    = [100,200,500,1000,2000,5000,10000,20000,50000,100000];
  var barM    = nice[0];
  for (var i = 0; i < nice.length; i++) {{
    barM = nice[i];
    if (nice[i] >= targetM * 0.6) break;
  }}
  var barPx = Math.round(barM * pxPerM);
  var lbl   = barM >= 1000 ? (barM/1000) + ' km' : barM + ' m';
  return {{px: barPx, label: lbl}};
}}

function _addCartographicElements() {{
  var sc   = _calcScaleBar();
  var half = Math.round(sc.px / 2);
  var mapEl = document.getElementById('map');

  // ── North arrow + scale bar (chosen corner) ────────────────────────────────
  var panel = document.createElement('div');
  panel.style.cssText = 'position:absolute;{_panel};z-index:900;'
    + 'display:flex;flex-direction:column;align-items:{_align};gap:8px;'
    + 'pointer-events:none;';

  // North arrow
  var arrow = document.createElement('div');
  arrow.style.cssText = 'background:rgba(255,255,255,0.93);border-radius:8px;'
    + 'padding:8px 12px;box-shadow:0 2px 8px rgba(0,0,0,0.22);text-align:center;';
  arrow.innerHTML =
    '<svg width="26" height="34" viewBox="0 0 26 34">'
    + '<polygon points="13,3 22,28 13,21" fill="#C5221F"/>'
    + '<polygon points="13,3 4,28 13,21" fill="#ffffff" stroke="#aaa" stroke-width="0.8"/>'
    + '<circle cx="13" cy="21" r="3.2" fill="#444"/>'
    + '</svg>'
    + '<div style="font-family:Arial,sans-serif;font-size:13px;font-weight:bold;'
    + 'color:#222;letter-spacing:2px;margin-top:3px;">N</div>';

  // Scale bar
  var scalebar = document.createElement('div');
  scalebar.style.cssText = 'background:rgba(255,255,255,0.93);border-radius:8px;'
    + 'padding:8px 12px;box-shadow:0 2px 8px rgba(0,0,0,0.22);';
  var barHtml =
    '<div style="display:flex;border:1.5px solid #333;margin-bottom:4px;">'
    + '<div style="width:' + half + 'px;height:10px;background:#333;"></div>'
    + '<div style="width:' + half + 'px;height:10px;background:#fff;"></div>'
    + '</div>'
    + '<div style="font-family:Arial,sans-serif;display:flex;'
    + 'justify-content:space-between;width:' + sc.px + 'px;'
    + 'font-size:11px;color:#333;">'
    + '<span>0</span><span>' + sc.label + '</span></div>';
  scalebar.innerHTML = barHtml;

  panel.appendChild(arrow);
  panel.appendChild(scalebar);
  mapEl.appendChild(panel);

  // ── ICGC attribution (opposite corner) ────────────────────────────────────
  var attr = document.createElement('div');
  attr.style.cssText = 'position:absolute;{_attr};z-index:900;pointer-events:none;';
  var attrBox = document.createElement('div');
  attrBox.style.cssText = 'background:rgba(255,255,255,0.88);border-radius:5px;'
    + 'padding:4px 8px;font-family:Arial,sans-serif;font-size:9px;color:#555;'
    + 'box-shadow:0 1px 4px rgba(0,0,0,0.15);line-height:1.4;';
  attrBox.innerHTML = 'Cartografia base: &copy; Institut Cartogr&agrave;fic '
    + 'i Geol&ograve;gic de Catalunya (ICGC)';
  attr.appendChild(attrBox);
  mapEl.appendChild(attr);
}}

// Give fitBounds/setView and first tile batch time to settle
setTimeout(_addCartographicElements, 700);
</script></body></html>"""


# ============================================================
# ELEVATION PROFILE WIDGET
# ============================================================

class ElevationCanvas(FigureCanvas):
    _FIG_BG  = "#FFFFFF"
    _AXES_BG = "#FAFAFA"
    _TEXT    = "#5F6368"
    _GRID    = "#E8EAED"

    def __init__(self, parent=None):
        fig = Figure(figsize=(10, 2.2), dpi=80, facecolor=self._FIG_BG)
        fig.subplots_adjust(left=0.07, right=0.98, top=0.90, bottom=0.28)
        self.ax = fig.add_subplot(111, facecolor=self._AXES_BG)
        super().__init__(fig)
        self.setParent(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._draw_empty()

    def _style_axes(self):
        self.ax.set_facecolor(self._AXES_BG)
        self.figure.patch.set_facecolor(self._FIG_BG)
        for spine in self.ax.spines.values():
            spine.set_edgecolor(self._GRID)
        self.ax.tick_params(colors=self._TEXT, labelsize=7)
        self.ax.xaxis.label.set_color(self._TEXT)
        self.ax.yaxis.label.set_color(self._TEXT)

    def _draw_empty(self):
        self.ax.clear()
        self._style_axes()
        self.ax.text(0.5, 0.5,
                     "Carrega rutes GPX per veure el perfil d'elevació",
                     ha='center', va='center',
                     transform=self.ax.transAxes,
                     fontsize=10, color=self._TEXT, style='italic')
        self.ax.set_xlabel('Distància (km)', fontsize=8)
        self.ax.set_ylabel('Altitud (m)', fontsize=8)
        self.figure.canvas.draw()

    def update_profile(self, entries):
        self.ax.clear()
        self._style_axes()
        has_data = False
        for e in entries:
            dists, eles = e['gpx_data'].get_elevation_profile()
            if dists and any(el != 0.0 for el in eles):
                self.ax.fill_between(dists, eles, alpha=0.18, color=e['color'])
                self.ax.plot(dists, eles, color=e['color'],
                             linewidth=1.8, label=e['gpx_data'].name[:26])
                has_data = True
        if has_data:
            leg = self.ax.legend(fontsize=7, loc='upper right',
                                 facecolor=self._FIG_BG, edgecolor=self._GRID,
                                 labelcolor=self._TEXT)
        else:
            self.ax.text(0.5, 0.5, "Sense dades d'elevació",
                         ha='center', va='center',
                         transform=self.ax.transAxes,
                         fontsize=10, color=self._TEXT, style='italic')
        self.ax.set_xlabel('Distància (km)', fontsize=8)
        self.ax.set_ylabel('Altitud (m)', fontsize=8)
        self.ax.grid(True, color=self._GRID, linewidth=0.6)
        self.figure.canvas.draw()


# ============================================================
# FILE ROW WIDGET
# ============================================================

class FileRowWidget(QFrame):
    removed = pyqtSignal(object)
    changed = pyqtSignal()

    def __init__(self, gpx_data: GPXData, color: str, parent=None):
        super().__init__(parent)
        self.gpx_data = gpx_data
        self._color = color
        self.setFrameStyle(QFrame.NoFrame)
        self.setMaximumHeight(68)
        self._apply_card_style()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 5, 8, 5)
        outer.setSpacing(3)

        # Row 1: color dot · name · delete
        r1 = QHBoxLayout()
        r1.setSpacing(6)

        self._color_btn = QPushButton()
        self._color_btn.setFixedSize(18, 18)
        self._color_btn.setStyleSheet(
            f"background:{color};border-radius:9px;border:2px solid #E0E0E0;")
        self._color_btn.setToolTip("Fes clic per canviar el color")
        self._color_btn.setCursor(Qt.PointingHandCursor)
        self._color_btn.clicked.connect(self._pick_color)

        name_lbl = QLabel(gpx_data.name[:36])
        name_lbl.setFont(QFont("Arial", 10, QFont.Bold))
        name_lbl.setStyleSheet(f"color:{P_TEXT1};background:transparent;")
        name_lbl.setToolTip(gpx_data.name)

        del_btn = QPushButton("✕")
        del_btn.setFixedSize(22, 22)
        del_btn.setStyleSheet(
            f"color:{P_TEXT3};font-size:11px;font-weight:bold;"
            f"border:none;background:transparent;border-radius:11px;")
        del_btn.setToolTip("Elimina aquesta ruta")
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.clicked.connect(lambda: self.removed.emit(self))

        r1.addWidget(self._color_btn)
        r1.addWidget(name_lbl, stretch=1)
        r1.addWidget(del_btn)

        # Row 2: thickness spinbox · distance/ele stats
        r2 = QHBoxLayout()
        r2.setSpacing(4)

        lbl_g = QLabel("Gruix")
        lbl_g.setFont(QFont("Arial", 8))
        lbl_g.setStyleSheet(f"color:{P_TEXT2};background:transparent;")

        self.thick_spin = QDoubleSpinBox()
        self.thick_spin.setRange(0.5, 20.0)
        self.thick_spin.setSingleStep(0.5)
        self.thick_spin.setValue(3.0)
        self.thick_spin.setFixedWidth(62)
        self.thick_spin.setFixedHeight(22)
        self.thick_spin.setToolTip("Gruix de la línia al mapa")
        self.thick_spin.valueChanged.connect(self.changed.emit)

        km   = gpx_data.total_km
        gain = gpx_data.ele_gain
        loss = gpx_data.ele_loss
        stats_lbl = QLabel(f"  {km:.1f} km  ↑{gain:.0f}  ↓{loss:.0f} m")
        stats_lbl.setFont(QFont("Arial", 8))
        stats_lbl.setStyleSheet(f"color:{P_BLUE};background:transparent;")

        r2.addWidget(lbl_g)
        r2.addWidget(self.thick_spin)
        r2.addStretch()
        r2.addWidget(stats_lbl)

        outer.addLayout(r1)
        outer.addLayout(r2)

    def _apply_card_style(self):
        self.setStyleSheet(
            f"QFrame{{background:{P_SURFACE};"
            f"border-left:4px solid {self._color};"
            f"border-top:1px solid {P_DIVIDER};"
            f"border-right:1px solid {P_DIVIDER};"
            f"border-bottom:1px solid {P_DIVIDER};"
            f"border-radius:8px;}}"
        )

    @property
    def color(self):
        return self._color

    @property
    def thickness(self):
        return self.thick_spin.value()

    def _pick_color(self):
        c = QColorDialog.getColor(QColor(self._color), self, "Tria color del camí")
        if c.isValid():
            self._color = c.name()
            self._color_btn.setStyleSheet(
                f"background:{self._color};border-radius:9px;border:2px solid #E0E0E0;")
            self._apply_card_style()
            self.changed.emit()


# ============================================================
# BACKGROUND EXPORT THREAD
# ============================================================

class ExportWorker(QThread):
    finished = pyqtSignal(str)
    error = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, html: str, path: str, width: int, height: int, scale: int):
        super().__init__()
        self.html = html
        self.path = path
        self.width = width
        self.height = height
        self.scale = scale

    def run(self):
        tmp = None
        try:
            tmp = tempfile.NamedTemporaryFile(
                suffix='.html', delete=False, mode='w', encoding='utf-8')
            tmp.write(self.html)
            tmp.close()

            self.status.emit("Obrint navegador en segon pla…")
            with sync_playwright() as p:
                browser = p.chromium.launch()
                ctx = browser.new_context(
                    viewport={"width": self.width, "height": self.height},
                    device_scale_factor=self.scale
                )
                page = ctx.new_page()
                page.goto(f"file://{tmp.name}")
                self.status.emit("Esperant que carreguin les tessel·les (10 s)…")
                page.wait_for_timeout(10000)
                page.screenshot(path=self.path)
                browser.close()

            self.finished.emit(self.path)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            if tmp and os.path.exists(tmp.name):
                os.unlink(tmp.name)


# ============================================================
# SEGMENT EXPORT — helper & background worker
# ============================================================

def _zoom_for_bounds(lat_min, lat_max, lon_min, lon_max,
                     width_px, height_px, padding=0.85):
    """Leaflet zoom level that fits the bounding box in the given pixel size."""
    lat_span = lat_max - lat_min or 0.001
    lon_span = lon_max - lon_min or 0.001
    clat = math.radians((lat_min + lat_max) / 2)
    TILE = 256
    z_lon = math.log2(360 * width_px * padding / (TILE * lon_span))
    z_lat = math.log2(360 * math.cos(clat) * height_px * padding / (TILE * lat_span))
    return max(1, min(18, int(min(z_lon, z_lat))))


def _split_by_distance(all_pts, n, overlap_frac):
    """
    Divide points into n segments of equal cumulative distance with overlap.
    Returns list of point-lists.
    """
    if len(all_pts) < 2:
        return [all_pts] * n

    # build cumulative distance array
    cum = [0.0]
    for i in range(1, len(all_pts)):
        cum.append(cum[-1] + _haversine(
            all_pts[i-1][0], all_pts[i-1][1],
            all_pts[i][0],   all_pts[i][1]))
    total = cum[-1]
    seg_d = total / n
    overlap_d = seg_d * overlap_frac

    def idx_at(target_d):
        for j, d in enumerate(cum):
            if d >= target_d:
                return j
        return len(all_pts) - 1

    segments = []
    for i in range(n):
        s = max(0.0, i * seg_d - overlap_d)
        e = min(total, (i + 1) * seg_d + overlap_d)
        segments.append(all_pts[idx_at(s): idx_at(e) + 1])
    return segments


class SegmentWorker(QThread):
    progress = pyqtSignal(int, int)   # (current, total)
    done     = pyqtSignal(str)        # save_dir
    error    = pyqtSignal(str)

    def __init__(self, jobs, export_scale):
        """
        jobs: list of dict {html, path, width, height}
        """
        super().__init__()
        self.jobs = jobs
        self.export_scale = export_scale

    def run(self):
        n = len(self.jobs)
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                for i, job in enumerate(self.jobs):
                    self.progress.emit(i + 1, n)
                    tmp = tempfile.NamedTemporaryFile(
                        suffix='.html', delete=False, mode='w', encoding='utf-8')
                    tmp.write(job['html'])
                    tmp.close()
                    ctx = browser.new_context(
                        viewport={"width": job['width'], "height": job['height']},
                        device_scale_factor=self.export_scale
                    )
                    page = ctx.new_page()
                    page.goto(f"file://{tmp.name}")
                    page.wait_for_timeout(10000)
                    page.screenshot(path=job['path'])
                    ctx.close()
                    os.unlink(tmp.name)
                browser.close()
            self.done.emit(os.path.dirname(self.jobs[0]['path']))
        except Exception as exc:
            self.error.emit(str(exc))


# ============================================================
# SEGMENT EXPORT DIALOG
# ============================================================

class SegmentDialog(QDialog):
    def __init__(self, file_rows, export_width, export_scale,
                 corner="BL", parent=None):
        super().__init__(parent)
        self.file_rows = file_rows
        self.export_width = export_width
        self.export_scale = export_scale
        self.corner = corner
        self._worker = None
        self.setWindowTitle("Segmentar Ruta — Exportar en Parts")
        self.setMinimumWidth(480)
        self.setStyleSheet(
            f"QDialog{{background:{P_SURFACE};color:{P_TEXT1};}}"
            f"QLabel{{color:{P_TEXT1};background:transparent;}}"
            f"QGroupBox{{background:{P_SURF2};border:1px solid {P_BORDER};"
            f"border-radius:8px;margin-top:10px;padding-top:8px;}}"
            f"QGroupBox::title{{color:{P_TEXT2};subcontrol-origin:margin;"
            f"left:10px;font-size:9px;font-weight:bold;letter-spacing:1px;}}"
            f"QSpinBox,QLineEdit{{background:{P_SURFACE};color:{P_TEXT1};"
            f"border:1px solid {P_BORDER};border-radius:6px;padding:4px 8px;}}"
            f"QSpinBox:focus,QLineEdit:focus{{border-color:{P_BLUE};}}"
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        info = QLabel(
            "Divideix la primera ruta en N parts d'igual distància i exporta\n"
            "cada part com a imatge separada. Totes les imatges s'obtenen\n"
            "al mateix zoom perquè siguin comparables al llibre."
        )
        info.setWordWrap(True)
        info.setStyleSheet(
            f"color:{P_TEXT2};padding:10px 12px;font-size:11px;"
            f"background:{P_BLUE_L};border-radius:8px;"
            f"border:1px solid #C5D8F8;")
        layout.addWidget(info)

        form = QGroupBox("Configuració")
        fl = QVBoxLayout(form)
        fl.setSpacing(10)

        r1 = QHBoxLayout()
        lbl1 = QLabel("Nombre de parts:")
        r1.addWidget(lbl1)
        self.n_spin = QSpinBox()
        self.n_spin.setRange(2, 20)
        self.n_spin.setValue(3)
        r1.addWidget(self.n_spin)
        r1.addStretch()
        fl.addLayout(r1)

        r2 = QHBoxLayout()
        lbl2 = QLabel("Solap entre parts (%):")
        r2.addWidget(lbl2)
        self.overlap_spin = QSpinBox()
        self.overlap_spin.setRange(0, 40)
        self.overlap_spin.setValue(10)
        r2.addWidget(self.overlap_spin)
        r2.addStretch()
        fl.addLayout(r2)

        r3 = QHBoxLayout()
        lbl3 = QLabel("Nom base dels fitxers:")
        r3.addWidget(lbl3)
        self.prefix_edit = QLineEdit("cami_part")
        r3.addWidget(self.prefix_edit)
        fl.addLayout(r3)

        layout.addWidget(form)

        self.log_lbl = QLabel("Preparat per exportar.")
        self.log_lbl.setWordWrap(True)
        self.log_lbl.setStyleSheet(
            f"color:{P_TEXT2};padding:8px 10px;font-size:10px;"
            f"background:{P_SURF2};border-radius:6px;"
            f"border:1px solid {P_BORDER};")
        layout.addWidget(self.log_lbl)

        btns = QHBoxLayout()
        btns.setSpacing(10)
        self.cancel_btn = QPushButton("Cancel·la")
        self.cancel_btn.setStyleSheet(_BTN_GHOST)
        self.cancel_btn.setCursor(Qt.PointingHandCursor)
        self.cancel_btn.clicked.connect(self.reject)
        self.go_btn = QPushButton("▶   Exporta tots els segments")
        self.go_btn.setMinimumHeight(42)
        self.go_btn.setStyleSheet(_BTN_SEGMENT)
        self.go_btn.setCursor(Qt.PointingHandCursor)
        self.go_btn.clicked.connect(self._start)
        btns.addWidget(self.cancel_btn)
        btns.addWidget(self.go_btn, stretch=1)
        layout.addLayout(btns)

    def _start(self):
        n = self.n_spin.value()
        overlap = self.overlap_spin.value() / 100.0
        prefix = self.prefix_edit.text().strip() or "cami_part"

        save_dir = QFileDialog.getExistingDirectory(
            self, "Tria carpeta on guardar els segments")
        if not save_dir:
            return

        first_gpx = self.file_rows[0].gpx_data
        all_pts = [pt for track in first_gpx.tracks for pt in track]
        if not all_pts:
            QMessageBox.warning(self, "Error", "La primera ruta no té punts de track.")
            return

        segments = _split_by_distance(all_pts, n, overlap)
        entries = [
            {'gpx_data': r.gpx_data, 'color': r.color, 'thickness': r.thickness}
            for r in self.file_rows
        ]
        width = self.export_width

        # --- Compute one consistent zoom for ALL segments ---
        seg_boxes = []
        for seg in segments:
            lats = [p[0] for p in seg]
            lons = [p[1] for p in seg]
            if lats:
                seg_boxes.append((min(lats), max(lats), min(lons), max(lons)))

        # aspect ratio: use the first segment's bbox to set image height
        b0 = seg_boxes[0]
        lat_h0 = b0[1] - b0[0] or 0.001
        lon_w0 = (b0[3] - b0[2]) * math.cos(math.radians((b0[0]+b0[1])/2)) or 0.001
        aspect0 = lon_w0 / lat_h0
        height = max(300, int(width / aspect0))

        # minimum zoom across all segments (most zoomed-out wins → same scale)
        min_zoom = min(
            _zoom_for_bounds(b[0], b[1], b[2], b[3], width, height)
            for b in seg_boxes
        )

        jobs = []
        for i, (seg, bbox) in enumerate(zip(segments, seg_boxes)):
            cx = (bbox[0] + bbox[1]) / 2
            cy = (bbox[2] + bbox[3]) / 2
            html = build_export_html(
                entries, wpts_visible=True,
                cx=cx, cy=cy, zoom=min_zoom,
                corner=self.corner)
            jobs.append({
                'html': html,
                'path': os.path.join(save_dir, f"{prefix}_{i + 1:02d}.png"),
                'width': width,
                'height': height,
            })

        self.go_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.log_lbl.setStyleSheet("color:#1a5a8a;font-weight:bold;padding:4px;")
        self.log_lbl.setText(f"Preparant {n} segments (zoom {min_zoom})…")

        self._worker = SegmentWorker(jobs, self.export_scale)
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(self._on_done)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_progress(self, current, total):
        self.log_lbl.setText(
            f"Exportant segment {current}/{total}  "
            f"(~{(total - current) * 11} s restants)…")

    def _on_done(self, save_dir):
        self.log_lbl.setStyleSheet(
            f"color:{P_GREEN};font-weight:bold;padding:8px 10px;"
            f"background:{P_GREEN_L};border-radius:6px;border:1px solid #A8D5B5;")
        self.log_lbl.setText(f"✓  Tots els segments guardats a:\n{save_dir}")
        QMessageBox.information(self, "Fet!",
                                f"Segments guardats a:\n{save_dir}")
        self.accept()

    def _on_error(self, msg):
        self.go_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        self.log_lbl.setStyleSheet(
            f"color:{P_RED};font-weight:bold;padding:8px 10px;"
            f"background:{P_RED_L};border-radius:6px;border:1px solid #F5C6C5;")
        self.log_lbl.setText(f"Error: {msg}")
        QMessageBox.critical(self, "Error d'exportació", msg)


# ============================================================
# LIGHT THEME — clean, professional (Google / Apple inspired)
# ============================================================

# Palette
P_BG       = "#F5F7FA"   # app window background
P_SURFACE  = "#FFFFFF"   # sidebar, cards
P_SURF2    = "#F1F3F4"   # hovered / slightly elevated
P_DIVIDER  = "#E8EAED"   # section dividers
P_BORDER   = "#DADCE0"   # input & card borders
P_TEXT1    = "#202124"   # primary text
P_TEXT2    = "#5F6368"   # secondary / captions
P_TEXT3    = "#BDC1C6"   # disabled / placeholder
P_BLUE     = "#1A73E8"   # primary CTA
P_BLUE_D   = "#1557B0"   # blue hover/pressed
P_BLUE_L   = "#E8F0FE"   # blue tint background
P_RED      = "#C5221F"   # Catalan crimson — brand accent
P_RED_D    = "#A50E0E"
P_RED_L    = "#FDE8E8"
P_GREEN    = "#137333"   # success / export
P_GREEN_D  = "#0D5226"
P_GREEN_L  = "#E6F4EA"
P_ORANGE   = "#E37400"   # POI toggle active
P_ORANGE_L = "#FEF7E0"
P_TEAL     = "#007B83"   # segment export
P_TEAL_L   = "#E0F5F6"

# Map old dark-theme names so internal uses still resolve correctly
C_BG_DEEP   = P_BG
C_BG_DARK   = P_SURF2
C_BG_CARD   = P_SURFACE
C_BG_LIFT   = P_SURF2
C_GOLD      = P_ORANGE
C_GOLD_DIM  = P_TEXT2
C_GOLD_PALE = P_TEXT1
C_CRIMSON   = P_RED
C_CRIMSON_H = P_RED
C_CRIMSON_D = P_RED_D
C_PARCH     = P_TEXT1
C_PARCH_DIM = P_TEXT2
C_BORDER    = P_BORDER
C_BORDER_LT = P_DIVIDER
C_MAP_FRAME = P_BORDER


# ---- Button style factory -----------------------------------------------
def _btn(bg, bg_h, fg, border="transparent", size=11, bold=True,
         radius=8, pad="7px 14px"):
    bw = "bold" if bold else "500"
    bd = f"1px solid {border}" if border != "transparent" else "none"
    return (
        f"QPushButton{{background:{bg};color:{fg};border:{bd};"
        f"border-radius:{radius}px;font-size:{size}px;"
        f"font-weight:{bw};padding:{pad};}}"
        f"QPushButton:hover{{background:{bg_h};}}"
        f"QPushButton:pressed{{background:{bg_h};}}"
        f"QPushButton:disabled{{background:#F1F3F4;color:{P_TEXT3};"
        f"border:1px solid {P_BORDER};}}"
    )


# Button presets
_BTN_PRIMARY = _btn(P_BLUE,   P_BLUE_D,   "white",   size=12, radius=8, pad="9px 16px")
_BTN_RED     = _btn(P_RED,    P_RED_D,    "white",   size=11)
_BTN_GOLD    = _btn(P_GREEN,  P_GREEN_D,  "white",   size=11)   # "full route" export
_BTN_EXPORT  = _btn(P_GREEN,  P_GREEN_D,  "white",   size=11, pad="8px 14px")
_BTN_SEGMENT = _btn(P_TEAL,   "#005B55",  "white",   size=11)
_BTN_AMBER   = _btn(P_ORANGE, "#C56400",  "white",   size=10)
_BTN_DARK    = (
    f"QPushButton{{background:{P_SURFACE};color:{P_BLUE};"
    f"border:1px solid {P_BORDER};border-radius:8px;"
    f"font-size:10px;padding:6px 12px;}}"
    f"QPushButton:hover{{background:{P_BLUE_L};border-color:{P_BLUE};}}"
    f"QPushButton:pressed{{background:#D4E3FC;}}"
    f"QPushButton:disabled{{color:{P_TEXT3};border-color:{P_BORDER};}}"
)
_BTN_GHOST = (
    f"QPushButton{{background:transparent;color:{P_TEXT2};"
    f"border:1px solid {P_BORDER};border-radius:6px;"
    f"font-size:10px;padding:4px 10px;}}"
    f"QPushButton:hover{{background:{P_SURF2};color:{P_TEXT1};}}"
)

# Backward-compat aliases
_BTN_GREEN  = _BTN_EXPORT
_BTN_PURPLE = _BTN_EXPORT
_BTN_BLUE   = _BTN_DARK
_BTN_TEAL   = _BTN_SEGMENT


# ---- App logo pixmap (drawn via QPainter, no external file needed) -------
def _make_logo_pixmap(size: int = 48) -> QPixmap:
    """Return a QPixmap with the Camins Rals app icon."""
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)

    # Gradient round rectangle — Catalan red
    grad = QLinearGradient(0, 0, 0, size)
    grad.setColorAt(0.0, QColor("#D4231F"))
    grad.setColorAt(1.0, QColor("#8A0D10"))
    p.setBrush(QBrush(grad))
    p.setPen(Qt.NoPen)
    r = size * 0.22
    p.drawRoundedRect(0, 0, size, size, r, r)

    # Route path — white polyline
    pen = QPen(QColor("white"), max(2, size // 10),
               Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    pts = [
        QPointF(size * 0.16, size * 0.72),
        QPointF(size * 0.38, size * 0.40),
        QPointF(size * 0.60, size * 0.58),
        QPointF(size * 0.84, size * 0.25),
    ]
    path = QPainterPath()
    path.moveTo(pts[0])
    for pt in pts[1:]:
        path.lineTo(pt)
    p.drawPath(path)

    # Waypoint dots — white circles at start/end
    dot_r = max(3, size // 9)
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(QColor("white")))
    p.drawEllipse(pts[0], dot_r, dot_r)
    p.drawEllipse(pts[-1], dot_r, dot_r)

    p.end()
    return pix


# ============================================================
# MAIN WINDOW
# ============================================================


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Camins Rals — Mapes GPX per al Llibre")
        self.resize(1440, 920)

        self.file_rows: list[FileRowWidget] = []
        self._wpts_visible = True
        self._tmp_html = None
        self._export_worker = None

        self._setup_ui()
        self._setup_webchannel()
        self._reload_map()

    # ----------------------------------------------------------
    # UI CONSTRUCTION
    # ----------------------------------------------------------

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # LEFT panel
        left = self._build_left_panel()
        left.setFixedWidth(320)
        root.addWidget(left)

        # RIGHT: map + elevation profile
        right_split = QSplitter(Qt.Vertical)

        map_frame = QFrame()
        map_frame.setStyleSheet(
            f"QFrame{{border:1px solid {P_BORDER};border-radius:6px;"
            f"background:{P_SURFACE};}}")
        mfl = QVBoxLayout(map_frame)
        mfl.setContentsMargins(2, 2, 2, 2)
        self.map_view = QWebEngineView()
        self.map_view.setMinimumHeight(420)
        s = self.map_view.settings()
        s.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
        s.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        mfl.addWidget(self.map_view)
        right_split.addWidget(map_frame)

        # Elevation container
        elev_box = QWidget()
        elev_box.setMaximumHeight(270)
        elev_box.setStyleSheet(
            f"background:{P_SURFACE};"
            f"border-top:1px solid {P_DIVIDER};")
        ev = QVBoxLayout(elev_box)
        ev.setContentsMargins(8, 6, 8, 4)
        ev.setSpacing(4)

        eh = QHBoxLayout()
        elev_title = QLabel("Perfil d'Elevació")
        elev_title.setFont(QFont("Arial", 10, QFont.Bold))
        elev_title.setStyleSheet(f"color:{P_TEXT1};background:transparent;")

        save_elev_btn = QPushButton("Exporta Perfil")
        save_elev_btn.setFixedHeight(28)
        save_elev_btn.setStyleSheet(
            f"QPushButton{{background:{P_SURFACE};color:{P_BLUE};"
            f"border:1px solid {P_BORDER};border-radius:6px;"
            f"font-size:10px;padding:2px 10px;}}"
            f"QPushButton:hover{{background:{P_BLUE_L};border-color:{P_BLUE};}}"
        )
        save_elev_btn.setToolTip("Exporta el perfil com a PNG 300 DPI")
        save_elev_btn.clicked.connect(self._save_elevation_profile)

        self._toggle_elev_btn = QPushButton("▲")
        self._toggle_elev_btn.setFixedSize(28, 28)
        self._toggle_elev_btn.setStyleSheet(
            f"QPushButton{{background:{P_SURF2};color:{P_TEXT2};"
            f"border:1px solid {P_BORDER};border-radius:6px;font-size:11px;}}"
            f"QPushButton:hover{{background:{P_DIVIDER};color:{P_TEXT1};}}"
        )
        self._toggle_elev_btn.clicked.connect(self._toggle_elevation)
        eh.addWidget(elev_title)
        eh.addStretch()
        eh.addWidget(save_elev_btn)
        eh.addSpacing(4)
        eh.addWidget(self._toggle_elev_btn)

        self.elev_canvas = ElevationCanvas()
        ev.addLayout(eh)
        ev.addWidget(self.elev_canvas)
        right_split.addWidget(elev_box)

        right_split.setStretchFactor(0, 4)
        right_split.setStretchFactor(1, 1)
        root.addWidget(right_split, stretch=1)

        # Status bar
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage(
            "Benvingut! Fes clic a «+ Afegir Ruta GPX» per començar.")

    def _build_left_panel(self):
        panel = QWidget()
        panel.setStyleSheet(
            f"background:{P_SURFACE};"
            f"border-right:1px solid {P_DIVIDER};")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── HEADER ───────────────────────────────────────────────
        header = QFrame()
        header.setFixedHeight(88)
        header.setStyleSheet(
            f"QFrame{{background:{P_SURFACE};"
            f"border-bottom:1px solid {P_DIVIDER};}}")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 12, 16, 12)
        hl.setSpacing(12)

        # Logo icon (generated pixmap)
        logo_lbl = QLabel()
        logo_lbl.setPixmap(_make_logo_pixmap(52))
        logo_lbl.setFixedSize(52, 52)
        logo_lbl.setStyleSheet("background:transparent;")

        # Title stack
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title = QLabel("Camins Rals")
        title.setFont(QFont("Arial", 16, QFont.Bold))
        title.setStyleSheet(f"color:{P_TEXT1};background:transparent;")
        subtitle = QLabel("Rutes Históriques de Catalunya")
        subtitle.setFont(QFont("Arial", 9))
        subtitle.setStyleSheet(f"color:{P_TEXT2};background:transparent;")
        title_col.addWidget(title)
        title_col.addWidget(subtitle)
        title_col.addStretch()

        hl.addWidget(logo_lbl)
        hl.addLayout(title_col, stretch=1)
        layout.addWidget(header)

        # ── SCROLLABLE BODY ──────────────────────────────────────
        body_scroll = QScrollArea()
        body_scroll.setWidgetResizable(True)
        body_scroll.setFrameShape(QFrame.NoFrame)
        body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body_scroll.setStyleSheet(
            f"QScrollArea{{background:{P_SURFACE};border:none;}}"
            f"QScrollBar:vertical{{background:{P_SURF2};width:6px;border-radius:3px;}}"
            f"QScrollBar::handle:vertical{{background:{P_BORDER};border-radius:3px;"
            f"min-height:20px;}}"
            f"QScrollBar::handle:vertical:hover{{background:{P_BLUE};}}"
            f"QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}"
        )
        body = QWidget()
        body.setStyleSheet(f"background:{P_SURFACE};")
        blay = QVBoxLayout(body)
        blay.setContentsMargins(16, 16, 16, 16)
        blay.setSpacing(12)
        body_scroll.setWidget(body)
        layout.addWidget(body_scroll, stretch=1)

        # ── helper: section label ─────────────────────────────────
        def section_label(text):
            lbl = QLabel(text.upper())
            lbl.setFont(QFont("Arial", 9, QFont.Bold))
            lbl.setStyleSheet(
                f"color:{P_TEXT2};background:transparent;"
                "letter-spacing:1px;")
            return lbl

        # ── ADD GPX  — primary CTA ────────────────────────────────
        add_btn = QPushButton("+ Afegir Ruta GPX")
        add_btn.setMinimumHeight(46)
        add_btn.setStyleSheet(_BTN_PRIMARY)
        add_btn.setToolTip("Obre un o més fitxers .gpx")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.clicked.connect(self._add_files)
        blay.addWidget(add_btn)

        # ── ROUTES LIST ───────────────────────────────────────────
        blay.addWidget(section_label("Rutes carregades"))

        routes_card = QFrame()
        routes_card.setStyleSheet(
            f"QFrame{{background:{P_SURF2};"
            f"border:1px solid {P_BORDER};border-radius:10px;}}")
        rcl = QVBoxLayout(routes_card)
        rcl.setContentsMargins(8, 8, 8, 8)
        rcl.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(160)
        scroll.setMaximumHeight(260)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(
            f"QScrollArea{{background:transparent;border:none;}}"
            f"QScrollBar:vertical{{background:{P_SURF2};width:5px;border-radius:2px;}}"
            f"QScrollBar::handle:vertical{{background:{P_BORDER};border-radius:2px;}}"
            f"QScrollBar::handle:vertical:hover{{background:{P_BLUE};}}"
            f"QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}"
        )
        self._files_container = QWidget()
        self._files_container.setStyleSheet(f"background:{P_SURF2};")
        self._files_vbox = QVBoxLayout(self._files_container)
        self._files_vbox.setAlignment(Qt.AlignTop)
        self._files_vbox.setSpacing(6)
        scroll.setWidget(self._files_container)
        rcl.addWidget(scroll)
        blay.addWidget(routes_card)

        # ── MAP CONTROLS ──────────────────────────────────────────
        blay.addWidget(section_label("Controls del mapa"))

        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(8)

        fit_btn = QPushButton("⊞  Encaixa Ruta")
        fit_btn.setMinimumHeight(36)
        fit_btn.setStyleSheet(_BTN_DARK)
        fit_btn.setToolTip("Centra el mapa per mostrar totes les rutes")
        fit_btn.setCursor(Qt.PointingHandCursor)
        fit_btn.clicked.connect(self._fit_all)

        self._wpt_btn = QPushButton("◉  POIs Visibles")
        self._wpt_btn.setMinimumHeight(36)
        self._wpt_btn.setStyleSheet(_BTN_AMBER)
        self._wpt_btn.setToolTip("Mostra o amaga els punts d'interès")
        self._wpt_btn.setCursor(Qt.PointingHandCursor)
        self._wpt_btn.clicked.connect(self._toggle_wpts)

        ctrl_row.addWidget(fit_btn, stretch=1)
        ctrl_row.addWidget(self._wpt_btn, stretch=1)
        blay.addLayout(ctrl_row)

        # ── GLOBAL THICKNESS ──────────────────────────────────────
        blay.addWidget(section_label("Gruix global de totes les rutes"))

        thick_row = QWidget()
        thick_row.setStyleSheet("background:transparent;")
        tl = QHBoxLayout(thick_row)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(8)

        lbl_t = QLabel("Gruix:")
        lbl_t.setStyleSheet(f"color:{P_TEXT2};background:transparent;font-size:10px;")
        self._global_thick = QDoubleSpinBox()
        self._global_thick.setRange(0.5, 20.0)
        self._global_thick.setSingleStep(0.5)
        self._global_thick.setValue(3.0)
        self._global_thick.setFixedWidth(74)

        apply_btn = QPushButton("Aplica a totes")
        apply_btn.setStyleSheet(_BTN_DARK)
        apply_btn.setCursor(Qt.PointingHandCursor)
        apply_btn.clicked.connect(self._apply_global_thick)

        tl.addWidget(lbl_t)
        tl.addWidget(self._global_thick)
        tl.addStretch()
        tl.addWidget(apply_btn)
        blay.addWidget(thick_row)

        # ── STATISTICS ────────────────────────────────────────────
        blay.addWidget(section_label("Estadístiques"))

        stats_card = QFrame()
        stats_card.setStyleSheet(
            f"QFrame{{background:{P_SURF2};"
            f"border:1px solid {P_BORDER};border-radius:10px;}}")
        scl = QVBoxLayout(stats_card)
        scl.setContentsMargins(14, 10, 14, 10)

        self._stats_lbl = QLabel("Cap ruta carregada.")
        self._stats_lbl.setFont(QFont("Arial", 10))
        self._stats_lbl.setWordWrap(True)
        self._stats_lbl.setStyleSheet(
            f"color:{P_TEXT1};background:transparent;line-height:170%;")
        scl.addWidget(self._stats_lbl)
        blay.addWidget(stats_card)

        # ── EXPORT ────────────────────────────────────────────────
        blay.addWidget(section_label("Exportació d'alta qualitat"))

        # Settings row (width + scale)
        cfg_card = QFrame()
        cfg_card.setStyleSheet(
            f"QFrame{{background:{P_SURF2};"
            f"border:1px solid {P_BORDER};border-radius:10px;}}")
        ccl = QVBoxLayout(cfg_card)
        ccl.setContentsMargins(14, 10, 14, 10)
        ccl.setSpacing(8)

        row_w = QHBoxLayout()
        lbl_w = QLabel("Amplada (px):")
        lbl_w.setStyleSheet(f"color:{P_TEXT2};font-size:10px;background:transparent;")
        self._exp_width = QSpinBox()
        self._exp_width.setRange(400, 8000)
        self._exp_width.setValue(2400)
        self._exp_width.setSingleStep(200)
        self._exp_width.setFixedWidth(80)
        self._exp_width.setToolTip("Amplada de la imatge exportada en píxels")
        row_w.addWidget(lbl_w)
        row_w.addStretch()
        row_w.addWidget(self._exp_width)
        ccl.addLayout(row_w)

        row_s = QHBoxLayout()
        lbl_s = QLabel("Escala DPI (×):")
        lbl_s.setStyleSheet(f"color:{P_TEXT2};font-size:10px;background:transparent;")
        self._exp_scale = QSpinBox()
        self._exp_scale.setRange(1, 6)
        self._exp_scale.setValue(4)
        self._exp_scale.setFixedWidth(56)
        self._exp_scale.setToolTip("Factor de resolució — 4 = qualitat d'impressió")
        row_s.addWidget(lbl_s)
        row_s.addStretch()
        row_s.addWidget(self._exp_scale)
        ccl.addLayout(row_s)

        # Cartographic indicators corner
        div = QFrame()
        div.setFrameShape(QFrame.HLine)
        div.setStyleSheet(f"color:{P_DIVIDER};background:{P_DIVIDER};max-height:1px;")
        ccl.addWidget(div)

        row_c = QHBoxLayout()
        lbl_c = QLabel("Rosa + escala gràfica:")
        lbl_c.setStyleSheet(f"color:{P_TEXT2};font-size:10px;background:transparent;")
        self._corner_combo = QComboBox()
        self._corner_combo.addItem("Baix esquerra",  "BL")
        self._corner_combo.addItem("Baix dreta",     "BR")
        self._corner_combo.addItem("Dalt dreta",     "TR")
        self._corner_combo.addItem("Dalt esquerra",  "TL")
        self._corner_combo.setToolTip(
            "Cantonada on apareixeran la rosa dels vents, "
            "l'escala gràfica i l'atribució")
        row_c.addWidget(lbl_c)
        row_c.addStretch()
        row_c.addWidget(self._corner_combo)
        ccl.addLayout(row_c)

        blay.addWidget(cfg_card)

        # Capture current view — green primary
        cap_btn = QPushButton("📸  Captura la Vista Actual")
        cap_btn.setMinimumHeight(46)
        cap_btn.setStyleSheet(_BTN_EXPORT)
        cap_btn.setToolTip("Exporta exactament el que veus al mapa ara mateix")
        cap_btn.setCursor(Qt.PointingHandCursor)
        cap_btn.clicked.connect(self._capture_current_view)
        blay.addWidget(cap_btn)

        # Full route + segment — side by side
        exp2_row = QHBoxLayout()
        exp2_row.setSpacing(8)

        full_btn = QPushButton("Ruta Sencera")
        full_btn.setMinimumHeight(38)
        full_btn.setStyleSheet(_BTN_GOLD)
        full_btn.setToolTip("Exporta tot el recorregut en una sola imatge")
        full_btn.setCursor(Qt.PointingHandCursor)
        full_btn.clicked.connect(self._export_full_route)

        seg_btn = QPushButton("✂  Segmenta")
        seg_btn.setMinimumHeight(38)
        seg_btn.setStyleSheet(_BTN_SEGMENT)
        seg_btn.setToolTip("Divideix la ruta en N parts i exporta cada part")
        seg_btn.setCursor(Qt.PointingHandCursor)
        seg_btn.clicked.connect(self._segment_export)

        exp2_row.addWidget(full_btn, stretch=1)
        exp2_row.addWidget(seg_btn, stretch=1)
        blay.addLayout(exp2_row)
        blay.addStretch()

        return panel

    # ----------------------------------------------------------
    # WEBCHANNEL
    # ----------------------------------------------------------

    def _setup_webchannel(self):
        self._bridge = MapBridge()
        self._channel = QWebChannel()
        self._channel.registerObject("bridge", self._bridge)
        self.map_view.page().setWebChannel(self._channel)

    # ----------------------------------------------------------
    # MAP MANAGEMENT
    # ----------------------------------------------------------

    def _reload_map(self):
        entries = self._entries()
        html = build_interactive_html(entries) if entries else build_empty_html()

        if self._tmp_html and os.path.exists(self._tmp_html):
            try:
                os.unlink(self._tmp_html)
            except OSError:
                pass

        tmp = tempfile.NamedTemporaryFile(
            suffix='.html', delete=False, mode='w', encoding='utf-8')
        tmp.write(html)
        tmp.close()
        self._tmp_html = tmp.name
        self.map_view.load(QUrl.fromLocalFile(tmp.name))

    def _entries(self):
        return [
            {'gpx_data': r.gpx_data, 'color': r.color, 'thickness': r.thickness}
            for r in self.file_rows
        ]

    # ----------------------------------------------------------
    # FILE MANAGEMENT
    # ----------------------------------------------------------

    def _add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Selecciona fitxers GPX",
            str(os.path.expanduser("~")),
            "Fitxers GPX (*.gpx)")
        for path in paths:
            try:
                gpx = GPXData(path)
                color = DEFAULT_COLORS[len(self.file_rows) % len(DEFAULT_COLORS)]
                row = FileRowWidget(gpx, color)
                row.removed.connect(self._remove_row)
                row.changed.connect(self._on_changed)
                self._files_vbox.addWidget(row)
                self.file_rows.append(row)
            except Exception as exc:
                QMessageBox.warning(
                    self, "Error de lectura",
                    f"No s'ha pogut llegir «{os.path.basename(path)}»:\n{exc}")
        if paths:
            self._on_changed()

    def _remove_row(self, row: FileRowWidget):
        self._files_vbox.removeWidget(row)
        self.file_rows.remove(row)
        row.deleteLater()
        self._on_changed()

    def _on_changed(self):
        self._reload_map()
        self._update_stats()
        entries = self._entries()
        if entries:
            self.elev_canvas.update_profile(entries)
        else:
            self.elev_canvas._draw_empty()

    def _update_stats(self):
        if not self.file_rows:
            self._stats_lbl.setText("Cap ruta carregada.")
            return
        total_km = sum(r.gpx_data.total_km for r in self.file_rows)
        total_gain = sum(r.gpx_data.ele_gain for r in self.file_rows)
        total_loss = sum(r.gpx_data.ele_loss for r in self.file_rows)
        total_wpts = sum(len(r.gpx_data.waypoints) for r in self.file_rows)
        self._stats_lbl.setText(
            f"Rutes: {len(self.file_rows)}\n"
            f"Distància total: {total_km:.1f} km\n"
            f"Guany d'altura: +{total_gain:.0f} m\n"
            f"Pèrdua d'altura: −{total_loss:.0f} m\n"
            f"Punts d'interès (POIs): {total_wpts}"
        )

    # ----------------------------------------------------------
    # CONTROLS
    # ----------------------------------------------------------

    def _get_corner(self):
        return self._corner_combo.currentData()

    def _apply_global_thick(self):
        val = self._global_thick.value()
        for r in self.file_rows:
            r.thick_spin.setValue(val)
        self._reload_map()

    def _fit_all(self):
        self.map_view.page().runJavaScript("fitAll();")

    def _toggle_wpts(self):
        self._wpts_visible = not self._wpts_visible
        js_val = "true" if self._wpts_visible else "false"
        self.map_view.page().runJavaScript(f"toggleWaypoints({js_val});")
        if self._wpts_visible:
            self._wpt_btn.setText("◉  POIs Visibles")
            self._wpt_btn.setStyleSheet(_BTN_AMBER)
        else:
            self._wpt_btn.setText("○  POIs Ocults")
            self._wpt_btn.setStyleSheet(_BTN_GHOST)

    def _save_elevation_profile(self):
        if not self.file_rows:
            QMessageBox.warning(self, "Atenció", "No hi ha cap ruta carregada!")
            return
        save_path, _ = QFileDialog.getSaveFileName(
            self, "Guardar Perfil d'Elevació",
            "perfil_elevacio.png", "Imatge PNG (*.png)")
        if not save_path:
            return
        try:
            fig = self.elev_canvas.figure
            fig.savefig(save_path, dpi=300, bbox_inches='tight',
                        facecolor=fig.get_facecolor())
            self._status.showMessage(f"✓  Perfil d'elevació guardat: {save_path}")
            QMessageBox.information(self, "Guardat!",
                                    f"Perfil guardat a:\n{save_path}")
        except Exception as exc:
            QMessageBox.critical(self, "Error", str(exc))

    def _toggle_elevation(self):
        if self.elev_canvas.isVisible():
            self.elev_canvas.hide()
            self._toggle_elev_btn.setText("Mostra ▼")
        else:
            self.elev_canvas.show()
            self._toggle_elev_btn.setText("Amaga ▲")

    # ----------------------------------------------------------
    # EXPORT
    # ----------------------------------------------------------

    def _capture_current_view(self):
        if not self.file_rows:
            QMessageBox.warning(self, "Atenció",
                                "No hi ha cap ruta carregada!")
            return
        save_path, _ = QFileDialog.getSaveFileName(
            self, "Guardar imatge — Vista Actual",
            "mapa_camins.png", "Imatge PNG (*.png)")
        if not save_path:
            return

        self._status.showMessage("Llegint l'estat del mapa…")

        def got_state(state_json):
            if not state_json:
                QMessageBox.critical(self, "Error",
                                     "No s'ha pogut llegir l'estat del mapa.")
                return
            try:
                state = json.loads(state_json)
            except Exception as exc:
                QMessageBox.critical(self, "Error", str(exc))
                return
            self._do_export(state=state, save_path=save_path)

        self.map_view.page().runJavaScript("getMapState()", got_state)

    def _do_export(self, state, save_path):
        entries = self._entries()
        n = state['n']
        s = state['s']
        e = state['e']
        w = state['w']
        zoom = int(state['zoom'])
        cx = state['cx']
        cy = state['cy']

        lat_h = n - s or 0.001
        lon_w = (e - w) * math.cos(math.radians((n + s) / 2)) or 0.001
        aspect = lon_w / lat_h
        width = self._exp_width.value()
        height = max(300, int(width / aspect))
        scale = self._exp_scale.value()

        html = build_export_html(
            entries, self._wpts_visible, cx, cy, zoom=zoom,
            corner=self._get_corner())

        self._status.showMessage(
            f"Exportant {width}×{height} px (escala {scale}×)…")
        self._start_export_worker(html, save_path, width, height, scale)

    def _export_full_route(self):
        if not self.file_rows:
            QMessageBox.warning(self, "Atenció",
                                "No hi ha cap ruta carregada!")
            return
        save_path, _ = QFileDialog.getSaveFileName(
            self, "Guardar imatge — Ruta Sencera",
            "mapa_sencer.png", "Imatge PNG (*.png)")
        if not save_path:
            return

        entries = self._entries()
        all_c = []
        for en in entries:
            all_c.extend(en['gpx_data'].all_coords())
        lats = [c[0] for c in all_c]
        lons = [c[1] for c in all_c]
        cx = (min(lats) + max(lats)) / 2
        cy = (min(lons) + max(lons)) / 2
        bounds = [[min(lats), min(lons)], [max(lats), max(lons)]]

        lat_h = (max(lats) - min(lats)) or 0.001
        lon_w = (max(lons) - min(lons)) * math.cos(math.radians(cx)) or 0.001
        aspect = lon_w / lat_h
        width = self._exp_width.value()
        height = max(300, int(width / aspect))
        scale = self._exp_scale.value()

        html = build_export_html(
            entries, self._wpts_visible, cx, cy, fit_bounds=bounds,
            corner=self._get_corner())
        self._status.showMessage(
            f"Exportant ruta sencera {width}×{height} px…")
        self._start_export_worker(html, save_path, width, height, scale)

    def _segment_export(self):
        if not self.file_rows:
            QMessageBox.warning(self, "Atenció",
                                "No hi ha cap ruta carregada!")
            return
        dlg = SegmentDialog(
            self.file_rows,
            self._exp_width.value(),
            self._exp_scale.value(),
            self._get_corner(),
            self)
        dlg.exec_()

    def _start_export_worker(self, html, path, width, height, scale):
        self._export_worker = ExportWorker(html, path, width, height, scale)
        self._export_worker.finished.connect(self._export_done)
        self._export_worker.error.connect(self._export_error)
        self._export_worker.status.connect(self._status.showMessage)
        self._export_worker.start()

    def _export_done(self, path):
        self._status.showMessage(f"✓  Imatge guardada: {path}")
        QMessageBox.information(
            self, "Guardat!",
            f"La imatge s'ha guardat correctament a:\n{path}")

    def _export_error(self, msg):
        self._status.showMessage("Error en exportar.")
        QMessageBox.critical(
            self, "Error d'Exportació",
            f"No s'ha pogut generar la imatge:\n\n{msg}")


# ============================================================
# ENTRY POINT
# ============================================================

def main():
    # When running as a PyInstaller Windows bundle, point Playwright to the
    # Chromium copy we bundled inside the distribution folder.
    if getattr(sys, 'frozen', False):
        _bundle_dir = os.path.dirname(sys.executable)
        _browsers = os.path.join(_bundle_dir, 'ms-playwright')
        if os.path.exists(_browsers):
            os.environ['PLAYWRIGHT_BROWSERS_PATH'] = _bundle_dir
        # QtWebEngine needs its helper process found relative to the bundle
        os.environ.setdefault(
            'QTWEBENGINEPROCESS_PATH',
            os.path.join(_bundle_dir, 'QtWebEngineProcess.exe'))

    os.environ.setdefault(
        'QTWEBENGINE_CHROMIUM_FLAGS',
        '--disable-web-security --allow-file-access-from-files'
    )

    app = QApplication(sys.argv)
    app.setApplicationName("Camins Rals")
    app.setStyle("Fusion")
    app.setStyleSheet(f"""
        QMainWindow, QWidget   {{ background-color: {P_BG}; color: {P_TEXT1}; }}
        QDialog                {{ background-color: {P_SURFACE}; color: {P_TEXT1}; }}
        QScrollArea            {{ border: none; background: transparent; }}
        QLabel                 {{ color: {P_TEXT1}; background: transparent; }}
        QStatusBar             {{
            background: {P_SURFACE}; color: {P_TEXT2};
            font-size: 11px; border-top: 1px solid {P_DIVIDER};
            padding: 2px 8px;
        }}
        QGroupBox {{
            border: 1px solid {P_BORDER}; border-radius: 8px;
            margin-top: 10px; padding-top: 8px;
            background: {P_SURF2};
        }}
        QGroupBox::title {{
            subcontrol-origin: margin; left: 12px;
            color: {P_TEXT2}; font-weight: bold; font-size: 10px;
        }}
        QSpinBox, QDoubleSpinBox, QLineEdit {{
            background: {P_SURFACE}; color: {P_TEXT1};
            border: 1px solid {P_BORDER}; border-radius: 6px;
            padding: 3px 8px; min-height: 26px;
            selection-background-color: {P_BLUE_L};
        }}
        QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {{
            border: 2px solid {P_BLUE};
        }}
        QSpinBox::up-button, QSpinBox::down-button,
        QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
            background: {P_SURF2}; border: none; width: 18px;
            border-radius: 0px;
        }}
        QSpinBox::up-button:hover, QSpinBox::down-button:hover,
        QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{
            background: {P_DIVIDER};
        }}
        QToolTip {{
            background: {P_TEXT1}; color: white;
            border: none; padding: 5px 8px; border-radius: 4px;
            font-size: 11px;
        }}
        QMessageBox {{ background: {P_SURFACE}; }}
        QMessageBox QLabel {{ color: {P_TEXT1}; font-size: 12px; }}
        QMessageBox QPushButton {{
            background: {P_BLUE}; color: white; border: none;
            border-radius: 6px; padding: 6px 16px; font-size: 11px;
            min-width: 80px;
        }}
        QMessageBox QPushButton:hover {{ background: {P_BLUE_D}; }}
    """)

    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
