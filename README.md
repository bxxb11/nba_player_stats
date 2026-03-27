# NBA Props Lab

A local web app for NBA player prop betting research. Pulls live stats directly from the NBA API and surfaces per-game breakdowns, prop hit rates, matchup defense data, team rankings, and at-a-glance intel chips — all in a dark-mode single-page interface.

---

## Features

### Player Analysis
- **Player search** — find any active NBA player by name
- **OVERVIEW** — L5 / L10 / season trend table + minutes load bars (last 10 games)
- **PROPS** — set a prop line for any stat and instantly see SEASON / L10 / L5 hit rates with streak dots; splits by home/away/B2B/rest
- **LOG** — full game log with W/L, MIN, PTS, 3PM, 3PA, REB, AST, TOV, STL, BLK, +/- (last 30 games)
- **MATCHUP** — opponent defensive stats (pts/reb/ast/3PM allowed, DEF rating, pace) vs league average + head-to-head history
- **INTEL strip** — auto-generated insight chips above all tabs: FORM · LOAD · 3PT FORM · MATCHUP · CONSISTENCY · B2B RISK · USAGE

### League View
- **TEAMS tab** — all 30 teams ranked across key metrics with sortable columns
  - OFF / DEF / NET ratings, PACE, PTS scored, OPP PTS allowed, 3PM, 3PA, AST
  - Color-coded: top 8 green, bottom 8 red per column; fast pace cyan (>100), slow pace orange (<97)
  - Pace explainer card: why pace matters for prop betting
  - Lazy-loads on first click, cached in memory for the session

### General
- **localStorage cache** — 2-hour TTL prevents redundant API calls; auto-downloads fetched data as JSON

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
    "defRating": 116.8, "pace": 101.2,
    "leagueAvgPts": 115.2, "leagueAvgReb": 43.8,
    "leagueAvgAst": 26.1, "leagueAvgThrees": 13.1
  }
}
```

### `/api/teams` response shape

```json
{
  "season": "2025-26",
  "teams": [
    {
      "team": "Oklahoma City Thunder",
      "w": 57, "l": 15,
      "pts": 118.7, "oppPts": 107.6,
      "reb": 44.1, "ast": 25.6,
      "fg3m": 13.6, "fg3a": 37.7, "oppFg3m": 14.3,
      "offRtg": 117.1, "defRtg": 106.2, "netRtg": 10.8,
      "pace": 100.4
    }
  ]
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

## Notes

- A fresh player fetch takes ~15–25 seconds (NBA API rate-limits requests; server adds sleep delays between calls).
- The TEAMS tab takes ~15 seconds on first load (3 API calls); result is cached for the session.
- `ScoreboardV2` has a known deprecation warning for 2025-26; next-game detection still works correctly.
- Season is hardcoded to `2025-26` in `server.py` — update `SEASON` at the top of the file each year.
