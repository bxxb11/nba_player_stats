"""
NBA Props Lab – local stats API
Uses nba_api (stats.nba.com) as the data source.
Run: python server.py
"""

import datetime
import time
import traceback

from flask import Flask, jsonify, request
from flask_cors import CORS

from nba_api.stats.endpoints import (
    PlayerGameLog,
    CommonPlayerInfo,
    LeagueDashTeamStats,
    LeagueDashPlayerStats,
    ScoreboardV2,
)
from nba_api.stats.static import players as nba_players, teams as nba_teams

app = Flask(__name__)
CORS(app)

SEASON = "2025-26"
SEASON_TYPE = "Regular Season"


# ── helpers ─────────────────────────────────────────────────────────────────

def parse_min(val):
    """'35:22' → 35.4  or  '35.0' → 35.0"""
    try:
        s = str(val)
        if ":" in s:
            m, sec = s.split(":")
            return round(int(m) + int(sec) / 60, 1)
        return round(float(s), 1)
    except Exception:
        return 0.0


def safe_int(v):
    try:
        return int(v or 0)
    except Exception:
        return 0


def safe_float(v, precision=1):
    try:
        return round(float(v or 0), precision)
    except Exception:
        return 0.0


def find_player(name: str):
    """Fuzzy player search; prefers active players."""
    results = nba_players.find_players_by_full_name(name)
    if results:
        active = [p for p in results if p.get("is_active")]
        return (active or results)[0]

    name_lc = name.lower().strip()
    active_all = nba_players.get_active_players()
    matches = [p for p in active_all if name_lc in p["full_name"].lower()]
    if matches:
        return matches[0]

    last = name_lc.split()[-1] if name_lc.split() else ""
    matches = [p for p in active_all if last in p["full_name"].lower()]
    return matches[0] if matches else None


def get_next_game(team_abbr: str):
    """Scan the next 8 days for a scheduled game; return opponent, date, isHome."""
    team_info = nba_teams.find_team_by_abbreviation(team_abbr)
    team_id = int(team_info["id"]) if team_info else None

    today = datetime.date.today()
    for delta in range(0, 8):
        check = today + datetime.timedelta(days=delta)
        date_str = check.strftime("%m/%d/%Y")
        try:
            sb = ScoreboardV2(game_date=date_str, timeout=20)
            ls = sb.line_score.get_data_frame()
            gh = sb.game_header.get_data_frame()

            team_rows = ls[ls["TEAM_ABBREVIATION"] == team_abbr]
            if team_rows.empty:
                time.sleep(0.3)
                continue

            game_id = team_rows.iloc[0]["GAME_ID"]
            opp_rows = ls[
                (ls["GAME_ID"] == game_id) & (ls["TEAM_ABBREVIATION"] != team_abbr)
            ]
            opp_abbr = (
                opp_rows.iloc[0]["TEAM_ABBREVIATION"] if not opp_rows.empty else None
            )

            is_home = False
            if team_id is not None:
                gh_row = gh[gh["GAME_ID"] == game_id]
                if not gh_row.empty:
                    is_home = int(gh_row.iloc[0]["HOME_TEAM_ID"]) == team_id

            return {"opponent": opp_abbr, "date": check.isoformat(), "isHome": is_home}

        except Exception:
            time.sleep(0.5)

    return {"opponent": None, "date": None, "isHome": None}


