# Prediction Market

`strategy.py` is our entry on the
[Prediction Market board](https://www.paradigm.xyz/puzzles/prediction-market-challenge):
**112.850308** mean edge over the board's 200 seeds, all 200 played (entry
`ba9dccf9-c1a5-46cf-b9c5-7e53522e9297`). It took first place on 19 November 2026.

**How it was made.** A run of our system on 18 November (`pm-search-v3-20261118`) started from our
previous best program, rewritten by hand as a frame with switchable arms that plays identically.
This file is one of that run's graded programs, commit `605daa6` (18 November, 21:42 UTC). One
change was made after the run: the method `_step` was renamed `play_step`, because the board's
checker refuses an attribute named `_step`.

**Where we stopped.**
- Each arm is a named unit of behaviour; the `ARMS` table at the top switches each one per spread,
  and `()` turns one off.
- The file is 198,165 characters; the board takes 200,000 at most.
- The board drops every character above U+FFFF, so packed data must stay in plain ASCII.

**Run it** with the organiser's simulator,
[danrobinson/prediction-market-challenge](https://github.com/danrobinson/prediction-market-challenge/tree/b244530ee2a6247b5f930830eeca9dd950da0e7e)
at `b244530`:

```bash
uv run orderbook-pm run strategy.py
```
