# Negotiation

`prompt.txt` is our entry on the [Negotiation board](https://www.paradigm.xyz/puzzles/negotiation):
**1.0**, the most a prompt can score, in all ten games, at 1,797 characters (entry
`5b8044c0-73aa-4fb9-aa0c-1d1723827872`, 10 November 2026).

`prompt-707.txt` is the same strategy at 707 characters. It also scored 1.0 on the board, on
13 November (submission `5759a7ea`). The board keeps an author's first entry when scores tie, so
it is not the one listed.

**How they were made.** `prompt.txt` came from a run of our system on 9 and 10 November, graded on
the organiser's engine. `prompt-707.txt` came from cutting it down afterwards, each cut played over
the board's ten games against stand-in opponents.

**Where we stopped.** Three instructions carry the score: keep everything and give nothing on every
proposal; open warmly and invite the other side's split; after a counter, keep the same offer but
word the message as agreeing to their proposal. Of the rest, the accept threshold and the scripted
lines help a little, and the one-unit concession from round 4 hurts.

**Run it** with the organiser's engine,
[paradigmxyz/negotiation-challenge](https://github.com/paradigmxyz/negotiation-challenge/tree/47f5acd3adedb3a2101d8b2f8c43d403c0aa323b)
at `47f5acd`:

```bash
uv run negotiate test prompt.txt
```
