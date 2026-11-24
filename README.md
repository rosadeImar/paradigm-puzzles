# rosadelmar on Paradigm's puzzles

Our programs for the challenges at [paradigm.xyz/puzzles](https://www.paradigm.xyz/puzzles), so
anyone can start from where we stopped. Each folder holds the exact file behind our board entry and
a note on how it was made, where we stopped, and how to run it with the organiser's code.

| Challenge | Our board entry | File |
|---|---|---|
| [Prediction Market](prediction-market/) | 112.850308 mean edge, 200 of 200 seeds (19 Nov 2026) | `strategy.py` |
| [Quantum Error Correction](quantum-error-correction/) | 612 errors in 240,000 shots (19 Nov 2026) | `solve.py` |
| [Anthropic's take-home](take-home/) | 861 cycles (24 Nov 2026) | `perf_takehome.py` |
| [Negotiation](negotiation/) | 1.0, ten games of ten, 1,797 characters (10 Nov 2026) | `prompt.txt` |
| [Persuasion](persuasion/) | $200 median price, 135 characters (13 Nov 2026) | `description.txt` |
| [Dogfight](dogfight/) | 60 matches won of 60, all by elimination (19 Nov 2026) | `pilot.onnx` |
| [Shape Packing](shape-packing/) | radius 2.948526950268147 (22 Nov 2026) | `solution.json` |
| [Verified compiler](verified-compiler/) | a pull request to the compiler | [paradigmxyz/solidus#29](https://github.com/paradigmxyz/solidus/pull/29) |

Most of these come from runs of our research system, in which many coding agents work on one task
together and every program is graded on a copy of the challenge. Each folder's note names the run,
and any change a person made after it.

## Licence

Our work here is under the [MIT No Attribution](LICENSE) licence: anyone may use, change, share
or submit it, for any purpose, without asking us or crediting us. The organisers' code is not
copied here, except Anthropic's take-home file, which our kernel is written into; each note links
the organiser's code at the commit we used.
