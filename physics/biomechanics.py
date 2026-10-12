# physics/biomechanics.py
"""
Vertical centre of gravity of a KNEELING paddler, from segmented anthropometry.

WHY THIS EXISTS
---------------
Loaded transverse stability is set by the crew, not by the concrete: a ~640 lb
crew against a ~160 lb hull means the paddlers' CG largely IS the combined KG.
The suite previously assumed a flat 12 in above the keel.  Measured against the
sensitivity that assumption carries -- loaded GM_T moves 0.8 in for every inch
of crew CG -- a bare guess was the largest single source of error in the
stability figure, larger by orders of magnitude than anything the mix
distribution could contribute (which measures at 0.00 in).

THE MODEL
---------
Two segments, because in a kneel only one of them rotates:

    KG_paddler = f_legs * h_legs  +  f_HAT * (h_hip + L_HAT * sin(theta))

  f_legs, f_HAT  mass fractions of the static lower body and of the rotating
                 head-arms-trunk (HAT) group
  h_legs         CG height of the static lower body above the hull's inner floor
  h_hip          hip-joint height above the inner floor -- THE DOMINANT INPUT
  L_HAT          hip-joint to HAT-CG distance along the torso axis
  theta          torso angle from HORIZONTAL (pi/2 = upright), RADIANS

Only the torso, head and arms rotate about the hip when a kneeling paddler
leans forward; the shins are flat on the floor and the thighs do not swing.  A
model that rotates everything above the KNEE (a common shortcut) is wrong for
this posture, and a model anchored at STANDING knee height (0.28*H) is wrong
twice over -- in a kneel the knee is on the floor, not 20 in above it.

POSTURE DOMINATES
-----------------
Two kneels are in play and they are ~8 in apart in hip height, which swamps
everything else in this module:

  Posture A  "heels-sitting" (seiza): hips resting back on the heels.
             h_hip ~ 0.10*H.
  Posture B  "upright kneel": hips off the heels, thighs near vertical.
             h_hip ~ thigh length ~ 0.245*H.

B is the default.  It is supported by the team's OWN measurement: canoe_suite
.cfg carries knee_frac_bending = 0.90 (weighed kneeling, knees vs toes) with
toe_offset_in = 17.0.  Resolving horizontal equilibrium about the knee contact,
heels-sitting places the combined CG ~11.5 in aft of the knee, which would put
~68% of the load on the toes; an upright kneel places it within ~1.3 in of the
knee, i.e. 90-100% on the knees.  The measurement matches B.  (n = 1 paddler --
see knee_frac_measured_n.)

Sweeping theta across its whole plausible range moves KG by only ~2 in, while
the posture choice moves it by ~8 in.  MEASURING OUR PADDLERS' ACTUAL KNEELING
HIP HEIGHT WOULD SUPERSEDE THIS ENTIRE MODULE.  That is the action item, not a
caveat: everything here is an anthropometric ESTIMATE standing in for one
tape-measure reading.

SOURCES
-------
Segment masses (sex-specific):
    Plagenhoef, S., Evans, F.G. and Abdelnour, T. (1983), "Anatomical Data for
    Analyzing Human Motion", Research Quarterly for Exercise and Sport, 54(2),
    169-178.  Sex-specific by design; Winter's Table 4.1 (after Dempster 1955)
    is male-derived and cannot supply the coed split.

Segment lengths as fractions of stature:
    Drillis, R. and Contini, R. (1966), "Body Segment Parameters", Office of
    Vocational Rehabilitation, New York.  As tabulated in Winter, D.A.,
    "Biomechanics and Motor Control of Human Movement", 4th ed., Fig. 4.1.

Cross-check (independent):
    ANSUR II (2012) kneeling height / stature ~ 0.709 for males.  This module's
    length data predicts thigh + trunk + head = 0.245 + 0.470 = 0.715 for an
    upright kneel -- agreement to 1%, which validates the length fractions.
"""
from __future__ import annotations

import math

