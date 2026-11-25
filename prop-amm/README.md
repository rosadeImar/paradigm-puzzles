# Prop AMM

`lib.rs` is our entry on the [Prop AMM board](https://www.paradigm.xyz/puzzles/prop-amm):
**1,020.75** average edge over the board's 1,000 simulations, 991 of them won (entry
`aa995d99-ed07-4f79-bd43-ae6590444374`). It took first place on 25 November 2026.

**How it was made.** A run of our system on 16 November (`prop-amm-push-3`) made the strategy,
commit `7b8d447` (16 November, 18:49 UTC), a readable program of 274,217 characters. We then
changed it in four steps, the last three without changing what it computes:
- three of its settings, so that each update costs fewer instructions, which left its edge on the
  public seeds within 0.1 of the run's program;
- packed under the board's 100,000 characters: dead code removed, tables compressed, repeated code
  factored and names shortened;
- smaller stack frames: the update and the quote read their inputs at fixed offsets instead of
  copying the 1,024 bytes of storage, and the largest functions are no longer inlined;
- two rewrites for the board's build: every function of more than five argument words takes one
  tuple, and every run-time `+ - * << >>` calls a wrapping helper, so that a build with overflow
  checks on computes what one without does.

**Where we stopped.**
- The file is 90,798 characters of the 100,000 the board takes, packed onto one line.
- An update costs at most about 74,000 compute units of the 100,000 allowed, on the seeds we
  measured.
- The board builds with overflow checks on, and fails a program whose BPF run disagrees with its
  native run. Before sending a change, build it with `overflow-checks = true` in the release
  profile of the organiser's CLI and check that BPF gives the same edges as native.

**Run it** with the organiser's engine,
[benedictbrady/prop-amm-challenge](https://github.com/benedictbrady/prop-amm-challenge/tree/4f0c612c91eb1e3942ceedfe467db0414b1540b4)
at `4f0c612`:

```bash
cargo install --path crates/cli
```

```bash
prop-amm validate lib.rs
```

```bash
prop-amm run lib.rs
```
