# visualization.py
"""
Visualisation: physical 3-D hull render + embedded structural charts.

All figures are created with the Object-Oriented Matplotlib API
(matplotlib.figure.Figure) so that pyplot never registers them in its
global state machine, preventing the 'More than 20 figures opened' leak.
"""
from matplotlib.figure import Figure
from mpl_toolkits.mplot3d import Axes3D      # noqa — registers "3d" projection
from matplotlib import cm
import numpy as np


def plot_hull_3d(offsets, L, T, title="Optimised Hull"):
    """
    Render the canoe as a physical 3-D hull surface with TRUE proportions.
    The canoe sits upright: length along X, beam along Y, height along Z.
    Axes are scaled proportionally so a long narrow canoe looks like one.

    Masking strategy
    ----------------
    Michlet/Godzilla fills unused grid cells with exact 0.0.  A naive
    "mask everything < threshold" approach also kills the v-keel, which
    legitimately has Y=0 along the centreline.

    Instead we scan each station row from the keel end (j=0) to find the
    first column with a genuinely positive breadth.  The cell immediately
    before that (j_keel = j_first_positive - 1) is the TRUE keel vertex —
    its Y may be 0, but matplotlib needs it to close the hull faces.  Only
    the cells before j_keel (pure rocker padding) are set to NaN.
    """
    _ZERO = 1e-9          # anything ≤ this is treated as exactly zero
    _VIZ  = 1e-4          # minimum visible breadth for the waterline ring

    ns, nw = offsets.shape
    x = np.linspace(0, L, ns)
    z = np.linspace(0, T, nw)
    X, Z = np.meshgrid(x, z, indexing="ij")

    # ── Per-station NaN masking ───────────────────────────────────────────
    # A station where ALL Y values are zero is a valid bow/stern stem —
    # the hull tapers to a point there.  The adjacent rocked station already
    # has NaN at its below-rocker waterlines, which breaks any zero-to-zero
    # face chain, so no phantom wall is produced.  Only mask the *leading*
    # zero block of intermediate stations (rocker padding).
    Y = offsets.copy().astype(float)
    for i in range(ns):
        row = Y[i]
        pos_idx = np.nonzero(row > _ZERO)[0]
        if pos_idx.size == 0:
            pass                              # valid hull tip — leave untouched
        else:
            j_keel = max(0, pos_idx[0] - 1)  # one step before first positive Y
            Y[i, :j_keel] = np.nan           # mask only rocker padding

    fig = Figure(figsize=(7, 5), facecolor="#1a1a2e")
    ax = fig.add_subplot(111, projection="3d", facecolor="#1a1a2e")

    depth_norm = Z / max(T, 0.01)
    colours    = cm.ocean(0.3 + 0.5 * depth_norm)

    ax.plot_surface(X,  Y, Z, facecolors=colours,
                    alpha=0.85, shade=True, linewidth=0, antialiased=True)
    ax.plot_surface(X, -Y, Z, facecolors=colours,
                    alpha=0.85, shade=True, linewidth=0, antialiased=True)

    # ── Waterline ring — only where the hull reaches the gunwale ─────────
    wl_y  = offsets[:, -1]
    wl_ok = wl_y > _VIZ
    if np.any(wl_ok):
        ax.plot(x[wl_ok],  wl_y[wl_ok], np.full(wl_ok.sum(), T),
                color="cyan", linewidth=1.2, alpha=0.7)
        ax.plot(x[wl_ok], -wl_y[wl_ok], np.full(wl_ok.sum(), T),
                color="cyan", linewidth=1.2, alpha=0.7)

    # ── Keel line — follows true rocker (lowest non-NaN Z per station) ───
    keel_pts = []
    for i in range(ns):
        valid = np.nonzero(~np.isnan(Y[i]))[0]
        if valid.size > 0:
            j0 = valid[0]
            keel_pts.append((x[i], float(Y[i, j0]), z[j0]))
    if keel_pts:
        kx, ky, kz = zip(*keel_pts)
        ax.plot(kx, ky, kz, color="white", lw=0.8, alpha=0.4)

    # ── True physical proportions (1 m in X = 1 m in Y = 1 m in Z) ─────
    max_half_beam = max(float(np.nanmax(offsets)), 0.05)
    x_range = L
    y_range = 2.0 * max_half_beam   # full beam = starboard + port mirrored
    z_range = max(T, 0.05)
    ax.set_box_aspect((x_range, y_range, z_range))

    ax.set_xlim(0, L)
    ax.set_ylim(-max_half_beam, max_half_beam)
    ax.set_zlim(0, z_range)

    ax.set_xlabel("Length (m)", color="white", fontsize=8)
    ax.set_ylabel("Beam (m)",   color="white", fontsize=8)
    ax.set_zlabel("Height (m)", color="white", fontsize=8)
    ax.set_title(title, color="white", fontsize=11, fontweight="bold")

    ax.tick_params(colors="gray", labelsize=6)
    for pane in [ax.xaxis.pane, ax.yaxis.pane, ax.zaxis.pane]:
        pane.fill = False
        pane.set_edgecolor("#333")

    ax.view_init(elev=20, azim=-130)
    fig.tight_layout(pad=0.5)
    return fig


