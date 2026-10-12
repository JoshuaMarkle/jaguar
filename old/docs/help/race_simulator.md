# Race Simulator

## What it does
The Race Simulator runs a genetic algorithm (GA) to find the combination of hull shape parameters and paddling strategy that maximises your combined score across the selected ASCE events.

## Workflow
1. Tick the events you are competing in.
2. Set **Course Weights** — higher weight = the GA optimises harder for that event.
3. Set **Evolution** parameters. Defaults (pop 40, gen 30) finish in under a minute on a typical laptop.
4. Click **Run Race Simulator**.
5. When complete, switch to the **Chart** and **Racing Line** tabs to review results.

## Key outputs
| Output | Meaning |
|--------|---------|
| Cs | Fraction of course distance on straights (0–1) |
| Ct | Fraction of course distance in turns (0–1) |
| Best fitness | Combined score across selected events (higher = better) |

## Tips
- **Population 40–60** balances speed vs. coverage.
- Increasing **generations** beyond 60 rarely improves results on a 7-factor search.
- The **Replay** tab lets you step through the winning hull's telemetry frame by frame.
