# NBA Props Lab

A local web app for NBA player prop betting research. Pulls live stats directly from the NBA API and surfaces per-game breakdowns, prop hit rates, matchup defense data, and at-a-glance intel chips — all in a dark-mode single-page interface.

![NBA Props Lab Screenshot](https://i.imgur.com/placeholder.png)

---

## Features

- **Player search** — find any active NBA player by name
- **OVERVIEW** — L5 / L10 / season trend table + minutes load bars
- **PROPS** — set a prop line for any stat and instantly see SEASON / L10 / L5 hit rates with streak dots
- **LOG** — full game log with W/L, MIN, PTS, 3PM, 3PA, REB, AST, TOV, STL, BLK, +/- (last 30 games)
- **MATCHUP** — opponent defensive stats (pts/reb/ast/3PM allowed, DEF rating, pace) vs league average
- **INTEL strip** — auto-generated insight chips: FORM · LOAD · 3PT FORM · MATCHUP · CONSISTENCY · B2B RISK · USAGE
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

### 2. Start the Flask API server

```bash
python server.py
```

The API runs at `http://localhost:5000`.

### 3. Serve the frontend

Any static file server works. The simplest option:

```bash
python -m http.server 3000
```

Then open `http://localhost:3000/nba-props-v3.html` in your browser.

---

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/health` | Server health check |
| `GET /api/player?name=<name>` | Full player stats, season averages, game log, matchup defense |

### Example response shape (`/api/player`)

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

---

## NBA API Data Sources

| Data | Endpoint | Fields |
|---|---|---|
| Player meta | `CommonPlayerInfo` | team, position, jersey |
| Game log | `PlayerGameLog` | all per-game box score stats |
| Advanced stats | `LeagueDashPlayerStats(Advanced)` | USG%, TS%, NET_RATING |
| Next game | `ScoreboardV2` | opponent, date, home/away |
| Opp defense | `LeagueDashTeamStats(Opponent)` | pts/reb/ast/3PM allowed |
| Opp advanced | `LeagueDashTeamStats(Advanced)` | DEF_RATING, PACE |

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

- The NBA API rate-limits requests — the server adds small sleep delays between calls. A fresh player fetch takes ~15–25 seconds.
- `ScoreboardV2` has a known deprecation warning for 2025-26 early season games; next-game detection still works correctly for current dates.
- Season is hardcoded to `2025-26` in `server.py` — update `SEASON` at the top of the file each year.
