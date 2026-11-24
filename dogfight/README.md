# Dogfight

`pilot.onnx` is our entry on the [Dogfight board](https://www.paradigm.xyz/puzzles/dogfight): it
won **all 60** of its board matches, every one by elimination (entry
`52b5d698-f028-4035-aa5e-bf82b73efea2`, 19 November 2026).

**How it was made.** It starts from r16h, our pilot network from earlier work on this task. On
19 November a run of our system (`dogfight-bracket-1`) studied how r16h lost its real games to the
strongest opponents and wrote rules against them. We assembled the entry from those rules on
r16h's graph, each one measured on replays of real games before it went in.

**What it holds.**
- r16h's network, on a faster graph that gives exactly the same answers.
- Two classifiers that recognise two strong opponents' flying styles from the pilot's own
  observations, and rules that act only against them: against the first, a 30-degree firing
  window during its climb, a slower throttle, a learned bonus and a dodge timed to its reload;
  against the second, holding fire until within 205 m.
- Moves against any opponent: a chase hybrid, a pursuit of an opponent stalled at the ceiling and
  a quick tight turn.
- Repeated parts of the graph merged for speed, with the same answers bit for bit.

**Where we stopped.** The file is 697,839 bytes and uses 249,709 of the 250,000 elements the
validator allows, so more rules need room. A version with two more rules against the first style,
a guard against stalling and a ranged rescue, measured stronger against it but took 16% longer
per decision than r16h, so we entered this one.

**Run it** with the organiser's simulator,
[benedictbrady/dogfight-challenge](https://github.com/benedictbrady/dogfight-challenge/tree/10a6b40d717f54a631e833ef850fd2f1e4066798)
at `10a6b40`: copy `pilot.onnx` into its `models/` folder, then

```bash
make build
```

```bash
make validate MODEL=models/pilot.onnx
```

```bash
make run P0=models/pilot.onnx P1=brawler
```
