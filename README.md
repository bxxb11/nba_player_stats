# NBA Props Lab

A local web app for NBA player prop betting research. Pulls live stats directly from the NBA API and surfaces per-game breakdowns, prop hit rates, matchup defense data, team rankings, a full game schedule, a parlay builder, and a deep analysis suite — all in a dark-mode single-page interface.

---

## Features

### 📊 INSIGHTS Tab — Analysis Suite

Surfaces precomputed analysis from three Jupyter notebooks without re-running them on every load. All data is read from cached `team_analysis/` files.

**1 · Player Season Metrics Table**
- All 91 tracked players sorted by AST per game
- Columns: GP · MIN · AST · STD · TOV · FGM · RELIABILITY (AST/STD) · POTENTIAL AST · CONV%
- Selected player highlighted in cyan; shows even if outside the top 20
- Click any column header to sort ascending / descending

**2 · AST Edge by Defensive Cluster (heatmap)**
- K-means clustering (k=6) of all 30 teams on 11 defensive play-type PPP metrics
- Cluster tiers: Elite-DEF → Strong-DEF → Avg-DEF → Avg-DEF+ → Soft-DEF → Porous-DEF
- Rows = top-20 assist players + selected player; diverging color (red = above avg, blue = below)
- Selected player pinned to the first row with cyan highlight

**3 · Per-Opponent Breakdown Table**
- For the selected player: avg AST, AST edge, STD, DEF cluster, and opp DEF rating for every opponent (min 2 games)
- Sorted by edge descending — most favorable opponents at the top
- Accent-normalized matching (e.g. "Doncic" matches "Dončić" in the data)

**4 · Play-Type Attribution (Ridge Coefficients)**
- Ridge regression (α=1.0) per player: 11 defensive play-type PPP features → AST edge
- Diverging heatmap (red = hurts assists, blue = helps); R² shown per row
- Top-15 assist players + selected player

**5 · League Play-Type Rankings**
- 30 teams × 11 play types; toggle between **OFFENSE** and **DEFENSE**
- Rank 1 = best in league; green = elite, red = poor
- Offense rank: highest PPP = rank 1 · Defense rank: lowest PPP allowed = rank 1

**↻ REFRESH ANALYSIS button**
- Re-runs all three notebooks via `jupyter nbconvert` in a background thread (~5–10 min)
- Live status indicator: Running… → Done (with timestamp)
- Auto-reloads INSIGHTS tab on completion

---

### 📅 Game Schedule
- Full 2025-26 season calendar (regular season + playoffs)
- Browse any date with **← PREV / NEXT →** navigation and a **TODAY** shortcut
- Game cards show: `AWAY @ HOME`, team records, tip-off time, arena
- Live games pulse red; completed games show the final score (winner highlighted)
- Single API call for the entire season — cached 2 hours server-side

---

### ⚡ Parlay Lab

**Today's Games Picker**
- Opens directly to today's schedule — no manual team selection needed
- Tap any game pill (`PHX @ CHA  7:00 pm ET`) to instantly kick off the analysis
- Changing the L5 / L10 / L20 window re-runs for the selected game automatically

**Matchup Auto-Recommendations**
- Backend fetches top 5 players per team in parallel and generates the best 2-leg → 5-leg combinations ranked by empirical hit rate
- Stats evaluated: **PTS · REB · AST · 3PM · STL · BLK**
- Shows same-team combos for each side + cross-team combos (naive independence)
- One-click **Add to Builder** copies any recommendation into the manual builder
- Results cached in-session

**Manual Parlay Builder**
- Add any player (cache-first — instant if already searched)
- Pick stat and line; hit rates update instantly: L5 · L10 · L20 · Season
- **CALCULATE PARLAY** computes joint probability client-side:
  - **Same-team legs**: empirical co-occurrence — counts actual games where *all* conditions hit simultaneously
  - **Cross-team legs**: naive independence (product of individual rates)
  - **Correlation ratio** (empirical ÷ naive) — values > 1× mean legs are positively correlated

---

### Player Analysis

- **Player search** — find any active NBA player by name with typeahead autocomplete
- **OVERVIEW** — L5 / L10 / season trend table + minutes load bars (last 10 games)
- **PROPS** — set a prop line and instantly see SEASON / L10 / L5 hit rates with streak dots; home/away/B2B/rest splits
- **LOG** — full game log including **playoffs** (PO badge, subtle orange row tint): W/L · MIN · PTS · 3PM · 3PA · REB · AST · TOV · STL · BLK · FGM · FGA · FG% · FTM · FTA · FT% · PF · +/-
- **MATCHUP** — opponent defensive stats (pts/reb/ast/3PM allowed, DEF rating, pace) vs league average + head-to-head history with per-stat avg/min/max cards
- **INTEL strip** — auto-generated insight chips: FORM · LOAD · 3PT FORM · MATCHUP · CONSISTENCY · B2B RISK · USAGE
- **↓ SAVE JSON** — manual one-click export of full player data (no auto-download)

---

