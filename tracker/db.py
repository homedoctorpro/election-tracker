"""SQLite storage. Early vote is stored as one snapshot per day so the time series builds up."""
import sqlite3

from .common import DATA, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    source      TEXT NOT NULL,
    status      TEXT NOT NULL,
    rows        INTEGER,
    message     TEXT
);

-- One row per state per snapshot day: source metadata from the UF Election Lab national file.
CREATE TABLE IF NOT EXISTS ev_state_meta (
    snapshot_date TEXT NOT NULL,
    state         TEXT NOT NULL,
    state_name    TEXT,
    last_update   TEXT,       -- date the state's figures were last updated at the source (YYYY-MM-DD)
    data_source   TEXT,
    notes         TEXT,
    turn_2022     INTEGER,    -- total 2022 general turnout, for pace comparisons
    PRIMARY KEY (snapshot_date, state)
);

-- Every numeric early-vote figure, long format. level = state | county | cd.
CREATE TABLE IF NOT EXISTS ev_values (
    snapshot_date TEXT NOT NULL,
    state         TEXT NOT NULL,
    level         TEXT NOT NULL,
    geo           TEXT NOT NULL,
    metric        TEXT NOT NULL,
    value         REAL,
    PRIMARY KEY (snapshot_date, state, level, geo, metric)
);

CREATE TABLE IF NOT EXISTS polls (
    poll_id     TEXT PRIMARY KEY,
    poll_type   TEXT NOT NULL,      -- us-senator | governor | us-representative | generic-ballot
    race        TEXT,               -- e.g. "TX Senate", "MN Governor", "NY-17"
    state       TEXT,
    district    TEXT,
    subject     TEXT,
    pollster    TEXT,
    sponsors    TEXT,
    start_date  TEXT,
    end_date    TEXT,
    created_at  TEXT,
    sample_size INTEGER,
    population  TEXT,
    internal    INTEGER,
    partisan    TEXT,
    url         TEXT,
    source      TEXT NOT NULL,
    quality_flag TEXT,              -- set when the source data looks wrong (e.g. impossible dates)
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS poll_answers (
    poll_id TEXT NOT NULL,
    choice  TEXT NOT NULL,
    pct     REAL,
    PRIMARY KEY (poll_id, choice)
);

CREATE TABLE IF NOT EXISTS race_ratings (
    snapshot_date TEXT NOT NULL,
    race          TEXT NOT NULL,
    state         TEXT,
    district      TEXT,
    outlet        TEXT NOT NULL,
    rating        TEXT,
    PRIMARY KEY (snapshot_date, race, outlet)
);

-- Ballots accepted per day before Election Day, from voter-level files (NC).
CREATE TABLE IF NOT EXISTS pace (
    state     TEXT NOT NULL,
    year      INTEGER NOT NULL,
    days_out  INTEGER NOT NULL,
    method    TEXT NOT NULL,     -- mail | in_person | other
    party     TEXT NOT NULL,     -- dem | rep | una | other
    race      TEXT NOT NULL,     -- white | black | hispanic | other
    age_group TEXT NOT NULL,
    n         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pace ON pace (state, year, days_out);

-- Final pre-Election-Day votes from the EAC's Election Administration and Voting Survey.
CREATE TABLE IF NOT EXISTS ref_eavs (
    state TEXT NOT NULL, year INTEGER NOT NULL,
    total_voters INTEGER, mail INTEGER, early_in_person INTEGER, all_mail_jurisdictions INTEGER,
    PRIMARY KEY (state, year)
);

-- UF Election Lab national-file snapshots from past cycles (data/reference/uf<year>_snapshots.csv).
-- days_out = days before that cycle's Election Day; -1 = the final post-election file.
DROP TABLE IF EXISTS ref_uf2024;
CREATE TABLE IF NOT EXISTS ref_uf_snapshots (
    year INTEGER NOT NULL,
    snapshot_date TEXT NOT NULL, days_out INTEGER NOT NULL, state TEXT NOT NULL,
    voted_all REAL, voted_dem REAL, voted_rep REAL, voted_none REAL, accept_all REAL, inperson_all REAL,
    accept_dem REAL, accept_rep REAL, accept_none REAL,
    PRIMARY KEY (year, snapshot_date, state)
);

CREATE INDEX IF NOT EXISTS ix_ev_metric ON ev_values (metric, level, state);
CREATE INDEX IF NOT EXISTS ix_polls_race ON polls (poll_type, state, end_date);

-- Latest snapshot per state with the headline numbers, wide.
DROP VIEW IF EXISTS v_ev_state_latest;
CREATE VIEW v_ev_state_latest AS
WITH latest AS (SELECT MAX(snapshot_date) d FROM ev_state_meta)
SELECT m.snapshot_date, m.state, m.state_name, m.last_update,
       MAX(CASE WHEN v.metric='request_all'  THEN v.value END) AS requested,
       MAX(CASE WHEN v.metric='accept_all'   THEN v.value END) AS mail_returned,
       MAX(CASE WHEN v.metric='inperson_all' THEN v.value END) AS in_person,
       MAX(CASE WHEN v.metric='voted_all'    THEN v.value END) AS total_voted,
       MAX(CASE WHEN v.metric='voted_dem'    THEN v.value END) AS voted_dem,
       MAX(CASE WHEN v.metric='voted_rep'    THEN v.value END) AS voted_rep,
       MAX(CASE WHEN v.metric='voted_none'   THEN v.value END) AS voted_other,
       m.turn_2022,
       ROUND(100.0 * MAX(CASE WHEN v.metric='voted_all' THEN v.value END) / NULLIF(m.turn_2022, 0), 2)
           AS pct_of_2022_turnout,
       m.data_source
FROM ev_state_meta m
JOIN latest ON m.snapshot_date = latest.d
LEFT JOIN ev_values v ON v.snapshot_date = m.snapshot_date AND v.state = m.state
                     AND v.level = 'state' AND v.geo = m.state
GROUP BY m.snapshot_date, m.state;

-- Day-over-day change in total early votes per state.
DROP VIEW IF EXISTS v_ev_state_daily;
CREATE VIEW v_ev_state_daily AS
SELECT snapshot_date, state, value AS total_voted,
       value - LAG(value) OVER (PARTITION BY state ORDER BY snapshot_date) AS change
FROM ev_values
WHERE level = 'state' AND metric = 'voted_all';
"""


def connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    return con


def log_run(con, started_at, source, status, rows=None, message=None):
    con.execute(
        "INSERT INTO runs (started_at, source, status, rows, message) VALUES (?,?,?,?,?)",
        (started_at, source, status, rows, message),
    )
    con.commit()
