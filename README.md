# ElectionTracker

Daily pull of 2026 general-election early-vote data and Senate / Governor / House polls.
**Dashboard:** https://homedoctorpro.github.io/election-tracker/

Two runners:
- **GitHub Actions** (`.github/workflows/refresh.yml`) runs every morning at about 7:15 AM ET, rebuilds the public dashboard on GitHub Pages, and commits the day's state totals to `data/history/`. It doesn't need this PC to be on.
- The **Windows Task Scheduler** task *ElectionTracker Daily Refresh* runs at 7:00 AM and keeps the full local SQLite history and raw archive. Those are too big to commit.

## Sources

| Data | Source | Notes |
|---|---|---|
| Early vote: all states, counties, congressional districts | [UF Election Lab](https://election.lab.ufl.edu/early-vote/2026-early-voting/) | Compiled from each state's official reports. CC BY-NC-ND 4.0, so attribute it and don't use it commercially. AL and NH aren't in their file. |
| House race ratings (10 outlets) | UF Election Lab `house_tossup_races.csv` | Cook, Inside Elections, Sabato, Economist, Split Ticket, DDHQ, 50+1, Fox, Silver, RCP |
| Senate, Governor, House, generic-ballot polls | [VoteHub API](https://votehub.com/polls/api/) | Free API. It returns the full poll history on every call. |

## Outputs

- `data/election.db`: SQLite. It keeps one early-vote snapshot per day, so the time series builds up over time.
  - `v_ev_state_latest` gives the headline numbers per state. `v_ev_state_daily` gives day-over-day change.
  - `ev_values` holds every metric (party, age, race, gender) at the state, county, cd and cd_county level.
  - `polls` and `poll_answers` hold the polls. `poll_matchup` tags each poll as current matchup, primary/hypothetical, or unknown.
- `data/latest/*.csv`: the latest export, ready for Excel.
- `data/raw/YYYY-MM-DD/`: every file exactly as downloaded, so any day can be re-parsed.
- `logs/refresh-YYYY-MM-DD.log`

## Caveats

- VoteHub doesn't separate primary polls from general-election polls. `matchup` is inferred from the two candidates polled most often in each race over the last 45 days. Races with no recent polling show `unknown`.
- `race_summary.csv` is a plain average of the last 5 current-matchup polls. Nothing is weighted or adjusted for pollster quality, and in three-way races only the top two candidates are compared.
- Polls with impossible dates in the source data get a `quality_flag` and are left out of the averages.
- The CA and ID district totals run below their state totals, because UF builds those files from a different feed. ID's district files leave out in-person votes.

## Commands

```bash
python -m tracker.run                 # full refresh
python -m tracker.run --site          # also build site/ (what CI runs)
python -m tracker.run --only polls    # or --only early_vote
```

To re-register the scheduled task, run `scripts/register_task.ps1` in PowerShell.