def plot_hull_orthographic(offsets, L, T, title="Hull Geometry"):
    """
    Generate a 4-panel orthographic projection of the hull for engineering review.

    Panels:
      [0,0] Top-Down (Plan View)   - X vs Y, showing hull half-breadth outline
      [0,1] Side (Profile View)    - X vs Z, showing keel + sheer line
      [1,0] Front (Body Plan)      - Y vs Z, at midship / bow / stern sections
      [1,1] 3D perspective         - Re-uses the plot_hull_3d style

    Memory Safety:
      Returns a single Figure object.  Caller is expected to pass it to
      _embed_figure() which calls deleteLater() on the old canvas after
      drawing the new one, preventing memory leaks during rapid UI updates.
    """
    ns, nw = offsets.shape
    x = np.linspace(0, L, ns)
    z = np.linspace(0, T, nw)
    X, Z = np.meshgrid(x, z, indexing="ij")
    max_beam = float(np.max(offsets)) * 2
    max_beam = max(max_beam, 0.1)

    fig = Figure(figsize=(10, 7), facecolor="#1a1a2e")
    fig.suptitle(title, fontsize=14, fontweight="bold", color="white", y=0.98)

    # Shared style helper
    def _style_ax(ax, xlabel, ylabel):
        ax.set_facecolor("#1a1a2e")
        ax.set_xlabel(xlabel, color="white", fontsize=8)
        ax.set_ylabel(ylabel, color="white", fontsize=8)
        ax.tick_params(colors="gray", labelsize=7)
        ax.grid(True, linestyle="--", alpha=0.15, color="gray")
        ax.set_aspect("equal")

    # ── [0,0] Top-Down / Plan View ──────────────────────────────
    ax1 = fig.add_subplot(2, 2, 1)
    wl_top = offsets[:, -1]
    ax1.fill_between(x, -wl_top, wl_top, color="#0077b6", alpha=0.3)
    ax1.plot(x, wl_top, color="#00d2ff", linewidth=1.5, label="DWL")
    ax1.plot(x, -wl_top, color="#00d2ff", linewidth=1.5)
    keel = offsets[:, 0]
    ax1.plot(x, keel, color="white", linewidth=0.7, alpha=0.5, linestyle=":")
    ax1.plot(x, -keel, color="white", linewidth=0.7, alpha=0.5, linestyle=":")
    ax1.set_title("Plan View (Top)", fontsize=10, fontweight="bold", color="white")
    _style_ax(ax1, "Length (m)", "Beam (m)")

    # ── [0,1] Side / Profile View ───────────────────────────────
    ax2 = fig.add_subplot(2, 2, 2)
    sheer_line = np.full(ns, T)
    keel_line = np.zeros(ns)
    ax2.fill_between(x, keel_line, sheer_line, color="#0077b6", alpha=0.2)
    ax2.plot(x, sheer_line, color="#00d2ff", linewidth=1.5, label="Sheer")
    ax2.plot(x, keel_line, color="white", linewidth=1, alpha=0.6, label="Keel")
    ax2.axhline(T, color="#f39c12", linewidth=0.8, linestyle="--", alpha=0.6,
                label=f"Draft={T:.3f}m")
    ax2.set_title("Profile View (Side)", fontsize=10, fontweight="bold", color="white")
    _style_ax(ax2, "Length (m)", "Height (m)")
    ax2.set_aspect("auto")
    ax2.legend(fontsize=7, facecolor="#222", edgecolor="#555", labelcolor="white")

    # ── [1,0] Front / Body Plan ──────────────────────────────────
    ax3 = fig.add_subplot(2, 2, 3)
    section_locs = [0.10, 0.25, 0.50, 0.75, 0.90]
    section_colors = ["#e74c3c", "#f39c12", "#00d2ff", "#2ecc71", "#9b59b6"]
    for frac, col in zip(section_locs, section_colors):
        idx = int(frac * (ns - 1))
        y_sec = offsets[idx, :]
        label = f"x={frac*L:.1f}m ({frac*100:.0f}%)"
        ax3.plot(y_sec, z, color=col, linewidth=1.3, label=label)
        ax3.plot(-y_sec, z, color=col, linewidth=1.3)
    ax3.set_title("Body Plan (Front)", fontsize=10, fontweight="bold", color="white")
    _style_ax(ax3, "Half-Beam (m)", "Height (m)")
    ax3.legend(fontsize=6, facecolor="#222", edgecolor="#555",
               labelcolor="white", loc="upper right")

    # ── [1,1] 3D Perspective ─────────────────────────────────────
    ax4 = fig.add_subplot(2, 2, 4, projection="3d", facecolor="#1a1a2e")
    depth_norm = Z / max(T, 0.01)
    colours = cm.ocean(0.3 + 0.5 * depth_norm)
    ax4.plot_surface(X, offsets, Z, facecolors=colours, alpha=0.8,
                     shade=True, linewidth=0, antialiased=True)
    ax4.plot_surface(X, -offsets, Z, facecolors=colours, alpha=0.8,
                     shade=True, linewidth=0, antialiased=True)
    height = max(T, 0.05)
    ax4.set_box_aspect([L, max_beam * 4, height * 8])
    ax4.set_xlabel("L (m)", color="white", fontsize=6)
    ax4.set_ylabel("B (m)", color="white", fontsize=6)
    ax4.set_zlabel("H (m)", color="white", fontsize=6)
    ax4.set_title("3D", fontsize=10, fontweight="bold", color="white")
    ax4.tick_params(colors="gray", labelsize=5)
    for pane in [ax4.xaxis.pane, ax4.yaxis.pane, ax4.zaxis.pane]:
        pane.fill = False
        pane.set_edgecolor("#333")
    ax4.view_init(elev=20, azim=-130)

    fig.tight_layout(pad=1.5)
    return fig


