# ui/hull3d_view.py
"""
Hull3DView — QWebEngineView wrapper hosting a Three.js WebGL hull renderer.

Replaces the Matplotlib Axes3D canvas in structural_tab.py.

Data flow
---------
Python:   Hull3DView.set_hull(offsets, L, T, title)
              → _hull_to_json(offsets, L, T)     # masking + triangulation
              → page().runJavaScript(             # push serialised mesh
                    "window.updateHull({...})")
JS:       window.updateHull(data)                 # rebuild BufferGeometry

Coordinate mapping (Three.js is Y-up)
--------------------------------------
    Hull X (length,   0 → L)  →  Three.js  X
    Hull Z (height,   0 → T)  →  Three.js  Y  (up)
    Hull Y (breadth,  0 → B)  →  Three.js  Z  (+Z = starboard)

No Qt imports below QWebEngineView — all geometry math is pure numpy.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import numpy as np
from pathlib import Path
from PyQt6.QtCore import QUrl
from PyQt6.QtWebEngineWidgets import QWebEngineView

# Waterline visibility threshold (matches visualization.plot_hull_3d)
_VIZ = 1e-4


# ── Geometry serialiser ───────────────────────────────────────────────────────

def _hull_to_json(offsets: np.ndarray, L: float, T: float) -> dict:
    """
    Convert the hull offset matrix to a PlaneGeometry-ready JSON payload.

    Stations are cleaned through the SAME sanitizer the CAD exporters use
    (services.step_export_service.sanitize_hull_data), so the preview shows
    exactly what will be exported: rows padded with y = 0 below the local
    rocker line are trimmed (no keel fin, the keel line follows the rocker),
    and the extreme bow/stern stations are sharp centerline slivers instead
    of blunt boxes.

    The hull cross-section is encoded as a full port→keel→starboard ring so
    that Three.js PlaneGeometry's built-in index buffer handles all face
    generation — including the keel seam — without hand-rolled winding logic.

    Ring layout (nw_ring = 2 × N vertices per station, N = resampled rows):
        p = 0 … N-1       port side,      gunwale (p=0) → keel  (p=N-1)   z ≤ 0
        p = N … 2*N-1     starboard side, keel    (p=N) → gunwale (p=end)  z ≥ 0

    PlaneGeometry(1, 1, ns-1, nw_ring-1) vertex index = p * ns + i, which
    exactly matches the (p, i) iteration order used here.

    Returns
    -------
    dict with keys:
        grid      flat float list [x,y,z …]  ns × nw_ring vertices, p-major
        ns        int  station count (valid stations only)
        nw_ring   int  profile points per station  (= 2 × N)
        colors    flat float list [r,g,b …]  per-vertex ocean palette
        keel      [[x,y,z] …]               keel-line points  (Three.js)
        waterline [[x,y,z] …]               DWL points, starboard only
        meta      {L, T, maxBeam, title}
    """
    from services.step_export_service import (
        sanitize_hull_data, _stations_from_offsets)

    offsets = np.asarray(offsets, dtype=float)
    ns_raw, nw = offsets.shape
    x_st = np.linspace(0.0, L, ns_raw)
    z_st = np.linspace(0.0, T, nw)

    secs = [s for s in sanitize_hull_data(
        _stations_from_offsets(offsets, x_st, z_st)) if s is not None]

    if len(secs) < 2:
        return {"grid": [], "ns": 0, "nw_ring": 0, "colors": [],
                "keel": [], "waterline": [],
                "meta": {"L": float(L), "T": float(T),
                         "maxBeam": float(np.max(offsets)) * 2.0, "title": ""}}

    ns = len(secs)
    N = len(secs[0])                     # uniform resampled rows keel → sheer
    nw_ring = 2 * N

    # ── Full-ring grid: p-major, i-minor ─────────────────────────────────────
    # PlaneGeometry vertex order: iy * gridX1 + ix = p * ns + i  (iy=p, ix=i).
    grid:   list[float] = []
    colors: list[float] = []
    for p in range(nw_ring):
        if p < N:
            j, sign = (N - 1) - p, -1.0   # port: gunwale (p=0) → keel
        else:
            j, sign = p - N, +1.0         # starboard: keel → gunwale
        for sec in secs:
            xi = float(sec[j, 0])
            yi = float(sec[j, 2])         # hull height → Three.js Y (up)
            zi = sign * float(sec[j, 1])  # half-breadth → Three.js Z
            h_n = yi / max(T, 0.01)
            grid   += [xi, yi, zi]
            colors += [0.04 + 0.10 * h_n,
                       0.30 + 0.28 * h_n,
                       0.52 + 0.33 * h_n]

    # ── Keel line (first sanitized row = true local keel, on centerline) ─────
    keel = [[float(sec[0, 0]), float(sec[0, 2]), 0.0] for sec in secs]

    # ── Waterline ring (starboard only; JS mirrors for port) ─────────────────
    waterline = [[float(sec[-1, 0]), float(sec[-1, 2]), float(sec[-1, 1])]
                 for sec in secs if float(sec[-1, 1]) > _VIZ]

    return {
        "grid":      grid,
        "ns":        ns,
        "nw_ring":   nw_ring,
        "colors":    colors,
        "keel":      keel,
        "waterline": waterline,
        "meta": {
            "L":       float(L),
            "T":       float(T),
            "maxBeam": float(np.max(offsets)) * 2.0,
            "title":   "",
        },
    }


# ── HTML / Three.js template ─────────────────────────────────────────────────
# Split into HEAD (static markup) and TAIL (app script).
# _build_html() inlines three.min.js and OrbitControls.js from ui/static/
# between them, producing a fully self-contained page with no CDN dependency.

_HTML_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#1a1a2e; overflow:hidden; }
  canvas { display:block; }
  #title {
    position:absolute; top:10px; left:0; right:0; text-align:center;
    color:rgba(200,220,255,.80); font:bold 12px 'Courier New',monospace;
    pointer-events:none;
  }
  #hint {
    position:absolute; top:10px; right:12px; text-align:right;
    color:rgba(200,220,255,.35); font:9px/1.7 'Courier New',monospace;
    pointer-events:none;
  }
  #hud {
    position:absolute; bottom:10px; left:12px;
    color:rgba(200,220,255,.45); font:10px 'Courier New',monospace;
    pointer-events:none;
  }
</style>
</head>
<body>
<div id="title">Awaiting hull data…</div>
<div id="hint">Left-drag · Orbit<br>Right-drag · Pan<br>Scroll · Zoom<br>Dbl-click · Reset</div>
<div id="hud"></div>
"""

