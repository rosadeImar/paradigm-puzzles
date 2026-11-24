# Shape Packing

`solution.json` is our entry on the [Shape Packing board](https://www.paradigm.xyz/puzzles/packing):
radius **2.948526950268147** (entry `d1a9d2ed-eebe-4db6-b57f-6fdcc63acb32`, 22 November 2026).

**How it was made.** A run of our system on 16 November (`packing-5c`) built an exact engine for
packings whose pieces touch, and found this one. A second run (`packing-5e`) carried it unchanged.

**Where we stopped.**
- Under the scorer's own constraints, this arrangement of contacts (30 touching pairs and 11
  contacts with the outer circle) has a true optimum of 2.9485265494513. The scorer rounds
  coordinates to six decimals, so a packing sits slightly above that optimum; how close depends on
  the rounding.
- `packing-5e` searched this arrangement's six-decimal points, and our searches for a different
  arrangement found none lower than this one.

**Run it** with the organiser's scorer,
[benedictbrady/shape-packing-challenge](https://github.com/benedictbrady/shape-packing-challenge/tree/78a78626f098cd236f3a09bb2cb242f72951599d)
at `78a7862`:

```bash
uv run python run.py solution.json
```