# ═══════════════════  STRUCTURAL CHARTS  ════════════════════════
def plot_structural_envelope(x, M_data, filename=None, cases=None,
                             sag_hog=None, seats=None):
    """
    M_data : (envelope, M_4, M_2) or (envelope, M_4, M_2, M_3) tuple, or a
             bare envelope array.
    cases  : optional iterable of {"envelope", "race_4", "race_3", "race_2"}
             selecting which curves to draw (None = all available).
    sag_hog: optional (M_sag, M_hog) magnitude arrays — drawn as signed
             dashed envelopes (+sag / −hog) so the governing bending sense
             per station is visible (sagging → bottom tension; hogging →
             gunwale tension).
    seats  : optional {"race_4": [ft…], "race_2": […], "race_3": […]} — the
             paddler stations used by the evaluation, drawn as ▼ markers in
             the matching case colors along the top of the chart.
    """
    fig = Figure(figsize=(8, 3), facecolor="#1a1a2e")
    ax = fig.add_subplot(111)
    ax.set_facecolor("#1a1a2e")

    if isinstance(M_data, tuple) and len(M_data) in (3, 4):
        M_envelope, M_4, M_2 = M_data[0], M_data[1], M_data[2]
        M_3 = M_data[3] if len(M_data) == 4 else None
        show = set(cases) if cases is not None else {
            "envelope", "race_4", "race_3", "race_2"}

        peaks, mins = [0.0], [0.0]
        if "race_4" in show and M_4 is not None:
            ax.plot(x, M_4, color="#e74c3c", linewidth=2, label="4-Person Race")
            ax.fill_between(x, M_4, color="#e74c3c", alpha=0.1)
            peaks.append(np.max(M_4)); mins.append(np.min(M_4))

        if "race_3" in show and M_3 is not None:
            ax.plot(x, M_3, color="#2ecc71", linewidth=2, label="3-Person Race")
            ax.fill_between(x, M_3, color="#2ecc71", alpha=0.1)
            peaks.append(np.max(M_3)); mins.append(np.min(M_3))

        if "race_2" in show and M_2 is not None:
            ax.plot(x, M_2, color="#f39c12", linewidth=2, label="2-Person Race")
            ax.fill_between(x, M_2, color="#f39c12", alpha=0.1)
            peaks.append(np.max(M_2)); mins.append(np.min(M_2))

        if "envelope" in show:
            if sag_hog is not None and sag_hog[0] is not None:
                M_sag, M_hog = sag_hog
                ax.plot(x, M_sag, color="#00d2ff", linewidth=1.5,
                        linestyle="--", alpha=0.75,
                        label="Sagging Env (+M, bottom tension)")
                ax.plot(x, -np.asarray(M_hog), color="#ff5ec4", linewidth=1.5,
                        linestyle="--", alpha=0.75,
                        label="Hogging Env (−M, gunwale tension)")
                peaks.append(np.max(M_sag)); mins.append(-np.max(M_hog))
            else:
                ax.plot(x, M_envelope, color="#00d2ff", linewidth=1.5,
                        linestyle="--", alpha=0.6, label="Max Envelope (Abs)")
                peaks.append(np.max(M_envelope))

        peak_y = max(peaks)
        if peak_y <= 0:
            peak_y = float(np.max(M_envelope))
        min_y = min(mins)
    else:
        M_envelope = M_data
        ax.plot(x, M_envelope, color="#00d2ff", linewidth=2, label="Max Moment")
        ax.fill_between(x, M_envelope, color="#00d2ff", alpha=0.15)
        peak_y = np.max(M_envelope)
        min_y = 0

    ax.set_title("Bending Moment Envelope", fontsize=12,
                 fontweight="bold", color="white")
    ax.set_xlabel("Distance from Bow (ft)", color="white")
    ax.set_ylabel("Moment (lb-ft)", color="white")
    ax.set_xlim(0, max(x))

    peak_y = peak_y if peak_y > 0 else 1
    ax.set_ylim(min_y * 1.15 if min_y < 0 else 0, peak_y * 1.15)

    # ── Paddler seat markers (▼, per-case colors, along the top) ────────
    if seats:
        _sc = {"race_4": "#e74c3c", "race_3": "#2ecc71", "race_2": "#f39c12"}
        _shown = (set(cases) if cases is not None
                  else {"race_4", "race_3", "race_2"})
        labelled = False
        for case, color in _sc.items():
            pos = seats.get(case)
            if pos is not None and len(pos) and case in _shown:
                ax.plot(pos, [peak_y * 1.06] * len(pos), "v", color=color,
                        ms=6, linestyle="none", clip_on=False,
                        label=("Paddler seats" if not labelled else None))
                labelled = True

    ax.tick_params(colors="gray")
    ax.grid(True, linestyle="--", alpha=0.2, color="gray")
    ax.legend(fontsize=8, facecolor="#222", edgecolor="#555",
              labelcolor="white", loc="best")
    fig.tight_layout()
    if filename:
        fig.savefig(filename, format="pdf", dpi=300)
    return fig


