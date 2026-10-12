# race_course.py
"""
ASCE Concrete Canoe race course definitions.
Three official course types modelled from the ASCE race rules:
  • Sprint        (Fig 4)  – 200 m out-and-back
  • Slalom        (Fig 2)  – 100 m straight + buoy slalom return
  • Endurance     (Fig 3)  – slalom + 500 m endurance loop
Each course is a list of waypoints.  The simulator integrates a canoe
through the waypoint sequence, handling straights and turns.
"""
import numpy as np
from dataclasses import dataclass, field
from typing import List, Tuple


# ────────────────────────  DATA TYPES  ───────────────────────────
@dataclass
class Waypoint:
    """Single point on the course.

    The ``y`` field is the strict lateral coordinate.  For START / FINISH
    lines that span a lane (or any other waypoint where the canoe may
    cross anywhere along a y-range), set ``y_min`` and ``y_max`` to the
    bounds of the allowed zone — the path solver will then pick the
    optimal y inside ``[y_min, y_max]`` automatically.  ``y`` is kept as
    the lane-centre fallback (used by anything that needs a single point,
    e.g. plotting and ``RaceCourse.total_distance``).
    """
    x: float                    # metres, longitudinal (up the course)
    y: float                    # metres, lateral (across lanes)
    turn_radius: float = 0.0   # if >0 this is a buoy-turn waypoint (m)
    turn_angle: float = 0.0    # degrees of heading change at this point
    label: str = ""
    y_min: float | None = None  # lower bound of allowed y-range (inclusive)
    y_max: float | None = None  # upper bound of allowed y-range (inclusive)

    def __post_init__(self):
        if self.y_min is not None and self.y_max is not None:
            if self.y_min > self.y_max:
                raise ValueError(
                    f"Waypoint {self.label!r}: y_min ({self.y_min}) "
                    f"must be ≤ y_max ({self.y_max})"
                )

    @property
    def is_bounded(self) -> bool:
        """True if the waypoint defines a y-range rather than a strict y."""
        return self.y_min is not None and self.y_max is not None


@dataclass
class RaceCourse:
    """Ordered sequence of waypoints defining a course."""
    name: str
    waypoints: List[Waypoint] = field(default_factory=list)
    n_paddlers: int = 4        # crew size for this race

    # ── derived helpers ──────────────────────────────────────────
    def segments(self):
        """Yield (wp_start, wp_end, distance, segment_type) tuples."""
        for i in range(len(self.waypoints) - 1):
            a = self.waypoints[i]
            b = self.waypoints[i + 1]
            dist = np.hypot(b.x - a.x, b.y - a.y)
            seg_type = "turn" if b.turn_radius > 0 else "straight"
            yield a, b, dist, seg_type

    def total_distance(self):
        """Total path distance including turn arcs."""
        d = 0.0
        for a, b, dist, stype in self.segments():
            if b.turn_radius > 0 and b.turn_angle > 0:
                arc = b.turn_radius * np.radians(b.turn_angle)
                d += dist + arc
            else:
                d += dist
        return d


# ────────────────────  SPRINT COURSE (Fig 4)  ────────────────────
def make_sprint_course() -> RaceCourse:
    """
    200 m sprint: start → 100 m straight → 180° buoy turn → 100 m back.
    Lane 1 (y=0–15, center 7.5) outbound; Lane 2 (y=15–30, center 22.5) return.
    Turn center at y=15.0, radius 7.5m sweeps lane centre to lane centre.
    """
    wps = [
        Waypoint(0.0, 7.5, label="START"),
        Waypoint(92.5, 15.0, turn_radius=7.5, turn_angle=180.0, label="buoy turn"),
        Waypoint(0.0, 22.5, label="FINISH"),
    ]
    return RaceCourse(name="Sprint", waypoints=wps, n_paddlers=4)