def get_opp_def_stats(opp_abbr: str):
    """Return per-game opponent (defense-allowed) stats for a team."""
    try:
        # Call 1 — Opponent measure: points/reb/ast/3pm allowed
        resp_opp = LeagueDashTeamStats(
            season=SEASON,
            per_mode_detailed="PerGame",
            measure_type_detailed_defense="Opponent",
            season_type_all_star=SEASON_TYPE,
            timeout=20,
        )
        df_opp = resp_opp.league_dash_team_stats.get_data_frame()
        # Opponent measure returns TEAM_ID/TEAM_NAME, not TEAM_ABBREVIATION
        team_info = nba_teams.find_team_by_abbreviation(opp_abbr)
        if not team_info:
            return None
        team_id = int(team_info["id"])
        row_opp = df_opp[df_opp["TEAM_ID"] == team_id]
        if row_opp.empty:
            return None
        r = row_opp.iloc[0]

        # Compute real league averages from the full 30-team df
        league_avg_pts    = safe_float(df_opp["OPP_PTS"].mean(), 1)
        league_avg_reb    = safe_float(df_opp["OPP_REB"].mean(), 1)
        league_avg_ast    = safe_float(df_opp["OPP_AST"].mean(), 1)
        league_avg_threes = safe_float(df_opp["OPP_FG3M"].mean(), 1)

        # Call 2 — Advanced measure: DEF_RATING + PACE
        time.sleep(0.4)
        resp_adv = LeagueDashTeamStats(
            season=SEASON,
            per_mode_detailed="PerGame",
            measure_type_detailed_defense="Advanced",
            season_type_all_star=SEASON_TYPE,
            timeout=20,
        )
        df_adv = resp_adv.league_dash_team_stats.get_data_frame()
        row_adv = df_adv[df_adv["TEAM_ID"] == team_id]
        def_rating = safe_float(row_adv.iloc[0].get("DEF_RATING"), 1) if not row_adv.empty else 0.0
        pace       = safe_float(row_adv.iloc[0].get("PACE"), 1)       if not row_adv.empty else 0.0

        return {
            "team":            opp_abbr,
            "ptsAllowed":      safe_float(r.get("OPP_PTS")),
            "rebAllowed":      safe_float(r.get("OPP_REB")),
            "astAllowed":      safe_float(r.get("OPP_AST")),
            "threesAllowed":   safe_float(r.get("OPP_FG3M")),
            "defRating":       def_rating,
            "pace":            pace,
            "leagueAvgPts":    league_avg_pts,
            "leagueAvgReb":    league_avg_reb,
            "leagueAvgAst":    league_avg_ast,
            "leagueAvgThrees": league_avg_threes,
        }
    except Exception:
        return None