def _pour_outline_and_mold(ax, x, sheer, mold, show_outline,
                           outline_color="white"):
    """
    Draw the hull side-profile outline and orient the axes for the mould.

    The pour chart is in height-above-keel coordinates, so the canoe's side
    silhouette is the sheer (gunwale) curve closed down to the keel (y=0) at
    bow and stern.  Orientation:

      female mould (default): keel DOWN — the hull sits in a concave mould and
          concrete is troweled into the cavity, outer surface against the mould.
      male mould / plug:      keel UP — the hull is laid up INVERTED over a
          convex form, so the whole view flips vertically to match how the
          zones physically stack during the pour.

    Must be called AFTER ``set_ylim`` so the flip is not overwritten.  Returns
    True for a male mould.
    """
    x = np.asarray(x, dtype=float)
    sheer = np.asarray(sheer, dtype=float)
    if show_outline and x.size:
        oc = outline_color
        ax.plot(x, sheer, color=oc, linewidth=1.8, alpha=0.95,
                solid_capstyle="round", label="Hull outline (gunwale)")
        # Close the silhouette down to the keel at bow & stern.
        ax.plot([x[0], x[0]], [0.0, float(sheer[0])],
                color=oc, linewidth=1.8, alpha=0.95)
        ax.plot([x[-1], x[-1]], [0.0, float(sheer[-1])],
                color=oc, linewidth=1.8, alpha=0.95)
        ax.axhline(0.0, color=oc, linewidth=1.0, alpha=0.45)   # keel line
    is_male = str(mold).lower().startswith("m")
    if is_male and not ax.yaxis_inverted():
        ax.invert_yaxis()
    return is_male