# ────────────────────  SLALOM COURSE (Fig 2)  ────────────────────
def make_slalom_course() -> RaceCourse:
    """
    Slalom: 100 m snaking through buoys out, 180° left-turn into return lane,
    and a 100 m straight sprint back to the finish line.
    """
    # Assuming Cartesian coordinates: +Y is "Left" of the forward vector, -Y is "Right"
    RIGHT = -1.0
    LEFT = 1.0
    
    # Starting zone bounds (0 to 15m)
    start_y_min = 0.0
    start_y_max = 15.0
    nominal_start_y = 7.5  # Midpoint for UI default
    
    # Finish zone bounds (15 to 30m)
    finish_y_min = 15.0
    finish_y_max = 30.0
    nominal_finish_y = 22.5  # Midpoint for UI default
    
    wps = [
        # Floating START line
        Waypoint(0.0, nominal_start_y, y_min=start_y_min, y_max=start_y_max, label="START"),
    ]

    # Slalom buoy x-positions
    slalom_xs = [10.0, 20.0, 30.0, 50.0, 60.0, 70.0, 80.0]
    
    # Buoy y-positions explicitly locked to 15.0 and 7.5
    buoy_ys = [7.5,15.0, 7.5, 15.0, 7.5, 15.0, 7.5]
    
    # If the buoy is at 15.0 (top), we turn Right (under it). 
    # If the buoy is at 7.5 (bottom), we turn Left (over it).
    turn_sequence = [RIGHT, LEFT, RIGHT, LEFT, RIGHT, LEFT, RIGHT]
    
    for sx, sy, direction in zip(slalom_xs, buoy_ys, turn_sequence):
        # Offset the apex by 2m from the buoy to give the canoe physical width clearance
        gate_y = sy + (direction * 2.0) 
        turn_name = "Right" if direction == RIGHT else "Left"
        
        wps.append(Waypoint(sx, gate_y,
                            turn_radius=2.5,  # Reduced from 5.0 to prevent overlapping circles
                            turn_angle=35.0, 
                            label=f"slalom buoy {sx}m ({turn_name})"))

    wps.append(Waypoint(87.5, 7.5, label="align straight"))
    wps.append(Waypoint(92.5, 15.0, turn_radius=7.5, turn_angle=180.0, label="100m 180-degree turn"))
    wps.append(Waypoint(0.0, 22.5, label="FINISH"))

    return RaceCourse(name="Slalom", waypoints=wps, n_paddlers=4)


# ───────────  ENDURANCE SLALOM COURSE (Fig 3)  ───────────────────
def make_endurance_course() -> RaceCourse:
    """
    Endurance slalom: same as slalom but with an extra 500 m polygonal loop
    fanning outward into open water using 5 buoys spaced ~100 m apart.
    """
    slalom = make_slalom_course()

    # Insert the loop right before the align-straight waypoint
    loop_start_idx = next(i for i, wp in enumerate(slalom.waypoints) if wp.label == "align straight")

    loop_wps = [
        Waypoint( 90.0,   5.0,                   label="align tangent entry"),
        Waypoint(100.0, 2.5, turn_radius=0.1,  label="Loop Entry Buoy"),
        Waypoint(195.0, -30.0, turn_radius=5.0,  label="Outer Buoy 1"),
        Waypoint(260.0,  45.0, turn_radius=5.0,  label="Outer Buoy 2 (Apex)"),
        Waypoint(195.0, 120.0, turn_radius=5.0,  label="Outer Buoy 3"),
        Waypoint(115.0,  87.5,                   label="align tangent exit"),
        Waypoint(100.0,  82.5, turn_radius=5.0,  label="Loop Exit Buoy"),
        Waypoint(  0.0,  78.75,                  label="FINISH"),
    ]

    new_wps = slalom.waypoints[:loop_start_idx] + loop_wps

    return RaceCourse(name="Endurance Slalom", waypoints=new_wps,
                      n_paddlers=2)   # endurance is 2-person


# ───────────  ENDURANCE SPRINT COURSE (400m double)  ─────────────
def make_endurance_sprint_course() -> RaceCourse:
    """400m Endurance Sprint: Out 100m, Back 100m, Out 100m, Back 100m."""
    wps = [
        Waypoint(0.0, 7.5, label='START'),
        Waypoint(92.5, 15.0, turn_radius=7.5, turn_angle=180.0, label='Turn 1 (100m)'),
        Waypoint(7.5, 15.0, turn_radius=7.5, turn_angle=180.0, label='Turn 2 (0m)'),
        Waypoint(92.5, 15.0, turn_radius=7.5, turn_angle=180.0, label='Turn 3 (100m)'),
        Waypoint(0.0, 22.5, label='FINISH'),
    ]
    return RaceCourse(name='Endurance Sprint', waypoints=wps, n_paddlers=4)


# ───────────────────  CONVENIENCE  ───────────────────────────────
ALL_COURSES = {
    "sprint":            make_sprint_course,
    "slalom":            make_slalom_course,
    "endurance":         make_endurance_course,
    "endurance_sprint":  make_endurance_sprint_course,
}

def get_course(name: str) -> RaceCourse:
    """Return a course by key: 'sprint', 'slalom', 'endurance', or 'endurance_sprint'."""
    key = name.lower().replace(" ", "_")
    if key in ALL_COURSES:
        return ALL_COURSES[key]()
    if "endurance_sprint" in key:
        return ALL_COURSES["endurance_sprint"]()
    if "endurance" in key:
        return ALL_COURSES["endurance"]()
    if "slalom" in key:
        return ALL_COURSES["slalom"]()
    return ALL_COURSES["sprint"]()