# ── routes ───────────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/player")
def player_stats():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "name parameter required"}), 400

    try:
        # 1 · find player id
        player = find_player(name)
        if not player:
            return jsonify({"error": f'Player "{name}" not found'}), 404
        player_id = player["id"]
        time.sleep(0.6)

        # 2 · player meta (team, position, jersey)
        info_ep = CommonPlayerInfo(player_id=player_id, timeout=20)
        info_df = info_ep.common_player_info.get_data_frame()
        if info_df.empty:
            return jsonify({"error": "Player info unavailable"}), 404
        meta = info_df.iloc[0]

        full_name = meta.get("DISPLAY_FIRST_LAST", player["full_name"])
        team_abbr = meta.get("TEAM_ABBREVIATION", "")
        position  = meta.get("POSITION", "")
        jersey    = str(meta.get("JERSEY", ""))
        time.sleep(0.6)

        # 3 · full-season game log (all games, newest-first)
        gl_ep = PlayerGameLog(
            player_id=player_id,
            season=SEASON,
            season_type_all_star=SEASON_TYPE,
            timeout=20,
        )
        gl_df = gl_ep.player_game_log.get_data_frame()

        # 4 · parse ALL season games
        games = []
        for _, g in gl_df.iterrows():
            matchup = str(g.get("MATCHUP", ""))
            is_home = "vs." in matchup
            if "vs." in matchup:
                opp = matchup.split("vs.")[-1].strip()
            elif "@" in matchup:
                opp = matchup.split("@")[-1].strip()
            else:
                opp = matchup.split()[-1]

            raw_date = str(g.get("GAME_DATE", ""))
            try:
                date_iso = datetime.datetime.strptime(raw_date, "%b %d, %Y").strftime("%Y-%m-%d")
            except Exception:
                date_iso = raw_date

            games.append({
                "date":  date_iso,
                "opp":   opp,
                "home":  is_home,
                "b2b":   False,
                "wl":    str(g.get("WL", "")),
                "min":   parse_min(g.get("MIN", 0)),
                "pts":   safe_int(g.get("PTS")),
                "3pm":   safe_int(g.get("FG3M")),
                "fg3a":  safe_int(g.get("FG3A")),
                "reb":   safe_int(g.get("REB")),
                "oreb":  safe_int(g.get("OREB")),
                "dreb":  safe_int(g.get("DREB")),
                "ast":   safe_int(g.get("AST")),
                "stl":   safe_int(g.get("STL")),
                "blk":   safe_int(g.get("BLK")),
                "tov":   safe_int(g.get("TOV")),
                "pm":    safe_int(g.get("PLUS_MINUS")),
                "usg":   0,
            })

        # 5 · mark back-to-backs (array is newest-first)
        for i in range(1, len(games)):
            try:
                d_new = datetime.datetime.strptime(games[i - 1]["date"], "%Y-%m-%d")
                d_old = datetime.datetime.strptime(games[i]["date"], "%Y-%m-%d")
                if (d_new - d_old).days == 1:
                    games[i - 1]["b2b"] = True
            except Exception:
                pass

        # 6 · season averages from full game log
        if not gl_df.empty:
            season_avg = {
                "pts": safe_float(gl_df["PTS"].mean()),
                "3pm": safe_float(gl_df["FG3M"].mean()),
                "reb": safe_float(gl_df["REB"].mean()),
                "ast": safe_float(gl_df["AST"].mean()),
                "stl": safe_float(gl_df["STL"].mean()),
                "blk": safe_float(gl_df["BLK"].mean()),
                "min": safe_float(gl_df["MIN"].apply(parse_min).mean()),
                "tov": safe_float(gl_df["TOV"].mean()),
                # advanced fields filled below
                "usg": 0.0,
                "ts_pct": 0.0,
                "net_rating": 0.0,
            }
        else:
            season_avg = {k: 0 for k in ["pts","3pm","reb","ast","stl","blk","min","tov","usg","ts_pct","net_rating"]}

        # 7 · advanced season stats (USG%, TS%, NET_RATING)
        try:
            time.sleep(0.6)
            adv_ep = LeagueDashPlayerStats(
                season=SEASON,
                season_type_all_star=SEASON_TYPE,
                measure_type_detailed_defense="Advanced",
                per_mode_detailed="PerGame",
                timeout=20,
            )
            adv_df = adv_ep.league_dash_player_stats.get_data_frame()
            adv_row = adv_df[adv_df["PLAYER_ID"] == int(player_id)]
            if not adv_row.empty:
                ar = adv_row.iloc[0]
                usg_raw = float(ar.get("USG_PCT") or 0)
                ts_raw  = float(ar.get("TS_PCT")  or 0)
                # nba_api returns these as decimals (0.28), convert to percent
                season_avg["usg"]        = round(usg_raw * 100 if usg_raw < 1.0 else usg_raw, 1)
                season_avg["ts_pct"]     = round(ts_raw  * 100 if ts_raw  < 1.0 else ts_raw,  1)
                season_avg["net_rating"] = safe_float(ar.get("NET_RATING"))
        except Exception:
            pass

        # 8 · next game
        next_game = {"opponent": None, "date": None, "isHome": None}
        if team_abbr:
            try:
                time.sleep(0.6)
                next_game = get_next_game(team_abbr)
            except Exception:
                pass

        # 9 · opponent defensive stats
        opp_def = None
        if next_game["opponent"]:
            try:
                time.sleep(0.6)
                opp_def = get_opp_def_stats(next_game["opponent"])
            except Exception:
                pass

        return jsonify({
            "name":        full_name,
            "team":        team_abbr,
            "opponent":    next_game["opponent"],
            "position":    position,
            "jersey":      jersey,
            "isHome":      next_game["isHome"],
            "gameDate":    next_game["date"],
            "seasonAvg":   season_avg,
            "quarterAvg":  {"q1": 0, "q2": 0, "q3": 0, "q4": 0},
            "games":       games,
            "opponentDef": opp_def,
            "riskFlags":   [],
            "source":      "stats.nba.com via nba_api",
        })

    except Exception as e:
        return jsonify({"error": str(e), "detail": traceback.format_exc()}), 500


if __name__ == "__main__":
    print("NBA Props Lab API  →  http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