def plot_pour_zones(x, layout, filename=None, na_line=None,
                    pour_line=None, sheer_line=None, mixes_pcf=None,
                    zone_pour_lines=None, zone_densities=None,
                    mold="female", show_outline=True, outline_color="white"):
    """
    layout     : (n, 2) int array [mix_bot, mix_top] per station.
    pour_line  : optional per-station hand-off height [in above keel]
                 (NaN = single mix through the depth).  When given together
                 with sheer_line the plot uses REAL section heights so the
                 team can read exactly where to switch mixes; otherwise the
                 legacy schematic bands are drawn.
    sheer_line : per-station section top height [in above keel].
    na_line    : per-station neutral-axis height [in above keel].
    mixes_pcf  : optional mix densities for the legend labels.
    zone_pour_lines : optional list of (n,) per-station pour-line heights
                 [in above keel] for the multi-zone (I-beam) strategy — one
                 curve per interior boundary, bottom→top.  When present the
                 plot draws N stacked density bands instead of the 2-zone
                 hand-off.
    zone_densities  : (N,) mix densities bottom→top for the multi-zone bands.
    mold       : "female" (default, keel down) or "male" (keel up / inverted) —
                 orients the chart to match the physical mould so the team
                 reads the pour order the right way up.  See
                 ``_pour_outline_and_mold``.
    show_outline : draw the hull side-profile silhouette (default True).
    """
    fig = Figure(figsize=(8, 2.6), facecolor="#1a1a2e")
    ax = fig.add_subplot(111)
    ax.set_facecolor("#1a1a2e")
    _mold_tag = ("male mould — keel up (inverted)"
                 if str(mold).lower().startswith("m")
                 else "female mould — keel down")

    colors = {0: "#e74c3c", 1: "#2ecc71", 2: "#3498db", 3: "#f39c12"}

    def _label(mid):
        tier = {0: "Heavy/Structural", 1: "Lightweight", 2: "Ultra-light"}
        base = f"Mix {mid + 1} ({tier.get(mid, '')})"
        if mixes_pcf is not None and mid < len(mixes_pcf):
            base = f"Mix {mid + 1} ({mixes_pcf[mid]:.0f} pcf)"
        return base

    # ── Multi-zone (I-beam) mode: N stacked bands at real heights ─────────
    if zone_pour_lines is not None and sheer_line is not None:
        x = np.asarray(x); sheer = np.asarray(sheer_line, dtype=float)
        wid = np.gradient(x)
        # Boundary heights per station: keel(0) … interior lines … sheer
        bounds = [np.zeros_like(sheer)]
        bounds += [np.asarray(z, dtype=float) for z in zone_pour_lines]
        bounds.append(sheer)
        dens = list(zone_densities or [])
        for k in range(len(bounds) - 1):
            zb, zt = bounds[k], bounds[k + 1]
            col = colors.get(k, "#888")
            lbl = (f"Zone {k + 1} ({dens[k]:.0f} pcf)" if k < len(dens)
                   else f"Zone {k + 1}")
            ax.bar(x, np.maximum(zt - zb, 0.0), width=wid, bottom=zb,
                   color=col, align="center", linewidth=0, alpha=0.9, label=lbl)
        for z in zone_pour_lines:
            ax.plot(x, z, color="white", linewidth=1.2, alpha=0.85)
        if na_line is not None:
            ax.plot(x, na_line, color="#00d2ff", linewidth=1.0,
                    linestyle="--", alpha=0.8, label="Composite neutral axis")
        ax.set_title(f"Multi-Zone Pour (I-Beam) — mix bands above keel  "
                     f"({_mold_tag})",
                     fontsize=12, fontweight="bold", color="white")
        ax.set_xlabel("Distance from Bow (ft)", color="white")
        ax.set_ylabel("Height above keel (in)", color="white")
        ax.set_xlim(0, max(x)); ax.set_ylim(0, float(np.nanmax(sheer)) * 1.08)
        ax.tick_params(colors="gray")
        ax.grid(True, linestyle="--", alpha=0.15, color="gray")
        _pour_outline_and_mold(ax, x, sheer, mold, show_outline, outline_color)
        ax.legend(bbox_to_anchor=(1.02, 1), loc="upper left", fontsize=8,
                  facecolor="#222", edgecolor="#555", labelcolor="white")
        fig.tight_layout()
        if filename:
            fig.savefig(filename, format="pdf", dpi=300, bbox_inches="tight")
        return fig

    # ── Real-height mode (fix plan C3): exact pour heights in inches ──────
    if (pour_line is not None and sheer_line is not None
            and na_line is not None and layout.ndim == 2):
        x = np.asarray(x); sheer = np.asarray(sheer_line, dtype=float)
        split = np.asarray(pour_line, dtype=float).copy()
        # Single-mix stations: draw the bottom mix through the full depth
        split[~np.isfinite(split)] = sheer[~np.isfinite(split)]

        # Per-station bars (contiguous bands, no where-mask gaps)
        wid = np.gradient(x)
        shown = set()
        for mid in sorted(set(layout[:, 0]) | set(layout[:, 1])):
            col = colors.get(mid, "#888")
            mb = layout[:, 0] == mid
            if np.any(mb):
                ax.bar(x[mb], split[mb], width=wid[mb], bottom=0.0,
                       color=col, align="center", linewidth=0, alpha=0.9,
                       label=(_label(mid) if mid not in shown else None))
                shown.add(mid)
            mt = (layout[:, 1] == mid) & (split < sheer - 1e-9)
            if np.any(mt):
                ax.bar(x[mt], (sheer - split)[mt], width=wid[mt],
                       bottom=split[mt], color=col, align="center",
                       linewidth=0, alpha=0.9,
                       label=(_label(mid) if mid not in shown else None))
                shown.add(mid)

        has_line = np.isfinite(np.asarray(pour_line, dtype=float))
        if np.any(has_line):
            ax.plot(x, np.where(has_line, split, np.nan), color="white",
                    linewidth=1.6, label="Pour hand-off height")
        ax.plot(x, na_line, color="#00d2ff", linewidth=1.0, linestyle="--",
                alpha=0.8, label="Neutral axis")

        ax.set_title(f"Pour Zones — mix hand-off height above keel  "
                     f"({_mold_tag})",
                     fontsize=12, fontweight="bold", color="white")
        ax.set_xlabel("Distance from Bow (ft)", color="white")
        ax.set_ylabel("Height above keel (in)", color="white")
        ax.set_xlim(0, max(x))
        ax.set_ylim(0, float(np.nanmax(sheer)) * 1.08)
        ax.tick_params(colors="gray")
        ax.grid(True, linestyle="--", alpha=0.15, color="gray")
        _pour_outline_and_mold(ax, x, sheer, mold, show_outline, outline_color)
        ax.legend(bbox_to_anchor=(1.02, 1), loc="upper left", fontsize=8,
                  facecolor="#222", edgecolor="#555", labelcolor="white")
        fig.tight_layout()
        if filename:
            fig.savefig(filename, format="pdf", dpi=300, bbox_inches="tight")
        return fig

    # ── Legacy schematic mode ──────────────────────────────────────────────
    labels = {0: "Mix 1 (Heavy/Structural)", 1: "Mix 2 (Lightweight)"}

    if layout.ndim == 2:
        for mid in [0, 1]:
            mask = layout[:, 0] == mid
            if np.any(mask):
                ax.fill_between(x, 0, 0.5, where=mask,
                                color=colors[mid], step="mid")
        for mid in [0, 1]:
            mask = layout[:, 1] == mid
            if np.any(mask):
                ax.fill_between(x, 0.5, 1.0, where=mask,
                                color=colors[mid], label=labels[mid], step="mid")
        ax.axhline(0.5, color="white", linewidth=0.8, alpha=0.3, linestyle=":")
    else:
        for mid in [0, 1]:
            mask = layout == mid
            if np.any(mask):
                ax.fill_between(x, 0, 1, where=mask,
                                color=colors[mid], label=labels[mid], step="mid")

    ax.set_title(f"2-Mix Pour Strategy (Stress-Ranked)  ({_mold_tag})",
                 fontsize=12, fontweight="bold", color="white")
    ax.set_xlabel("Distance from Bow (ft)", color="white")
    ax.set_yticks([0.25, 0.75])
    ax.set_yticklabels(["Bottom", "Top"], color="gray", fontsize=7)
    ax.set_xlim(0, max(x)); ax.set_ylim(0, 1)
    ax.tick_params(axis='x', colors="gray")
    # No real section heights here (normalised Bottom/Top), so only the mould
    # flip applies — invert so "Bottom" (keel) sits on top for a male mould.
    if str(mold).lower().startswith("m"):
        ax.invert_yaxis()
    ax.legend(bbox_to_anchor=(1.02, 1), loc="upper left", fontsize=8,
              facecolor="#222", edgecolor="#555", labelcolor="white")
    fig.tight_layout()
    if filename:
        fig.savefig(filename, format="pdf", dpi=300, bbox_inches="tight")
    return fig


