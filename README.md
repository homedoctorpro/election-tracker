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
| Ohio early vote by party, age, county and day | [Ohio SOS Absentee and Early Voting Dashboard](https://data.ohiosos.gov/portal/election-dashboards) | Queried through the dashboard's public Power BI endpoint, which returns aggregate counts. See "Ohio" below. |

## Candidate parties

VoteHub only gives candidate names, so `tracker/sources/parties.py` adds a party to each one every morning:
1. `data/reference/party_overrides.csv` for manual fixes (columns: poll_type, state, choice, party)
2. the FEC's 2026 candidate file for Senate and House, matched on state, district and last name
3. the Wikipedia article on each race, used for governors and as a fallback; it reads the infobox and the party sections that list candidates

Names that can't be matched unambiguously get no party, so the page never guesses. One-letter typos in poll data (e.g. "Maura Healy") are matched within the same race. Every poll view shows party tags and margins as D/R/I +n; `race_summary.csv` adds `party_a`, `party_b` and a signed `margin_d_minus_r`.

## Battlegrounds tab

The first tab covers the battleground states (`STATES` in `tracker/battleground.py`): AZ, GA, MI, NC, NV, PA, WI, IA, OH, ME, KS, FL and TX. At the top, an at-a-glance chart gives each state one row: a line from its 2024 D−R margin to its 2026 margin, the shift, turnout pace and the poll margins, grouped by how solid the comparison is. Below it, one card per state adds the vote counts, party mix and a mini chart. Hover over a row or card, or tap it, for cumulative and party-mix charts by days out for every cycle on file, the same-day comparison table, the mail shift projection and the county lean.

The headline shift uses the most reliable comparison the state has, and every row and card says which one and which votes it counts:
- **same day · all votes** (solid): all early votes, mail and in person, compared at the same days out in 2024, from voter files (NC, PA) or daily state reports (IA). The mail-only shifts (registration-adjusted for NC and PA) are on the card too.
- **vs final · mail only** (hatched): mail ballots compared with the final 2024 mail mix (AZ, NV, ME, KS, FL). This overstates any move toward D early on. In-person votes are left out because in-person voting starts later, so only a few days of it would be compared with the whole 2024 in-person vote.
- **county lean · all votes** (hatched): for states without party registration (GA, MI, WI, and OH, whose primary-ballot "party" isn't comparable). It's the 2024 presidential margin of the counties this year's early votes come from, weighted by each county's early votes so far, minus the same figure for the whole 2024 (or 2022) early vote by county. Earlier early votes by county are pre-Election-Day votes from the EAC survey, with jurisdictions summed to counties (`data/reference/eavs_county.csv`; county sums match the state EAVS totals). Comparing early votes with early votes takes out most of the usual pull of early voting toward metro counties, but the base is a final count while 2026 is partway through. Wisconsin's survey data is by municipality, so its 2024 base is 2024 turnout and it has no 2022 lean. County results are in `data/reference/county_results.csv` (from [tonmcg/US_County_Level_Election_Results_08-24](https://github.com/tonmcg/US_County_Level_Election_Results_08-24); D and R totals match the FEC exactly in the states checked). Texas has no UF county file and Maine's is by town, so neither has a county lean.

**Share images:** every site build also writes three fixed 1080×1350 cards (4:5, sized for phones and social posts), each as a page and a headless-Chrome screenshot (`tracker/share.py`), published next to the dashboard so the same link always shows the latest numbers:
- `share.png`: the shift in each state's early-vote margin since 2024.
- `share-2022.png`: the same since 2022. Only NC, PA and IA have 2022 party data; the rest use county lean against the 2022 early vote.
- `share-trend.png`: each state's margin by days before Election Day, 2026 against 2024 and 2022. Same-day states have every day of all three cycles. Mail states show the 2024 final as a dashed line and the few 2024 days on file as dots. County-lean states show their daily lean against the whole 2024 and 2022 early vote. Daily 2026 figures come from the committed archive (`data/archive/2026/`), skipping days with under 1,000 ballots or under 5% of the latest count.

The `.html` pages carry preview tags, so a pasted link shows the image. The PNGs are skipped if no Chrome or Edge is found; set `CHROME` to point at one.

Cards with under 5,000 mail ballots, or under 5% of the 2024 early vote, are faded, marked "too early", and sorted to the bottom of their group.

## 2022 / 2024 comparisons

The "vs 2022 / 2024" tab and `data/latest/compare_states.csv` compare each state's 2026 early vote with earlier cycles:

- **Every state, against the final early vote:** this uses the [EAC Election Administration and Voting Survey](https://www.eac.gov/research-and-data/datasets-codebooks-and-surveys) for 2022 and 2024, where pre-Election-Day votes are mail plus early in-person. Early in the window this mostly shows how soon a state mails ballots, so the dashboard also shows each state's pace against the median state.
- **Exact same-days-out comparison:** North Carolina's voter files give the date every ballot came back in 2022, 2024 and 2026, broken down by party, race, age and voting method (`data/latest/nc_same_day.csv`). Only aggregate counts are kept; names and addresses are never stored.
- **Party mix:** each state's 2026 mail-ballot D−R margin is compared with its final 2024 mail-ballot margin from UF Election Lab. This is not a same-day comparison. Archived 2024 UF snapshots also exist at 2, 1 and 0 days out, so a same-day column fills in for those days.

- **"If the mail shift held":** the comparison tab adds each state's mail-ballot shift to its actual result, with a 2022/2024 toggle that defaults to 2022. For 2022 the baseline is the same office as the state's top 2026 race: the Senate race if the state has one in 2026, otherwise the governor's race (AZ, CA, CT, MD, NV and PA in the chart), as long as both parties ran in 2022. Otherwise it's the statewide U.S. House vote (e.g. ME, NE, NJ and NM have 2026 Senate races but had none in 2022). For 2024 it's the presidential race. Senate, House and presidential totals are the FEC's certified figures. Governor totals are entered by hand from each state's certified canvass in `data/reference/governor_2022.csv`, with the source for each row. The 2026 race lists are `SENATE_2026` and `GOVERNOR_2026` in `tracker/common.py`. Pennsylvania's mail-ballot files on data.pa.gov give an exact same-days-out comparison for 2022, 2024 and 2026, like North Carolina's. Iowa's daily SOS reports do the same for 2022 and 2024 (see "Iowa" below). Those three are the only states with 2022 party data. For the other states the 2024 baseline is the final mail mix, which overstates any move toward Democrats; the page shows by how much, using PA and NC. This is arithmetic, not a forecast.
- **Registration adjustment (NC, PA):** a party can make up a smaller share of mail ballots just because fewer voters are registered with it. NC Democrats, for example, fell from 33.7% of registered voters in Nov 2022 to 30.0% in March 2026, with unaffiliated rising. So for NC and PA, the 2026 mail ballots are reweighted to the baseline year's party registration before computing the shift, and the projection uses that adjusted shift; the raw shift is shown alongside. Registration sources: NCSBE `voter_stats` snapshots (the latest posted, currently the March 2026 primary) and PA Department of State statistics (certified Nov 2022 and Nov 2024 PDFs plus the weekly `currentvotestats.xlsx`).

### Ohio

Ohio doesn't register voters by party. Its "party" is the party ballot a voter took in a recent primary; anyone who hasn't voted in one counts as unaffiliated. The Secretary of State's dashboard has ballots received per day by county, ballot type, age group and that party for every general election since Nov 2024, and nothing with party before that. So Ohio gets an exact same-days-out comparison with 2024 only: statewide (`data/latest/oh_same_day.csv`) and for all 88 counties (`oh_county_same_day.csv`). Mail, drop-box and hand-delivered absentee ballots are counted as mail.

- Counties report to the state a day late, so the newest one or two days in the dashboard are only partly in. The tracker drops trailing days that hold less than a quarter of a normal day's ballots.
- ohiosos.gov, including the dashboard's raw voter-level download, is behind a Cloudflare bot check, so scripts can't reach it. The dashboard's Power BI data endpoint isn't, and that's what `tracker/sources/oh_sos.py` queries. If the state republishes the report with a new key, update `KEY`, `MODEL` and `DATASET` there. They're in the dashboard iframe's `?r=` parameter, which is base64 JSON.
- Ohio isn't in the "if the mail shift held" chart. UF has no Ohio party data, and Ohio's primary-ballot party isn't comparable to registration.
- Not used: Franklin County's nightly voter-level absentee lists (the statewide county data already covers Franklin, and those files contain names and addresses), and Cuyahoga's vote-by-mail export, which is behind reCAPTCHA.

### Iowa

The Iowa Secretary of State posts "Absentee Ballot Statistics" by congressional district for each general (`sos.iowa.gov/elections/pdf/<year>/general/AbsenteeCongressional<year>.pdf`). Each PDF holds every daily report of the cycle. `data/reference/ia_sos_absentee.csv` has the statewide ballots received by party for each report date in 2018, 2022 and 2024, with in-person absentee (counter, satellite) split out for 2022 and 2024. The 2018 file has no receipt method. These rows replace UF's 2024 Iowa figures, which counted in-person absentee as mail, so Iowa's mail mix is compared at the same days out for 2022 and 2024, and its final 2024 total includes ballots received on Election Day.

- A report dated D covers ballots received through the night before. Days out are counted from the report date, as for UF's 2026 Iowa figures.
- "Other" in the comparisons is No Party plus Libertarian plus other parties, matching UF. Figures that cite No Party alone leave out those smaller parties (about 2,900 ballots in 2018, 4,200 in 2024).
- sos.iowa.gov blocks scripted downloads, so the CSV was extracted in a browser and checked against each report's Grand Total. To update it, open the PDFs in a browser.

Georgia also publishes voter-level absentee files with dates, but its download portal blocks automated requests, so Georgia isn't included yet.

The reference data in `data/reference/` is built once and committed. To rebuild it (this downloads about 400 MB):
```bash
python -m tracker.reference --build all
```

## Daily archive (for future cycles)

The cloud job runs at 7:15 AM and again at 10 PM ET (UF posts updates through the day, so the evening run catches them). Each run saves that day's full snapshot, the evening one replacing the morning's, to `data/archive/2026/YYYY-MM-DD/` and commits it to GitHub. That's about 0.5 MB a day:
- the UF national, county, district and race-rating files, exactly as downloaded
- the full VoteHub poll history
- NC accepted ballots by return date, party, race, age and method (counts only)
- Ohio ballots by return date, party, age and method, statewide and by county (counts only)

The archive is what lets a future cycle compare against 2026 at the exact same number of days before Election Day, for every state. After Nov 3, 2026, turn it into a baseline file:
```bash
python -m tracker.reference --from-archive 2026
```
That writes `data/reference/uf2026_snapshots.csv` in the same format as the 2024 baseline. It uses each state's own last-update date, so a state whose figures lag a day is matched on the date its data actually covers. For a new cycle, add its Election Day to `ELECTION_DAYS` in `tracker/common.py`. NC's 2026 pace can also be rebuilt from the state's final 2026 voter file, the same way 2022 and 2024 were.

## Outputs

- `data/election.db`: SQLite. It keeps one early-vote snapshot per day, so the time series builds up over time.
  - `v_ev_state_latest` gives the headline numbers per state. `v_ev_state_daily` gives day-over-day change.
  - `ev_values` holds every metric (party, age, race, gender) at the state, county, cd and cd_county level.
  - `pace` holds NC, PA and OH ballots by days before Election Day, method, party, race and age, for every cycle. `pace_county` has the same for Ohio by county.
  - `polls` and `poll_answers` hold the polls. `poll_matchup` tags each poll as current matchup, primary/hypothetical, or unknown.
- `data/latest/*.csv`: the latest export, ready for Excel.
- `data/raw/YYYY-MM-DD/`: every file exactly as downloaded, so any day can be re-parsed.
- `logs/refresh-YYYY-MM-DD.log`

## Caveats

- VoteHub doesn't separate primary polls from general-election polls. `matchup` is inferred from the two candidates polled most often in each race over the last 60 days. Races with no recent polling show `unknown`.
- `race_summary.csv` is a plain average of up to 5 current-matchup polls that ended in the last 60 days. Older polls are never averaged, so one stale poll can't flip a thinly polled race. Races with nothing in that window get `status` = `no recent polling` and their last poll date. Nothing is weighted or adjusted for pollster quality, and in three-way races only the top two candidates are compared.
- Polls with impossible dates in the source data get a `quality_flag` and are left out of the averages.
- The CA and ID district totals run below their state totals, because UF builds those files from a different feed. ID's district files leave out in-person votes.

## Commands

```bash
python -m tracker.run                 # full refresh
python -m tracker.run --site --archive   # also build site/ and archive the day (what CI runs)
python -m tracker.run --only polls    # or --only early_vote
```

To re-register the scheduled task, run `scripts/register_task.ps1` in PowerShell.
