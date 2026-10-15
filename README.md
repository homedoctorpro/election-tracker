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

## 2022 / 2024 comparisons

The "vs 2022 / 2024" tab and `data/latest/compare_states.csv` compare each state's 2026 early vote with earlier cycles:

- **Every state, against the final early vote:** this uses the [EAC Election Administration and Voting Survey](https://www.eac.gov/research-and-data/datasets-codebooks-and-surveys) for 2022 and 2024, where pre-Election-Day votes are mail plus early in-person. Early in the window this mostly shows how soon a state mails ballots, so the dashboard also shows each state's pace against the median state.
- **Exact same-days-out comparison:** North Carolina's voter files give the date every ballot came back in 2022, 2024 and 2026, broken down by party, race, age and voting method (`data/latest/nc_same_day.csv`). Only aggregate counts are kept; names and addresses are never stored.
- **Party mix:** each state's 2026 mail-ballot D−R margin is compared with its final 2024 mail-ballot margin from UF Election Lab. This is not a same-day comparison. Archived 2024 UF snapshots also exist at 2, 1 and 0 days out, so a same-day column fills in for those days.

- **"If the mail shift held":** the comparison tab adds each state's mail-ballot shift to its actual result, with a 2022/2024 toggle that defaults to 2022. Results come from the FEC's certified totals: for 2022, the Senate race where both parties ran, otherwise the statewide U.S. House vote; for 2024, the presidential race. Pennsylvania's mail-ballot files on data.pa.gov give an exact same-days-out comparison for 2022, 2024 and 2026, like North Carolina's. Those two are the only states with 2022 party data. For the other states the 2024 baseline is the final mail mix, which overstates any move toward Democrats; the page shows by how much, using PA and NC. This is arithmetic, not a forecast.

Georgia also publishes voter-level absentee files with dates, but its download portal blocks automated requests, so Georgia isn't included yet.

The reference data in `data/reference/` is built once and committed. To rebuild it (this downloads about 400 MB):
```bash
python -m tracker.reference --build all
```

## Daily archive (for future cycles)

Every morning the cloud job saves that day's full snapshot to `data/archive/2026/YYYY-MM-DD/` and commits it to GitHub. That's about 0.5 MB a day:
- the UF national, county, district and race-rating files, exactly as downloaded
- the full VoteHub poll history
- NC accepted ballots by return date, party, race, age and method (counts only)

The archive is what lets a future cycle compare against 2026 at the exact same number of days before Election Day, for every state. After Nov 3, 2026, turn it into a baseline file:
```bash
python -m tracker.reference --from-archive 2026
```
That writes `data/reference/uf2026_snapshots.csv` in the same format as the 2024 baseline. It uses each state's own last-update date, so a state whose figures lag a day is matched on the date its data actually covers. For a new cycle, add its Election Day to `ELECTION_DAYS` in `tracker/common.py`. NC's 2026 pace can also be rebuilt from the state's final 2026 voter file, the same way 2022 and 2024 were.

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
python -m tracker.run --site --archive   # also build site/ and archive the day (what CI runs)
python -m tracker.run --only polls    # or --only early_vote
```

To re-register the scheduled task, run `scripts/register_task.ps1` in PowerShell.