def plot_utilization(x, util, filename=None, fos=None):
    """
    Demand/Capacity (D/C) utilization along the hull (fix plan C4).

    util : dict with per-station arrays "bot_t", "top_t" (tension — the
           gated checks) and optionally "bot_c", "top_c" (compression —
           advisory).  1.0 = exactly at the design limit at the configured
           FOS (the FOS already lives inside the capacity).
    fos  : factor of safety baked into the allowable capacity, shown on the
           chart so a 0.9 D/C is not misread as "barely safe".  None reads the
           live config value.
    """
    if fos is None:
        try:
            import config as _cfg
            fos = float(getattr(_cfg, "FACTOR_OF_SAFETY", 1.5))
        except Exception:
            fos = 1.5
    fig = Figure(figsize=(8, 3.2), facecolor="#1a1a2e")
    ax = fig.add_subplot(111)
    ax.set_facecolor("#1a1a2e")

    curves = [
        ("bot_t", "#e74c3c", "-",  "Bottom fiber tension (sagging)"),
        ("top_t", "#f39c12", "-",  "Top fiber tension (hogging)"),
        ("bot_c", "#3498db", "--", "Bottom compression (advisory)"),
        ("top_c", "#9b59b6", "--", "Top compression (advisory)"),
    ]
    peak = 0.0
    for key, color, ls, label in curves:
        u = util.get(key) if util else None
        if u is None:
            continue
        ax.plot(x, u, color=color, linewidth=1.8 if ls == "-" else 1.2,
                linestyle=ls, label=label,
                alpha=1.0 if ls == "-" else 0.7)
        peak = max(peak, float(np.max(u)))

    ax.axhline(1.0, color="white", linewidth=1.2, alpha=0.8)
    ax.text(0.01 * max(x), 1.02,
            f"Allowable Limit = 1.0  (already includes {fos:.1f}× FoS)",
            color="white", fontsize=7, alpha=0.8)
    ax.axhspan(0.85, 1.0, color="#2ecc71", alpha=0.08)

    # Mark the governing (peak tension) station
    ut = np.maximum(util.get("bot_t", 0.0), util.get("top_t", 0.0)) \
        if util and util.get("bot_t") is not None else None
    if ut is not None:
        i = int(np.argmax(ut))
        ax.plot([x[i]], [ut[i]], "o", color="white", ms=5)
        ax.annotate(f"peak {ut[i]:.2f} @ {x[i]:.1f} ft",
                    (x[i], ut[i]), textcoords="offset points",
                    xytext=(6, 6), color="white", fontsize=8)

    ax.set_title("Utilization — Demand / Allowable Capacity", fontsize=12,
                 fontweight="bold", color="white")
    ax.set_xlabel("Distance from Bow (ft)", color="white")
    ax.set_ylabel("Utilization (Allowable Limit)", color="white")
    ax.set_xlim(0, max(x))
    ax.set_ylim(0, max(1.15, peak * 1.1))
    ax.tick_params(colors="gray")
    ax.grid(True, linestyle="--", alpha=0.2, color="gray")
    ax.legend(fontsize=8, facecolor="#222", edgecolor="#555",
              labelcolor="white", loc="best")
    # FoS transparency note: the "1.0" limit is the ALLOWABLE, not ultimate —
    # a 0.9 D/C already sits below a capacity reduced by the factor of safety.
    fig.text(0.5, 0.005,
             f"*Capacity includes a {fos:.1f} Factor of Safety from Ultimate "
             "Strength (D/C ≤ 1.0 = safe)",
             ha="center", color="#9aa0b5", fontsize=7.5, style="italic")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    if filename:
        fig.savefig(filename, format="pdf", dpi=300)
    return fig


