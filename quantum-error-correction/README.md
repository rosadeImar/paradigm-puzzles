# Quantum Error Correction

`solve.py` is our entry on the [QEC board](https://www.paradigm.xyz/puzzles/qec): **612 errors in
240,000 shots**, 2,550 per million (entry `857f6c16-a0c9-417c-a737-e4f5249704e3`, "half-TT +
BDD"). It took first place on 19 November 2026.

**How it was made.** It is the best decoder of a run of our system on 9 November
(`justin-qec-v2`), with its tables repacked as base85 that numpy decodes, so that it passes the
board's checker. Every table is byte for byte the run's, and it scores the same.

**Where we stopped.** Under the organiser's noise model this decoder is close to the best any
decoder can do: the run's labs bounded it within about 2 errors per million of the Bayes-optimal
decoder at the largest code size, and independent checks agreed there and at the smallest.

**Run it** with the organiser's benchmark,
[MaxResnick/quantum_error_correction_challenge](https://github.com/MaxResnick/quantum_error_correction_challenge/tree/96fb86f2499ee86b77ad72c9a358d4dbc3f6325e)
at `96fb86f`: put this `solve.py` in place of theirs, then

```bash
uv run python run.py
```
