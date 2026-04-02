# NBA Props Lab

A local web app for NBA player prop betting research. Pulls live stats directly from the NBA API and surfaces per-game breakdowns, prop hit rates, matchup defense data, team rankings, a full game schedule, and a parlay builder — all in a dark-mode single-page interface.

---

## Features

### 📅 Game Schedule
- Full 2025-26 season calendar (regular season + playoffs)
- Browse any date with **← PREV / NEXT →** navigation and a **TODAY** shortcut
- Game cards show: `AWAY @ HOME`, team records, tip-off time, arena
- Live games pulse red; completed games show the final score (winner highlighted)
- Single API call for the entire season — cached 2 hours server-side

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

### Player Analysis
- **Player search** — find any active NBA player by name
- **OVERVIEW** — L5 / L10 / season trend table + minutes load bars (last 10 games)
- **PROPS** — set a prop line and instantly see SEASON / L10 / L5 hit rates with streak dots; home/away/B2B/rest splits
- **LOG** — full game log: W/L, MIN, PTS, 3PM, 3PA, REB, AST, TOV, STL, BLK, +/- (last 30 games)
- **MATCHUP** — opponent defensive stats (pts/reb/ast/3PM allowed, DEF rating, pace) vs league average + head-to-head history
- **INTEL strip** — auto-generated insight chips: FORM · LOAD · 3PT FORM · MATCHUP · CONSISTENCY · B2B RISK · USAGE

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

---

## Quick Start

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2. Start the Flask API server (Terminal 1)

```bash
python server.py
```

The API runs at `http://localhost:5000`. Keep this terminal open.

### 3. Serve the frontend (Terminal 2)

```bash
python -m http.server 3000
```

### 4. Open the app

```
http://localhost:3000/nba-props-v3.html
```

The green pill in the top-right corner confirms the API is reachable.

---

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/health` | Server health check |
| `GET /api/player?name=<name>` | Full player stats, season averages, game log, matchup defense |
| `GET /api/teams` | All 30 teams — ratings, pace, per-game offense & defense stats |
| `GET /api/schedule` | Full season schedule grouped by date (cached 2 hrs) |
| `GET /api/schedule?date=YYYY-MM-DD` | Games for a specific date |
| `GET /api/matchup-parlays?home=LAL&away=BOS&window=10` | Auto-recommended multi-leg parlays for a matchup |

### `/api/schedule` response shape

```json
{
  "date": "2026-04-02",
  "count": 6,
  "games": [
    {
      "gameId": "0022501234",
      "date": "2026-04-02",
      "statusText": "7:00 pm ET",
      "status": 1,
      "tipEt": "00:00",
      "arenaName": "Spectrum Center",
      "arenaCity": "Charlotte",
      "arenaState": "NC",
      "homeTricode": "CHA", "homeName": "Hornets", "homeCity": "Charlotte",
      "homeWins": 18, "homeLosses": 55, "homeScore": null,
      "awayTricode": "PHX", "awayName": "Suns", "awayCity": "Phoenix",
      "awayWins": 31, "awayLosses": 42, "awayScore": null
    }
  ]
}
```

`status`: `1` = scheduled · `2` = live · `3` = final

### `/api/matchup-parlays` parameters

| Param | Values | Default |
|---|---|---|
| `home` | 3-letter team abbreviation (e.g. `LAL`) | required |
| `away` | 3-letter team abbreviation (e.g. `BOS`) | required |
| `window` | `5`, `10`, or `20` | `10` |

### `/api/matchup-parlays` response shape

```json
{
  "home_team": "LAL",
  "away_team": "BOS",
  "window": 10,
  "home_players": [{ "name": "LeBron James", "team": "LAL", "avg": { "pts": 22.0 } }],
  "away_players": [...],
  "home_combos": {
    "2": [
      {
        "legs": [
          { "player": "LeBron James", "stat": "pts", "line": 21.5, "individual_rate": 0.70 },
          { "player": "Anthony Davis", "stat": "reb", "line": 11.5, "individual_rate": 0.80 }
        ],
        "empirical_rate": 0.60,
        "naive_rate": 0.56,
        "correlation_ratio": 1.07,
        "hits": 6,
        "sample_size": 10
      }
    ],
    "3": [...], "4": [...], "5": [...]
  },
  "away_combos": { "2": [...], "3": [...], "4": [...], "5": [...] },
  "cross_team_combos": { "2": [...], "3": [...], "4": [...], "5": [...] },
  "warnings": []
}
```

### `/api/player` response shape

```json
{
  "name": "LeBron James",
  "team": "LAL",
  "opponent": "IND",
  "position": "Forward",
  "jersey": "23",
  "isHome": false,
  "gameDate": "2026-03-25",
  "seasonAvg": {
    "pts": 21.0, "3pm": 1.3, "reb": 6.0, "ast": 6.9,
    "min": 33.5, "usg": 26.0, "ts_pct": 59.2, "net_rating": 4.1
  },
  "games": [
    {
      "date": "2026-03-25", "opp": "IND", "home": false, "b2b": false,
      "wl": "W", "min": 34, "pts": 23, "3pm": 0, "fg3a": 1,
      "reb": 11, "ast": 7, "tov": 1, "stl": 1, "blk": 0, "pm": 24
    }
  ],
  "opponentDef": {
    "team": "IND", "ptsAllowed": 120.7, "rebAllowed": 46.7,
    "astAllowed": 26.6, "threesAllowed": 11.9,
    "defRating": 116.8, "pace": 101.2
  }
}
```

---

## NBA API Data Sources

| Data | Endpoint | Fields |
|---|---|---|
| Player meta | `CommonPlayerInfo` | team, position, jersey |
| Game log | `PlayerGameLog` | all per-game box score stats |
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

## Project Structure

```
.
├── nba-props-v3.html   # Frontend SPA (no build step)
├── server.py           # Flask API backend
├── requirements.txt    # Python dependencies
└── README.md
```

---

## Performance Notes

- Player fetch: ~15–25 seconds (NBA API rate-limits; server adds delays between calls)
- Parlay matchup analysis: ~20–40 seconds (10 player game logs fetched in parallel across both teams); cached in-session
- Schedule: instant after first load (full season fetched once, cached 2 hours server-side)
- Teams tab: ~15 seconds on first load (3 API calls); cached for the session
- Season is hardcoded to `2025-26` in `server.py` — update `SEASON` at the top each year