def plot_shear_envelope(x, V_data, filename=None, cases=None):
    """
    Plots a dual-sign shear envelope showing max positive and negative
    shear forces across all race conditions.

    cases : optional iterable of {"envelope", "race_4", "race_3", "race_2"}
            selecting which curves to draw (None = all available).
    """
    fig = Figure(figsize=(8, 3), facecolor="#1a1a2e")
    ax = fig.add_subplot(111)
    ax.set_facecolor("#1a1a2e")

    V_max = V_data["max_pos"]
    V_min = V_data["max_neg"]
    V_4 = V_data["race_4"]
    V_2 = V_data["race_2"]
    V_3 = V_data.get("race_3")
    show = set(cases) if cases is not None else {
        "envelope", "race_4", "race_3", "race_2"}

    if "race_4" in show and V_4 is not None:
        ax.plot(x, V_4, color="#e74c3c", linewidth=2, label="4-Person Race")
        ax.fill_between(x, V_4, 0, color="#e74c3c", alpha=0.05)

    if "race_3" in show and V_3 is not None:
        ax.plot(x, V_3, color="#2ecc71", linewidth=2, label="3-Person Race")
        ax.fill_between(x, V_3, 0, color="#2ecc71", alpha=0.05)

    if "race_2" in show and V_2 is not None:
        ax.plot(x, V_2, color="#f39c12", linewidth=2, label="2-Person Race")
        ax.fill_between(x, V_2, 0, color="#f39c12", alpha=0.05)

    if "envelope" in show:
        ax.fill_between(x, V_max, V_min, color="#00d2ff", alpha=0.15,
                        label="Total Shear Envelope")
        ax.plot(x, V_max, color="#00d2ff", linewidth=1, linestyle="--", alpha=0.5)
        ax.plot(x, V_min, color="#00d2ff", linewidth=1, linestyle="--", alpha=0.5)

    ax.axhline(0, color="white", linewidth=0.8, alpha=0.4)

    ax.set_title("Shear Force Envelope", fontsize=12, fontweight="bold", color="white")
    ax.set_xlabel("Distance from Bow (ft)", color="white")
    ax.set_ylabel("Shear Force (lb)", color="white")
    ax.set_xlim(0, max(x))

    limit = max(np.max(np.abs(V_max)), np.max(np.abs(V_min)), 1.0)
    ax.set_ylim(-limit * 1.2, limit * 1.2)

    ax.tick_params(colors="gray")
    ax.grid(True, linestyle="--", alpha=0.2, color="gray")
    ax.legend(fontsize=8, facecolor="#222", edgecolor="#555",
              labelcolor="white", loc="best")
    fig.tight_layout()

    if filename:
        fig.savefig(filename, format="pdf", dpi=300)
    return fig


