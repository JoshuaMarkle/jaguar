# services/godzilla_io.py
"""Generates a clean Godzilla (mlt933w.exe) input file from dynamic hull parameters.

Writes explicit CRLF line endings (binary mode) so no hidden carriage returns
can be introduced regardless of the host OS or editor settings.
"""
from errors import domain_only


def generate_godzilla_input(
    filename='in.mlt',
    L=5.1,
    beam=0.6,
    T=0.12,
    vol=0.098,
    factors=None,
    n_ships=40,
    water_density=1025.9,
    kinematic_viscosity=1.18831,
    L_min=None,
    L_max=None,
    beam_min=None,
    beam_max=None,
    T_min=None,
    T_max=None,
    total_mass_kg=None,
    rng_seed=12345,
    design_speeds=None,
    design_weights=None,
    box_enabled=False,
    box_L=1.5,
    box_B=0.5,
    box_T=0.3,
    box_location="Midships",
    min_gmt=0.05,
    max_gmt=10.0,
    objective_code=3,
):
    """Write a Godzilla / Michlet input file with exact CRLF line endings.

    Parameters
    ----------
    filename             : path to write (default 'in.mlt')
    L                    : hull length (m)
    beam                 : hull beam (m)
    T                    : hull draft (m)
    vol                  : displacement volume (m³) — used as seed and maximum displacement
    factors              : list of 7 Series-7 shape-factor seeds (floats)
    n_ships              : number of Godzilla population ships (min 3, max 120)
    water_density        : water density (kg/m³; min 995.0, max 1030.0)
    kinematic_viscosity  : kinematic viscosity (×10⁻⁶ m²/s; min 0.5, max 2.0)
    L_min / L_max        : explicit hull length search bounds (m); defaults to ±20% of L
    beam_min / beam_max  : explicit beam search bounds (m); defaults to ±30% of beam
    T_min / T_max        : explicit draft search bounds (m); defaults to ±50% of T
    total_mass_kg        : total mass to float (kg); minimum displacement floor equals
                           total_mass_kg / water_density.  Falls back to vol when not provided.
    rng_seed             : Godzilla random seed (0–32000)
    design_speeds        : list of up to 3 design speeds in m/s; zero-speed entries ignored
    design_weights       : list of corresponding weightings (must be parallel to design_speeds)
    box_enabled          : whether to enforce internal paddler box constraint
    box_L / box_B / box_T: paddler box length / beam / draft (m)
    box_location         : "Midships", "LCB", or "Stern"
    """
    if factors is None:
        factors = [0.5] * 7

    # Build active design-speed pairs (non-zero weight and non-zero speed)
    if design_speeds is None:
        design_speeds = [3.0, 4.5, 0.0]
    if design_weights is None:
        design_weights = [1.0, 0.5, 0.0]
    active_pairs = [
        (s, w)
        for s, w in zip(design_speeds, design_weights)
        if s > 0.0 and w > 0.0
    ]
    if not active_pairs:
        active_pairs = [(3.0, 1.0)]

    # Calculation speed range: span from half the slowest to 1.25× the fastest
    _speeds_only = [s for s, _ in active_pairs]
    min_speed = max(0.01, min(_speeds_only) * 0.50)
    max_speed = max(_speeds_only) * 1.25
    n_speeds = 17

    # Code 13 requires variant 1; all others use variant 0
    _variant_map = {13: 1}
    variant = _variant_map.get(objective_code, 0)

    # Box constraint location code (Michlet/Godzilla internal numbering)
    _loc_map = {"Midships": 2, "LCB": 3, "Stern": 4}
    _box_loc_code = _loc_map.get(box_location, 2)

    if L_min is None:
        L_min = L * 0.80
    if L_max is None:
        L_max = L * 1.20
    if beam_min is None:
        beam_min = beam * 0.70
    if beam_max is None:
        beam_max = beam * 1.30
    if T_min is None:
        T_min = T * 0.50
    if T_max is None:
        T_max = T * 1.50

    # Minimum displacement: physics floor derived from total mass to float.
    # Ensures Godzilla never proposes a hull that would sink under the required load.
    if total_mass_kg is not None:
        min_disp_vol = total_mass_kg / water_density
    else:
        min_disp_vol = vol

    # Michlet's internal quadrature over the 41×41 offset grid introduces
    # ~±0.0005 m³ round-trip error vs. the .3f file format, and Godzilla's
    # population initialiser adds another ~±0.001 m³.  A zero-width [vol, vol]
    # window causes an immediate constraint-violation crash before the GA starts.
    # ±0.002 m³ is the minimum safe padding for canoe-scale hulls at this grid.
    _DISP_TOL = 0.002  # m³
    max_disp_vol = vol + _DISP_TOL
    # Pull the floor down only when it is not already below vol (i.e. when the
    # physics floor is looser than the tight-lock case).
    if min_disp_vol >= vol:
        min_disp_vol = max(min_disp_vol - _DISP_TOL, 0.0)

    lines = [
        "# ======================================================================",
        "#",
        "# Godzilla Kayak Example: Sprint Kayak with negative LCB",
        "#",
        "# =================== INPUT FILE TYPE AND SUBTYPE ======================",
        "# Input File Type (0=Standard)",
        "0",
        "# Input File Subtype (0=Standard)",
        "0",
        "# =================== OUTPUT FILE TYPE AND SUBTYPE =====================",
        "# Output File Type (0=Standard)",
        "0",
        "# Output File Subtype (0=Standard)",
        "0",
        "# ====================== COURSE AND VESSEL TYPE ========================",
        "# Course Particulars (0=None)",
        "0",
        "# Number of Hulls (1, 2,..., or 5)",
        "1",
        "# ======================== PHYSICAL QUANTITIES =========================",
        "# Gravitational Acceleration (m/sec/sec) (min 9.6, max 9.9)",
        "9.80665",
        "# ========================= WATER PROPERTIES ===========================",
        "# Water Density (kg/cubic metre) (min 995.0, max 1030.0)",
        f"{water_density:.5f}",
        "# Water Kin. Viscosity (sq. m/sec * 10^-6) (min 0.5, max 2.0)",
        f"{kinematic_viscosity:.5f}",
        "# Base Eddy Kin. Viscosity (non-dimensional, min 1.0)",
        "10.0",
        "# Water Depth (metres) (max=10000.0)",
        "10000.0",
        "# ========================== AIR PROPERTIES ============================",
        "# Air Density (kg/cubic metre) (min 0.9, max 2.0)",
        "1.226",
        "# Air Kin. Viscosity (sq. m/sec * 10^-6) (min 10.0, max 20.0)",
        "14.4",
        "# Wind Speed (m/sec)",
        "0.0",
        "# Wind Direction (degrees)",
        "0.0",
        "# ======================= CALCULATION PARAMETERS =======================",
        "# Minimum Speed (m/sec) (min 0.01, max 51.9)",
        f"{min_speed:.5f}",
        "# Maximum Speed (m/sec) (max 52.0)",
        f"{max_speed:.5f}",
        "# Number of Speeds (min 2, max 101)",
        f"{n_speeds}",
        "# Leeway Parameters (0=None)",
        "0",
        "# Wave Drag Ntheta",
        "128",
        "# Skin Friction Method (0=None, 1=ITTC1957, 2=Grigson)",
        "1",
        "# Viscous Form Factor Method (0=None, 3=Dual)",
        "3",
        "# Viscous Drag Form Factor",
        "1.0",
        "# Wave Drag Form Factor",
        "1.0",
        "# Pressure Signature Method (0=None,1=Slender body)",
        "1",
        "# ==================== SHIP CALCULATION PARAMETERS =====================",
        "# Number of Offset Stations (rows) (odd integer: min 5, max 81)",
        "41",
        "# Number of Offset Waterlines (columns) (odd integer: min 5, max 81)",
        "41",
        "# Ship Loading Type",
        "3",
        "# Ship Loading Formula Parameters",
        "1.0,0.0,0.0",
        "# ===================== WAVE ELEVATION PARAMETERS ======================",
        "# Sectorial Cuts and Patches",
        "# R0",
        "40.0",
        "# R1",
        "140.0",
        "# Beta",
        "22.5",
        "# Nr",
        "10",
        "# Nbeta",
        "10",
        "# Rectangular Cuts and Patches",
        "# x0",
        "40.0",
        "# x1",
        "140.0",
        "# y0",
        "-50.0",
        "# y1",
        "50.0",
        "# Nwx",
        "101",
        "# Nwy",
        "101",
        "# Beaches and Walls",
        "# x0",
        "40.0",
        "# x1",
        "140.0",
        "# y0",
        "30.0",
        "# z0",
        "-1.0",
        "# z1",
        "0.0",
        "# Slope",
        "90.0",
        "# Nbx",
        "101",
        "# Nbz",
        "101",
        "# ============================ FIRST HULL ==============================",
        "# Offsets",
        "1, 1.0, 1.0, 0.0",
        "# Displacement Volume (cubic metres)",
        f"{vol:.5f}",
        "# Length (metres)",
        f"{L:.4f}",
        "# Draft (metres)",
        f"{T:.4f}",
        "# Longitudinal Separation (metres) (0.0 for a monohull)",
        "0.0",
        "# Lateral Separation Distance (metres) (0.0 for a monohull)",
        "0.0",
        "# Loading Type for this hull",
        "3",
        "# Loading Formula Parameters",
        "1.0,0.0,0.0",
        "# Trim Method",
        "0",
        "# Trim: Number of speeds ( >= 2)",
        "2",
        "# Trim: speed, angle",
        "0.0,0.0",
        "40.0,0.0",
        "# Sinkage Method",
        "0",
        "# Sinkage: Number of speeds ( >= 2)",
        "2",
        "# Sinkage: speed, amount",
        "0.0,0.0",
        "40.0,0.0",
        "# Heel Method",
        "0",
        "# Heel: Number of speeds ( >= 2)",
        "2",
        "# Heel: speed, angle",
        "0.0,0.0",
        "40.0,0.0",
        "# Appendages (0=None)",
        "0",
        "# Other Particulars (0=None)",
        "0",
        "# ======================== GODZILLA INPUT DATA =========================",
        "# Random Seed (integer: min 0, max 32000)",
        f"{rng_seed}",
        "# Number of Ships (integer: min 3, max 120)",
        f"{n_ships}",
        "# Objective Function (integer: see Manual)",
        f"{objective_code}",
        "# Objective Function Variant(integer: see Manual)",
        f"{variant}",
        "# Number of Design Speeds (min 1, max 50)",
        f"{len(active_pairs)}",
        "# Design Speeds and Weightings (one pair per design speed)",
        *[f"{s:.2f},{w:.2f}" for s, w in active_pairs],
        "# Hull Shape Function (integer: see Manual)",
        "7",
        "# ========================== SHIP CONSTRAINTS ==========================",
        "# Minimum Displacement Overall",
        f"{min_disp_vol:.3f}",
        "# Maximum Displacement Overall",
        f"{max_disp_vol:.3f}",
        "# Minimum Length Overall",
        f"{L_min:.5f}",
        "# Maximum Length Overall",
        f"{L_max:.5f}",
        "# Minimum Width Overall",
        f"{beam_min:.5f}",
        "# Maximum Width Overall",
        f"{beam_max:.5f}",
        "# Minimum GMT Overall",
        "-100.0",
        "# Maximum GMT Overall",
        "100.0",
        "# Minimum GML Overall",
        "0.0",
        "# Maximum GML Overall",
        "10000.0",
        "# Ship Pressure Signature Constraints (0=None)",
        "0",
        "# Special Ship Constraints (See manual)",
        "0",
        "# ====================== FIRST HULL CONSTRAINTS ========================",
        "# Minimum Displacement",
        f"{min_disp_vol:.3f}",
        "# Maximum Displacement",
        f"{max_disp_vol:.3f}",
        "# Minimum Length (refer also to Min LOA and Max LOA)",
        f"{L_min:.5f}",
        "# Maximum Length (refer also to Min LOA and Max LOA)",
        f"{L_max:.5f}",
        "# Minimum Draft",
        f"{T_min:.5f}",
        "# Maximum Draft",
        f"{T_max:.5f}",
        "# Minimum Longitudinal Offset (Always 0.0 for Hull 1)",
        "0.0",
        "# Maximum Longitudinal Offset (Always 0.0 for Hull 1)",
        "0.0",
        "# Minimum Lateral Offset (Always 0.0 for Hull 1)",
        "0.0",
        "# Maximum Lateral Offset (Always 0.0 for Hull 1)",
        "0.0",
        "# Shape Factors: Minimum, Maximum, Seed.",
        f"0.0, 3.0,  {factors[0]:.5f}",
        f"0.0, 3.0,  {factors[1]:.5f}",
        f"0.0, 3.0,  {factors[2]:.5f}",
        f"0.0, 3.0,  {factors[3]:.5f}",
        f"0.0, 3.0,  {factors[4]:.5f}",
        f"0.0, 0.75, {factors[5]:.5f}",
        f"0.0, 0.75, {factors[6]:.5f}",
        "# Minimum Beam (refer also to Min WOA and Max WOA)",
        f"{beam_min:.5f}",
        "# Maximum Beam (refer also to Min WOA and Max WOA)",
        f"{beam_max:.5f}",
        "# Minimum Surface Area (sq. metres)",
        "0.0",
        "# Maximum Surface Area (sq. metres)",
        "1000.0",
        "# Minimum Awp (sq. metres) (min 0.0)",
        "0.1",
        "# Maximum Awp",
        "1000.0",
        "# Minimum LCB",
        "-0.140",
        "# Maximum LCB",
        "-0.025",
        "# Minimum VCB",
        "0.065",
        "# Maximum VCB",
        "0.3",
        "# Minimum LCF",
        "-1000.0",
        "# Maximum LCF",
        "1000.0",
        "# Minimum BMT",
        "0.0",
        "# Maximum BMT",
        "10000.0",
        "# Minimum GMT",
        f"{min_gmt:.3f}",
        "# Maximum GMT",
        f"{max_gmt:.3f}",
        "# Minimum At",
        "0.0",
        "# Maximum At",
        "10000.0",
        "# Minimum Bt",
        "0.0",
        "# Maximum Bt",
        "100.0",
        "# Minimum Tt",
        "0.0",
        "# Maximum Tt",
        "100.0",
        "# Minimum Cb",
        "0.0",
        "# Maximum Cb",
        "1.0",
        "# Minimum Cm",
        "0.0",
        "# Maximum Cm",
        "1.0",
        "# Minimum Cp",
        "0.0",
        "# Maximum Cp",
        "1.0",
        "# Minimum Cwp",
        "0.0",
        "# Maximum Cwp",
        "1.0",
        "# Minimum L/B",
        "0.1",
        "# Maximum L/B",
        "100.0",
        "# Minimum L/T",
        "0.1",
        "# Maximum L/T",
        "100.0",
        "# Minimum B/T",
        "0.1",
        "# Maximum B/T",
        "100.0",
        "# Hull Pressure Signature Constraints (0=None)",
        "0",
        "# Special Hull Constraints (0=None)",
        (
            f"{_box_loc_code}, 1, {box_L:.3f}, {box_T:.3f}, {box_B:.3f}"
            if box_enabled else "0"
        ),
    ]

    content = "\r\n".join(lines) + "\r\n"
    try:
        with open(filename, 'wb') as fh:
            fh.write(content.encode('ascii'))
    except Exception as e:
        domain_only(e)   # a bug is not a skippable condition
        print(f"Error writing Godzilla input: {e}")