# App script injected AFTER the inlined library scripts
_HTML_TAIL = """<script>
'use strict';

// ── Renderer ─────────────────────────────────────────────────────────────────
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(window.devicePixelRatio || 1);
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.setClearColor(0x1a1a2e, 1);
document.body.appendChild(renderer.domElement);

// ── Scene & camera ────────────────────────────────────────────────────────────
const scene  = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(
    22,                                         // narrow FOV → near-isometric feel
    window.innerWidth / window.innerHeight,
    0.001, 500
);

// ── OrbitControls ─────────────────────────────────────────────────────────────
const controls = new THREE.OrbitControls(camera, renderer.domElement);
controls.enableDamping     = true;
controls.dampingFactor     = 0.06;
controls.screenSpacePanning = true;    // right-drag pans in screen space (CAD-style)
controls.rotateSpeed       = 0.65;
controls.zoomSpeed         = 1.1;
controls.panSpeed          = 0.8;
controls.minDistance       = 0.02;
controls.maxDistance       = 200;

// Double-click to reset camera to the initial hull view
let _resetPos = new THREE.Vector3(5, 2, 4);
let _resetTarget = new THREE.Vector3(0, 0, 0);
renderer.domElement.addEventListener('dblclick', () => {
    camera.position.copy(_resetPos);
    controls.target.copy(_resetTarget);
    controls.update();
});

// ── Lighting ──────────────────────────────────────────────────────────────────
scene.add(new THREE.AmbientLight(0x334466, 0.90));
const sun = new THREE.DirectionalLight(0xffffff, 0.85);
sun.position.set(1, 3, 2);
scene.add(sun);
const fill = new THREE.DirectionalLight(0x2244aa, 0.40);
fill.position.set(-2, -1, -3);
scene.add(fill);

// ── Scene-object lifecycle ────────────────────────────────────────────────────
const _objs = [];
function _clearScene() {
    _objs.forEach(obj => {
        scene.remove(obj);
        if (obj.geometry) obj.geometry.dispose();
        if (obj.material) {
            (Array.isArray(obj.material) ? obj.material : [obj.material])
                .forEach(m => m.dispose());
        }
    });
    _objs.length = 0;
}
function _add(obj) { scene.add(obj); _objs.push(obj); return obj; }

// ── Main entry point — called from Python via runJavaScript ──────────────────
window.updateHull = function(data) {
    _clearScene();
    const { grid, ns, nw_ring, colors, keel, waterline, meta } = data;
    const { L, T, maxBeam, title } = meta;

    // ── Hull surface — PlaneGeometry deformed to a full port→keel→stbd ring ──
    // Python encodes the hull as a ring cross-section with nw_ring = 2*nw
    // profile points per station, laid out p-major (p outer, i inner):
    //   p = 0..nw-1      port side   (gunwale → keel,  z ≤ 0)
    //   p = nw..2*nw-1   stbd side   (keel → gunwale,  z ≥ 0)
    // PlaneGeometry(1, 1, ns-1, nw_ring-1) has vertex index p*ns+i, which
    // exactly matches this layout — no manual index winding needed.
    // All-zero tip stations are snapped to a single point in Python so their
    // triangles are zero-area.  Rocker-padded cells are collapsed to the keel.
    if (grid && grid.length === ns * nw_ring * 3) {
        const geo = new THREE.PlaneGeometry(1, 1, ns - 1, nw_ring - 1);
        const pos = geo.attributes.position;
        const col = new Float32Array(ns * nw_ring * 3);

        for (let p = 0; p < nw_ring; p++) {
            for (let i = 0; i < ns; i++) {
                const vi = p * ns + i;   // PlaneGeometry: iy * gridX1 + ix
                const gi = vi * 3;
                pos.setXYZ(vi, grid[gi], grid[gi + 1], grid[gi + 2]);
                col[gi]     = colors[gi];
                col[gi + 1] = colors[gi + 1];
                col[gi + 2] = colors[gi + 2];
            }
        }
        geo.setAttribute('color', new THREE.BufferAttribute(col, 3));
        pos.needsUpdate = true;
        geo.computeVertexNormals();
        _add(new THREE.Mesh(geo, new THREE.MeshPhongMaterial({
            vertexColors: true, side: THREE.DoubleSide,
            shininess: 90, specular: new THREE.Color(0x112244),
        })));
    }

    // ── Keel line ─────────────────────────────────────────────────────────
    if (keel && keel.length > 1) {
        const geo = new THREE.BufferGeometry();
        geo.setAttribute('position',
            new THREE.Float32BufferAttribute(keel.flat(), 3));
        _add(new THREE.Line(geo,
            new THREE.LineBasicMaterial({ color:0xffffff, transparent:true, opacity:0.45 })));
    }

    // ── Waterline rings (starboard + mirrored port) ───────────────────────
    if (waterline && waterline.length > 1) {
        const stbd = waterline.flat();
        const port = waterline.flatMap(([x, y, z]) => [x, y, -z]);
        const wMat = new THREE.LineBasicMaterial({
            color:0x00d2ff, transparent:true, opacity:0.75 });
        [stbd, port].forEach(pts => {
            const geo = new THREE.BufferGeometry();
            geo.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
            _add(new THREE.Line(geo, wMat.clone()));
        });
    }

    // ── Camera placement ──────────────────────────────────────────────────
    // Centre the orbit target on the hull centroid (Three.js coords):
    //   X = L/2  (mid-length), Y = T/2  (mid-height), Z = 0  (centreline)
    const cx = L / 2, cy = T / 2;
    const dist = Math.max(L, maxBeam * 2, T * 5) * 1.15;

    // Stern–starboard–above view  (mirrors matplotlib elev=20 azim=-130)
    const camX = cx + dist * 0.55;
    const camY = cy + dist * 0.30;
    const camZ =      dist * 0.45;

    camera.position.set(camX, camY, camZ);
    controls.target.set(cx, cy, 0);
    controls.update();

    // Save for double-click reset
    _resetPos.set(camX, camY, camZ);
    _resetTarget.set(cx, cy, 0);

    // ── HUD ───────────────────────────────────────────────────────────────
    document.getElementById('title').textContent =
        title || 'ASCE Concrete Canoe — Hull Preview';
    document.getElementById('hud').innerHTML =
        `L&nbsp;=&nbsp;${L.toFixed(3)}&nbsp;m&nbsp;&nbsp;` +
        `B&nbsp;=&nbsp;${maxBeam.toFixed(3)}&nbsp;m&nbsp;&nbsp;` +
        `T&nbsp;=&nbsp;${T.toFixed(3)}&nbsp;m`;
};

// ── Render loop ───────────────────────────────────────────────────────────────
(function animate() {
    requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
})();

// ── Resize ────────────────────────────────────────────────────────────────────
window.addEventListener('resize', () => {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
});
</script>
</body>
</html>
"""