def plot_stress_envelope(
    x,
    M_envelope,
    I_in4,
    c_top,
    c_bot,
    allowable_c_psi,
    allowable_t_psi,
    filename=None,
    fos=None,
):
    """
    Per-station peak bending-stress chart with allowable-stress bands and
    a utilization ratio panel.  Worst station is highlighted in yellow.
    """
    import numpy as _np

    if fos is None:
        try:
            import config as _cfg
            fos = float(getattr(_cfg, "FACTOR_OF_SAFETY", 1.5))
        except Exception:
            fos = 1.5

    I_safe = _np.maximum(I_in4, 1e-6)
    M_lb_in = M_envelope * 12.0

    sigma_top = M_lb_in * c_top / I_safe
    sigma_bot = M_lb_in * c_bot / I_safe

    util_c = sigma_top / _np.maximum(allowable_c_psi, 1e-3)
    util_t = sigma_bot / _np.maximum(allowable_t_psi, 1e-3)
    util   = _np.maximum(util_c, util_t)

    worst_idx = int(_np.argmax(util))
    worst_x   = float(x[worst_idx])
    worst_u   = float(util[worst_idx])
    worst_sig = float(max(sigma_top[worst_idx], sigma_bot[worst_idx]))

    fig = Figure(figsize=(8, 6), facecolor="#1a1a2e")
    ax_s = fig.add_subplot(2, 1, 1)
    ax_u = fig.add_subplot(2, 1, 2, sharex=ax_s)

    for ax in (ax_s, ax_u):
        ax.set_facecolor("#1a1a2e")
        ax.tick_params(colors="gray")
        ax.grid(True, linestyle="--", alpha=0.2, color="gray")

    ax_s.plot(x, sigma_top, color="#e74c3c", linewidth=2, label="σ_top (compression)")
    ax_s.plot(x, sigma_bot, color="#3498db", linewidth=2, label="σ_bot (tension)")
    ax_s.fill_between(x, allowable_c_psi, color="#e74c3c", alpha=0.08,
                      label=f"Allowable Limit — compressive (Ultimate / {fos:.1f} FoS)")
    ax_s.fill_between(x, allowable_t_psi, color="#3498db", alpha=0.08,
                      label=f"Allowable Limit — tensile (Ultimate / {fos:.1f} FoS)")
    ax_s.axvline(worst_x, color="yellow", linewidth=1.0, linestyle=":", alpha=0.7)
    ax_s.set_ylabel("Bending Stress (psi)", color="white")
    ax_s.set_title("Per-Station Peak Bending Stress", color="white",
                   fontsize=12, fontweight="bold")
    ax_s.legend(fontsize=8, facecolor="#222", edgecolor="#555",
                labelcolor="white", loc="upper right")

    bar_w = (x[1] - x[0]) * 0.85 if len(x) > 1 else 0.5
    bar_colors = _np.where(util > 1.0, "#e74c3c",
                 _np.where(util > 0.75, "#f39c12", "#2ecc71"))
    for xi, ui, ci in zip(x, util, bar_colors):
        ax_u.bar(xi, ui, width=bar_w, color=ci, alpha=0.85, linewidth=0)
    ax_u.axhline(1.0, color="white", linewidth=1.2, linestyle="--", alpha=0.8,
                 label=f"Allowable Limit (util=1.0, incl. {fos:.1f} FoS)")
    ax_u.axvline(worst_x, color="yellow", linewidth=1.0, linestyle=":",
                 label=f"Worst: x={worst_x:.1f} ft  util={worst_u:.3f}")
    ax_u.set_ylabel("Utilization (Allowable Limit)", color="white")
    ax_u.set_xlabel("Distance from Bow (ft)", color="white")
    ax_u.legend(fontsize=8, facecolor="#222", edgecolor="#555",
                labelcolor="white", loc="upper right")
    ax_u.set_xlim(0, float(max(x)) if len(x) else 1)
    ax_u.set_ylim(0, max(float(_np.max(util)) * 1.2, 1.3))
    ax_u.annotate(
        f"WORST\n{worst_sig:.0f} psi\nutil={worst_u:.3f}",
        xy=(worst_x, worst_u),
        xytext=(worst_x + (float(max(x)) - float(min(x))) * 0.04, worst_u * 1.05),
        color="yellow", fontsize=7,
        arrowprops=dict(arrowstyle="->", color="yellow", lw=0.8),
    )

    fig.tight_layout(pad=1.2)
    if filename:
        fig.savefig(filename, format="pdf", dpi=300, bbox_inches="tight")
    return fig
