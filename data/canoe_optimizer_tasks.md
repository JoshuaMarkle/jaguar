# Canoe Optimizer Tasks

## High
- Accurate punching shear
  - Separate number of mixes from construction layers
- Export canoe as STEP file

## Normal
- Geometric constraints
  - Draft increments: __ in
  - Depth increments: __ in
  - Rocker: __ in
  - Flare angle: __
  - Min entry angle: __ degrees
  - Doubly symmetric
- Structural
  - Buoyancy distributed force
  - Self weight distributed force
  - Kupfer failure envelope (combined shear and moment)
  - LRFD safety factors
    - Buoyant force and self weight: 1.2
    - Paddler loads: 1.6
  - Enter race cases separately (4, 3, 2 person)
  - Auto seat solver to minimize weight per race case
    - Seating variance: __ in
    - Manual paddler placement allowed, auto is default
  - Multi-mix placement in vertical and horizontal lines only
    - I-beam shape in center length
    - Ends all light weight concrete
- Displacement optimization with load weightings
  - Regionals: 350 lb 75%, 700 lb 25%
  - Nationals: 350 lb 60%, 525 lb 20%, 700 lb 20%
  - Total: 350 lb 67.5%, 525 lb 10%, 700 lb 22.5%
- Straight vs turning weighting
  - Straight: 80%
  - Turning: 20%
- Graphs
  - Multi-pour zone (side view)
  - Stress utilization along length
  - Moment and shear, all race cases with toggles

## Optional
- Structural sweep with fixed mold shape
  - Recalculate wall thickness and multi-mix placement for new mixes
- Bow and stern air pockets so canoe floats when swamped
  - Max extent: __ in from bow and stern
  - Wall volume + air pockets displaces 1.__ x canoe weight
- Rebuild race simulator
  - Calculate straight vs turning weighting
  - Machine learning for optimal race path per course
  - Detailed metrics: distance, time, turn angle
- Mix data
  - CSV import
  - Density vs tension, flexural, compression graph with best fit line
  - Editable data table with row delete
