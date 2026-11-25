# Anthropic's performance take-home

`perf_takehome.py` is our entry on the
[take-home board](https://www.paradigm.xyz/puzzles/anthropic-challenge): **861 cycles**, correct on
every seed (entry `6e37ae8a-368a-4d26-8975-83f4a5d6d57c`). It took first place on 24 November
2026.

**How it was made.** A run of our system on 23 and 24 November (`vliw-12`). This is the run's
first program to grade 861, commit `0cff3686` (24 November, 05:20 UTC). By the run's end, ten
different programs had reached 861. The file is the run's program as the board scored it: a
`KernelBuilder` of a few lines that unpacks the 861-cycle instruction stream, stored compressed
below it. Its first two lines restate, in short, the notice at the top of Anthropic's file.

**Run it** with Anthropic's repository,
[anthropics/original_performance_takehome](https://github.com/anthropics/original_performance_takehome/tree/5452f74bd977807ac2e74f3d29432b9df6f25197)
at `5452f74`: put this `perf_takehome.py` in place of theirs, then

```bash
python tests/submission_tests.py
```