# ── Segment mass fractions (Plagenhoef, Evans & Abdelnour 1983, Table 1) ─────
# HAT   = head + trunk + 2*(upper arm + forearm + hand)
# Legs  = 2*(thigh + shank + foot)
# Male:   head 8.26, trunk 46.84, upper arm 3.25, forearm 1.87, hand 0.65,
#         thigh 10.50, shank 4.75, foot 1.43  [% body mass]
# Female: head 8.20, trunk 45.00, upper arm 2.90, forearm 1.57, hand 0.50,
#         thigh 11.75, shank 5.35, foot 1.33  [% body mass]
# Female carries proportionally more mass in thigh and shank, which is why the
# female CG sits lower -- this is exactly the effect a sex-blind model loses.
SEGMENT_MASS_FRACTION = {
    "M": {"hat": 0.6664, "legs": 0.3336},
    "F": {"hat": 0.6314, "legs": 0.3686},
}

# ── Segment lengths as fractions of stature H (Drillis & Contini 1966) ───────
HIP_HEIGHT_STANDING_FRAC = 0.530     # greater trochanter
KNEE_HEIGHT_STANDING_FRAC = 0.285    # femoral condyle
ANKLE_HEIGHT_STANDING_FRAC = 0.039   # medial malleolus
THIGH_LENGTH_FRAC = (HIP_HEIGHT_STANDING_FRAC - KNEE_HEIGHT_STANDING_FRAC)
SHANK_LENGTH_FRAC = (KNEE_HEIGHT_STANDING_FRAC - ANKLE_HEIGHT_STANDING_FRAC)

# Standing CG height of the LEGS group, built from the segment table:
#   thigh CG at 0.433 of thigh length from the hip (proximal), mass 0.200
#   shank CG at 0.433 of shank length from the knee, mass 0.093
#   foot CG at roughly ankle height, mass 0.029
# giving (0.200*0.424 + 0.093*0.178 + 0.029*0.039) / 0.322 = 0.318 H.
LEGS_CG_STANDING_FRAC = 0.318

# ── Whole-body standing CG, as a fraction of stature ─────────────────────────
# Used ONLY to calibrate L_HAT (below).  These are the classic whole-body
# values; they are NOT segment CGs and must never be used as one -- a whole-body
# ratio already contains the legs' contribution, so applying it to the rotating
# segment double-counts.
WHOLE_BODY_CG_STANDING_FRAC = {"M": 0.565, "F": 0.545}

# ── Posture presets: hip-joint height above the hull floor, fraction of H ────
# A: heels-sitting (seiza) -- hips resting on the heels.
# B: upright kneel -- hips off the heels, thighs near vertical, so the hip rides
#    at approximately thigh length above the floor.
POSTURE_HIP_HEIGHT_FRAC = {"A_heels_sitting": 0.100, "B_upright_kneel": THIGH_LENGTH_FRAC}
POSTURE_LEGS_CG_FRAC = {"A_heels_sitting": 0.060, "B_upright_kneel": 0.0969}
DEFAULT_POSTURE = "B_upright_kneel"

# Plausible torso range for the sensitivity band [deg from horizontal].
TORSO_ANGLE_BAND_DEG = (45.0, 90.0)


def hat_cg_offset_frac(sex: str) -> float:
    """
    ``L_HAT`` as a fraction of stature: hip joint to HAT centre of gravity.

    CALIBRATED rather than asserted.  Solve for the offset that makes the model
    reproduce the published standing whole-body CG:

        R_cg * H = f_legs * (0.318 H) + f_HAT * (0.530 H + L_HAT)

    =>  L_HAT = (R_cg - f_legs*0.318) / f_HAT - 0.530     [x H]

    This is what the 0.565 / 0.545 whole-body ratios are legitimately FOR: they
    pin the model against a known aggregate, instead of being pressed into
    service as a segment CG they do not represent.  Yields 0.159 H male,
    0.148 H female.
    """
    s = _norm_sex(sex)
    f = SEGMENT_MASS_FRACTION[s]
    r_cg = WHOLE_BODY_CG_STANDING_FRAC[s]
    return ((r_cg - f["legs"] * LEGS_CG_STANDING_FRAC) / f["hat"]
            - HIP_HEIGHT_STANDING_FRAC)


def _norm_sex(sex) -> str:
    s = str(sex).strip().upper()[:1]
    if s not in ("M", "F"):
        raise ValueError(f"sex must be 'M' or 'F', got {sex!r}")
    return s