def _build_html(static_dir: Path) -> str:
    """
    Return a fully self-contained HTML page with Three.js libraries inlined.

    Raises FileNotFoundError if setup_offline_viewer.py has not been run yet.
    """
    three_path = static_dir / "three.min.js"
    orbit_path = static_dir / "OrbitControls.js"

    missing = [p for p in (three_path, orbit_path) if not p.exists()]
    if missing:
        names = ", ".join(p.name for p in missing)
        raise FileNotFoundError(
            f"Offline JS assets not found: {names}\n"
            f"Run  python setup_offline_viewer.py  once from the project root."
        )

    three_js = three_path.read_text(encoding="utf-8")
    orbit_js = orbit_path.read_text(encoding="utf-8")

    return (
        _HTML_HEAD
        + "\n<script>\n" + three_js + "\n</script>\n"
        + "\n<script>\n" + orbit_js + "\n</script>\n"
        + _HTML_TAIL
    )


# ── Qt widget ─────────────────────────────────────────────────────────────────

class Hull3DView(QWebEngineView):
    """
    QWebEngineView hosting the Three.js hull renderer.

    Usage (main thread only):
        view = Hull3DView(parent)
        view.set_hull(offsets_m, L_m, T_m, title="…")

    The widget handles the async page-load sequence internally.
    Callers may call set_hull() at any time; data is queued until the page
    is ready.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self._page_ready = False
        self._pending: dict | None = None
        self.loadFinished.connect(self._on_ready)

        # Inline Three.js from ui/static/ into a temp HTML file.
        # The page is fully self-contained — no network access needed.
        # Under a PyInstaller bundle the data files live in sys._MEIPASS.
        _meipass = getattr(sys, "_MEIPASS", None)
        _static = (Path(_meipass) / "ui" / "static" if _meipass
                   else Path(__file__).parent / "static")
        html = _build_html(_static)

        fd, self._html_path = tempfile.mkstemp(
            suffix=".html", prefix="hull3d_canoe_"
        )
        os.close(fd)
        with open(self._html_path, "w", encoding="utf-8") as fh:
            fh.write(html)
        self.load(QUrl.fromLocalFile(self._html_path))

    # ------------------------------------------------------------------
    def _on_ready(self, ok: bool) -> None:
        self._page_ready = ok
        if ok and self._pending is not None:
            self._inject(self._pending)
            self._pending = None

    # ------------------------------------------------------------------
    def set_hull(
        self,
        offsets: np.ndarray,
        L: float,
        T: float,
        title: str = "",
    ) -> None:
        """Serialise hull geometry and push it to the Three.js renderer."""
        payload = _hull_to_json(offsets, L, T)
        payload["meta"]["title"] = title
        if self._page_ready:
            self._inject(payload)
        else:
            self._pending = payload   # held until _on_ready fires

    # ------------------------------------------------------------------
    def _inject(self, payload: dict) -> None:
        """Push a geometry payload to the live Three.js page."""
        js_call = (
            "if(typeof window.updateHull==='function')"
            f"{{window.updateHull({json.dumps(payload)})}}"
        )
        self.page().runJavaScript(js_call)

    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:
        try:
            os.unlink(self._html_path)
        except OSError:
            pass
        super().closeEvent(event)