### League View
- **TEAMS tab** — all 30 teams ranked across key metrics with sortable columns
  - OFF / DEF / NET ratings, PACE, PTS scored, OPP PTS allowed, 3PM, 3PA, AST
  - Color-coded: top 8 green, bottom 8 red; fast pace cyan (>100), slow orange (<97)
  - Lazy-loads on first click, cached for the session

---

## Stack

| Layer | Tech |
|---|---|
| Frontend | Single-file HTML/CSS/JS (no build step) |
| Backend | Python · Flask · flask-cors |
| Data source | [nba_api](https://github.com/swar/nba_api) → stats.nba.com |
| Analysis | pandas · scikit-learn · Jupyter notebooks |

---

## Quick Start

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2. Start the Flask API server

```bash
python server.py
```

The API runs at `http://localhost:5000`.

### 3. Open the app

```
http://localhost:5000/nba-props-v3.html
```

The green pill in the top-right corner confirms the API is reachable.

---

## Project Structure

```
.
├── nba-props-v3.html          # Frontend SPA (no build step)
├── server.py                  # Flask API + analysis endpoints
├── requirements.txt           # Python dependencies
├── team_analysis/             # Precomputed analysis data
│   ├── ast_game_logs_2025_26.pkl
│   ├── synergy_offense_2025-26.csv
│   ├── synergy_defense_2025-26.csv
│   ├── team_advanced_2025-26.csv
│   ├── assist_prediction_analysis.ipynb
│   ├── assist_matchup_analysis.ipynb
│   └── preliminary_synergy_team_analysis.ipynb
└── README.md
```

---

## API Endpoints

### Core

| Endpoint | Description |
|---|---|
| `GET /api/health` | Server health check |
| `GET /api/player?name=<name>` | Full player stats, season averages, game log (reg season + playoffs), matchup defense |
| `GET /api/teams` | All 30 teams — ratings, pace, per-game offense & defense stats |
| `GET /api/schedule` | Full season schedule grouped by date (cached 2 hrs) |
| `GET /api/schedule?date=YYYY-MM-DD` | Games for a specific date |
| `GET /api/matchup-parlays?home=LAL&away=BOS&window=10` | Auto-recommended multi-leg parlays for a matchup |

### Analysis (INSIGHTS tab)

| Endpoint | Description |
|---|---|
| `GET /api/analysis/player-stats` | Season aggregates + reliability for all 91 tracked players |
| `GET /api/analysis/assist-matchup?player=<name>` | Per-opponent AST breakdown + defensive cluster heatmap data |
| `GET /api/analysis/coef-matrix?player=<name>` | Ridge regression coefficients (players × play-type defense) |
| `GET /api/analysis/synergy-ranks` | 30 teams × 11 play types — offense and defense rank matrices |
| `POST /api/analysis/refresh` | Trigger background re-execution of all analysis notebooks |
| `GET /api/analysis/refresh-status` | Poll notebook refresh progress |

---

## NBA API Data Sources

| Data | Endpoint | Fields |
|---|---|---|
| Player meta | `CommonPlayerInfo` | team, position, jersey |
| Game log (reg season) | `PlayerGameLog(Regular Season)` | all per-game box score stats |
| Game log (playoffs) | `PlayerGameLog(Playoffs)` | playoff box score stats |
| Advanced player stats | `LeagueDashPlayerStats(Advanced)` | USG%, TS%, NET_RATING |
| Next game | `ScoreboardV2` | opponent, date, home/away |
| Opp defense | `LeagueDashTeamStats(Opponent)` | pts/reb/ast/3PM allowed |
| Opp advanced | `LeagueDashTeamStats(Advanced)` | DEF_RATING, PACE |
| Team base stats | `LeagueDashTeamStats(Base)` | PTS, REB, AST, FG3M, FG3A, W/L |
| Parlay roster | `LeagueDashPlayerStats(Base)` | top players by MIN per team |
| Full schedule | `ScheduleLeagueV2` | all games, dates, arenas, scores |

---

## Correlation Ratio — the edge

```
correlation_ratio = empirical_joint_rate / naive_independent_rate
```

| Ratio | Meaning |
|---|---|
| > 1.05 | Legs are **positively correlated** — books may underprice this parlay |
| ≈ 1.00 | Approximately independent — no edge either way |
| < 0.95 | Legs are **negatively correlated** — avoid |

Same-team legs share the same games, so co-occurrence is measured directly. High-scoring, high-pace, blowout, or rest games inflate all counting stats together — that's the correlation source.

---

## Performance Notes

- Player fetch: ~15–25 seconds (NBA API rate-limits; server adds delays between calls)
- Parlay matchup analysis: ~20–40 seconds (10 player game logs fetched in parallel across both teams); cached in-session
- Schedule: instant after first load (full season fetched once, cached 2 hours server-side)
- Teams tab: ~15 seconds on first load (3 API calls); cached for the session
- INSIGHTS tab: instant (reads from precomputed pkl/CSV files on disk)
- Analysis refresh: ~5–10 minutes (re-runs 3 notebooks via `jupyter nbconvert`)
- Season is hardcoded to `2025-26` in `server.py` — update `SEASON` at the top each year
