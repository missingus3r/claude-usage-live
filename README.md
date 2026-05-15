# claude-usage-live

A public page showing the time-until-next-reset for Claude Code sessions and weekly quotas.

When a "canary" tracker detects the Anthropic-side weekly counter dropping ahead of its scheduled reset, a global banner notifies everyone that **Anthropic reset all users early**.

🔗 **Live**: [missingus3r.github.io/claude-usage-live](https://missingus3r.github.io/claude-usage-live/)

## How it works

1. A local `tracker.py` spawns `claude --usage` every ~5 hours.
2. It parses the TUI output for current `Current session` and `Current week` percent-used values.
3. It computes the absolute timestamp of the next reset.
4. **If the current weekly %-used drops by >50pp *before* the previously-recorded scheduled reset** → that's an early reset across all Claude Code users. The script writes `early_reset_detected: true` and appends a row to `early_reset_history`.
5. The script commits and pushes `data/usage.json`. GitHub Pages serves `index.html` which renders the live countdown from that JSON.

## Privacy

`data/usage.json` **does not** contain any actual usage percentages. It only contains:

- absolute timestamps of the next scheduled resets
- an alarm bit for early-reset events
- a history of when early resets happened (with the drop magnitude, but not the absolute values)

The internal comparison state needed by the tracker lives in `.tracker_state.json`, which is **gitignored**.

## Running the tracker locally

```bash
pip install pexpect
python3 tracker.py
```

Set up a cron entry to run every 5 hours, off the half-hour to avoid colliding with Anthropic's own reset boundary:

```cron
17 */5 * * * cd ~/proyectos/claude-usage-live && /usr/bin/python3 tracker.py >> tracker.log 2>&1
```

## Stack

- Python 3 + `pexpect` (TUI scraping)
- Plain HTML + CSS + vanilla JS
- GitHub Pages

## License

MIT