def paddler_kg_in(height_in, sex, torso_angle_deg, posture=DEFAULT_POSTURE,
                  hip_height_in=None, legs_cg_in=None, datum_offset_in=0.0):
    """
    One kneeling paddler's CG height [in], measured from ``datum_offset_in``.

    Parameters
    ----------
    height_in        stature [in].
    sex              'M' or 'F' -- selects the segment mass fractions.
    torso_angle_deg  torso angle from HORIZONTAL, DEGREES (90 = upright).
                     Converted to radians here; math.sin takes radians and
                     passing degrees is a silent
                     error that returns a plausible-looking number.
    posture          key into POSTURE_HIP_HEIGHT_FRAC; sets the hip datum when
                     hip_height_in is not given explicitly.
    hip_height_in    MEASURED hip-joint height above the hull floor.  Overrides
                     the posture preset.  This is the input worth measuring:
                     it dominates the result.
    legs_cg_in       measured static lower-body CG height; defaults from posture.
    datum_offset_in  height of the hull floor above the KEEL -- shell thickness
                     plus any knee pad.  GM_T is measured from the keel, so this
                     must be included or the crew sits one shell thickness too
                     low.
    """
    s = _norm_sex(sex)
    H = float(height_in)
    if H <= 0.0:
        raise ValueError(f"height_in must be positive, got {height_in}")
    if posture not in POSTURE_HIP_HEIGHT_FRAC:
        raise ValueError(f"unknown posture {posture!r}; "
                         f"expected one of {sorted(POSTURE_HIP_HEIGHT_FRAC)}")
    f = SEGMENT_MASS_FRACTION[s]
    h_hip = (float(hip_height_in) if hip_height_in is not None
             else POSTURE_HIP_HEIGHT_FRAC[posture] * H)
    h_legs = (float(legs_cg_in) if legs_cg_in is not None
              else POSTURE_LEGS_CG_FRAC[posture] * H)
    l_hat = hat_cg_offset_frac(s) * H
    # DEGREES -> RADIANS.  The single most likely silent defect in this file.
    theta = math.radians(float(torso_angle_deg))
    return (f["legs"] * h_legs
            + f["hat"] * (h_hip + l_hat * math.sin(theta))
            + float(datum_offset_in))


def crew_kg_in(heights_in, sexes, weights_lb, torso_angle_deg,
               posture=DEFAULT_POSTURE, hip_height_in=None,
               datum_offset_in=0.0):
    """
    Mass-weighted combined crew CG [in]:  sum(KG_i * W_i) / sum(W_i).

    Seating ARRANGEMENT is deliberately absent.  A mass-weighted mean over the
    whole crew is invariant to who sits where, so for crew KG the coed
    arrangement genuinely does not matter -- this is a property of the sum, not
    an unhandled case.  (Arrangement DOES matter for the bending envelope, where
    paddlers act at distinct x-stations; that is a separate concern and is not
    modelled here.)
    """
    hs = [float(h) for h in heights_in]
    ws = [float(w) for w in weights_lb]
    ss = list(sexes)
    if not (len(hs) == len(ws) == len(ss)):
        raise ValueError(f"crew arrays disagree: {len(hs)} heights, "
                         f"{len(ss)} sexes, {len(ws)} weights")
    if not hs:
        raise ValueError("empty crew")
    tot = sum(ws)
    if tot <= 0.0:
        raise ValueError(f"crew weight must be positive, got {tot}")
    num = sum(w * paddler_kg_in(h, s, torso_angle_deg, posture=posture,
                                hip_height_in=hip_height_in,
                                datum_offset_in=datum_offset_in)
              for h, s, w in zip(hs, ss, ws))
    return num / tot


def crew_kg_band_in(heights_in, sexes, weights_lb, datum_offset_in=0.0,
                    postures=("A_heels_sitting", "B_upright_kneel"),
                    torso_angle_band_deg=TORSO_ANGLE_BAND_DEG):
    """
    Crew CG across the plausible posture x torso-angle space.

    Returns ``[(posture, theta_deg, kg_in), ...]`` over the corners of the band.
    The corners are the point: the SPREAD between postures is the headline, and
    it is far wider than the spread across torso angle.  A single number here
    would imply a precision the inputs do not have.
    """
    out = []
    for p in postures:
        for th in torso_angle_band_deg:
            out.append((p, float(th),
                        crew_kg_in(heights_in, sexes, weights_lb, th,
                                   posture=p, datum_offset_in=datum_offset_in)))
    return out
