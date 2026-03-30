"""
NBA Props Lab – local stats API
Uses nba_api (stats.nba.com) as the data source.
Run: python server.py
"""

import datetime
import itertools
import math
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


def parse_game_log(gl_df):
    """Parse a PlayerGameLog DataFrame into standardized game dicts (newest-first).
    Returns list of dicts with date, opp, home, b2b, wl, min, pts, 3pm, fg3a,
    reb, oreb, dreb, ast, stl, blk, tov, pm, usg.
    """
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

    # mark back-to-backs (array is newest-first)
    for i in range(1, len(games)):
        try:
            d_new = datetime.datetime.strptime(games[i - 1]["date"], "%Y-%m-%d")
            d_old = datetime.datetime.strptime(games[i]["date"], "%Y-%m-%d")
            if (d_new - d_old).days == 1:
                games[i - 1]["b2b"] = True
        except Exception:
            pass

    return games


def auto_line(avg_val):
    """Compute auto betting line: 0.5 below rounded average.
    e.g. avg 22.3 → 21.5,  avg 8.7 → 7.5,  avg 1.2 → 0.5
    """
    return max(0.5, math.floor(avg_val * 2 - 1) / 2)


def generate_viable_legs(player_name, games, window):
    """Generate viable prop legs for a player given their game log.
    Stats: PTS, REB, AST, 3PM, STL, BLK
    Viable = avg >= min threshold AND hit rate >= 50% over the window.
    Returns list of {player, stat, line, individual_rate, hits, sample_size, avg}.
    """
    if not games:
        return []

    stat_configs = [
        {"key": "pts",  "label": "PTS",  "min_avg": 1.0},
        {"key": "reb",  "label": "REB",  "min_avg": 1.0},
        {"key": "ast",  "label": "AST",  "min_avg": 1.0},
        {"key": "3pm",  "label": "3PM",  "min_avg": 0.5},
        {"key": "stl",  "label": "STL",  "min_avg": 0.5},
        {"key": "blk",  "label": "BLK",  "min_avg": 0.5},
    ]

    slice_games = games[:window] if len(games) >= window else games
    n = len(slice_games)
    if n == 0:
        return []

    legs = []
    for sc in stat_configs:
        k = sc["key"]
        vals = [g.get(k, 0) or 0 for g in slice_games]
        avg_val = sum(vals) / n if n > 0 else 0

        if avg_val < sc["min_avg"]:
            continue

        line = auto_line(avg_val)
        hits = sum(1 for v in vals if v >= line)
        rate = hits / n

        if rate >= 0.50:
            legs.append({
                "player":          player_name,
                "stat":            k,
                "stat_label":      sc["label"],
                "line":            line,
                "individual_rate": round(rate, 3),
                "hits":            hits,
                "sample_size":     n,
                "avg":             round(avg_val, 1),
            })

    return legs


def compute_combos(all_legs, games_by_player, window, top_n=3):
    """For each leg count (2-5), compute empirical co-occurrence for all combinations.
    games_by_player: {player_name: [game_dicts]} (newest-first)
    Returns {2: [top_n], 3: [top_n], 4: [top_n], 5: [top_n]}.
    """
    if len(all_legs) < 2:
        return {}

    # Build date → player_stats lookup
    date_stats = {}  # date → {player_name: {stat: val}}
    for pname, pgames in games_by_player.items():
        for g in pgames[:window]:
            d = g["date"]
            if d not in date_stats:
                date_stats[d] = {}
            date_stats[d][pname] = g

    # Find shared dates (all players in this group played)
    players_in_legs = list({leg["player"] for leg in all_legs})
    shared_dates = sorted(
        [d for d, ps in date_stats.items() if all(p in ps for p in players_in_legs)],
        reverse=True
    )[:window]

    n_shared = len(shared_dates)
    if n_shared == 0:
        return {}

    results = {}
    max_legs = min(5, len(all_legs))

    for leg_count in range(2, max_legs + 1):
        combos = list(itertools.combinations(range(len(all_legs)), leg_count))
        scored = []

        for combo_indices in combos:
            combo_legs = [all_legs[i] for i in combo_indices]

            # Compute naive probability (product of individual rates within shared dates)
            # Re-compute individual rates on shared dates for accuracy
            leg_shared_rates = []
            for leg in combo_legs:
                pname = leg["player"]
                stat  = leg["stat"]
                line  = leg["line"]
                vals  = [date_stats[d][pname].get(stat, 0) or 0 for d in shared_dates]
                leg_hits = sum(1 for v in vals if v >= line)
                leg_shared_rates.append(leg_hits / n_shared if n_shared > 0 else 0)

            naive_rate = 1.0
            for r in leg_shared_rates:
                naive_rate *= r

            # Compute empirical (all legs hit on same date)
            empirical_hits = 0
            for d in shared_dates:
                all_hit = all(
                    (date_stats[d][leg["player"]].get(leg["stat"], 0) or 0) >= leg["line"]
                    for leg in combo_legs
                )
                if all_hit:
                    empirical_hits += 1

            empirical_rate = empirical_hits / n_shared

            # Correlation ratio
            corr_ratio = (empirical_rate / naive_rate) if naive_rate > 0 else 1.0

            scored.append({
                "legs":              combo_legs,
                "empirical_rate":    round(empirical_rate, 3),
                "naive_rate":        round(naive_rate, 3),
                "correlation_ratio": round(corr_ratio, 3),
                "hits":              empirical_hits,
                "sample_size":       n_shared,
            })

        # Sort by empirical rate descending, take top_n
        scored.sort(key=lambda x: x["empirical_rate"], reverse=True)
        results[str(leg_count)] = scored[:top_n]

    return results


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


@app.route("/api/players-list")
def players_list():
    """Return all active players as [{name}]. Uses local static data — instant, no API call."""
    active = nba_players.get_active_players()
    return jsonify([{"name": p["full_name"]} for p in active])


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

        # 4 · parse ALL season games (using shared helper)
        games = parse_game_log(gl_df)

        # 5 · season averages from full game log
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

        # 6 · advanced season stats (USG%, TS%, NET_RATING)
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

        # 7 · next game
        next_game = {"opponent": None, "date": None, "isHome": None}
        if team_abbr:
            try:
                time.sleep(0.6)
                next_game = get_next_game(team_abbr)
            except Exception:
                pass

        # 8 · opponent defensive stats
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


@app.route("/api/teams")
def team_rankings():
    """Return all 30 teams ranked by NET_RATING with offense, defense & pace metrics."""
    try:
        time.sleep(0.3)
        df_base = LeagueDashTeamStats(
            season=SEASON,
            per_mode_detailed="PerGame",
            measure_type_detailed_defense="Base",
            season_type_all_star=SEASON_TYPE,
            timeout=25,
        ).league_dash_team_stats.get_data_frame()

        time.sleep(0.4)
        df_opp = LeagueDashTeamStats(
            season=SEASON,
            per_mode_detailed="PerGame",
            measure_type_detailed_defense="Opponent",
            season_type_all_star=SEASON_TYPE,
            timeout=25,
        ).league_dash_team_stats.get_data_frame()

        time.sleep(0.4)
        df_adv = LeagueDashTeamStats(
            season=SEASON,
            per_mode_detailed="PerGame",
            measure_type_detailed_defense="Advanced",
            season_type_all_star=SEASON_TYPE,
            timeout=25,
        ).league_dash_team_stats.get_data_frame()

        merged = df_base.merge(
            df_opp[["TEAM_ID", "OPP_PTS", "OPP_REB", "OPP_FG3M"]], on="TEAM_ID"
        ).merge(
            df_adv[["TEAM_ID", "OFF_RATING", "DEF_RATING", "NET_RATING", "PACE"]], on="TEAM_ID"
        )
        merged = merged.sort_values("NET_RATING", ascending=False).reset_index(drop=True)

        teams = []
        for _, r in merged.iterrows():
            teams.append({
                "team":    str(r["TEAM_NAME"]),
                "w":       safe_int(r["W"]),
                "l":       safe_int(r["L"]),
                "pts":     safe_float(r["PTS"]),
                "oppPts":  safe_float(r["OPP_PTS"]),
                "reb":     safe_float(r["REB"]),
                "ast":     safe_float(r["AST"]),
                "fg3m":    safe_float(r["FG3M"]),
                "fg3a":    safe_float(r["FG3A"]),
                "oppFg3m": safe_float(r["OPP_FG3M"]),
                "offRtg":  safe_float(r["OFF_RATING"]),
                "defRtg":  safe_float(r["DEF_RATING"]),
                "netRtg":  safe_float(r["NET_RATING"]),
                "pace":    safe_float(r["PACE"]),
            })

        return jsonify({"teams": teams, "season": SEASON})

    except Exception as e:
        return jsonify({"error": str(e), "detail": traceback.format_exc()}), 500


@app.route("/api/matchup-parlays")
def matchup_parlays():
    """Auto-recommend top parlay combos for a given matchup (home vs away teams).
    Query params: home (abbr), away (abbr), window (5/10/20, default 10).

    Steps:
    1. LeagueDashPlayerStats(Base) → get top 5 players by MIN for each team
    2. PlayerGameLog for each player (with rate limiting)
    3. Generate viable prop legs per player
    4. Compute empirical co-occurrence for same-team combos (2-5 legs)
    5. Compute naive cross-team combos (best individual rates from each team)
    6. Return top 3 per leg count for home, away, and cross-team
    """
    home_abbr = request.args.get("home", "").strip().upper()
    away_abbr = request.args.get("away", "").strip().upper()
    try:
        window = max(5, min(20, int(request.args.get("window", "10"))))
    except ValueError:
        window = 10

    if not home_abbr or not away_abbr:
        return jsonify({"error": "home and away parameters required"}), 400
    if home_abbr == away_abbr:
        return jsonify({"error": "home and away teams must be different"}), 400

    # Validate team abbreviations
    home_info = nba_teams.find_team_by_abbreviation(home_abbr)
    away_info = nba_teams.find_team_by_abbreviation(away_abbr)
    if not home_info:
        return jsonify({"error": f'Unknown team: {home_abbr}'}), 400
    if not away_info:
        return jsonify({"error": f'Unknown team: {away_abbr}'}), 400

    warnings = []

    try:
        # ── Step 1: Get all players for both teams via LeagueDashPlayerStats ──
        time.sleep(0.4)
        base_ep = LeagueDashPlayerStats(
            season=SEASON,
            season_type_all_star=SEASON_TYPE,
            measure_type_detailed_defense="Base",
            per_mode_detailed="PerGame",
            timeout=30,
        )
        all_players_df = base_ep.league_dash_player_stats.get_data_frame()

        home_id = int(home_info["id"])
        away_id = int(away_info["id"])

        home_players_df = all_players_df[all_players_df["TEAM_ID"] == home_id].copy()
        away_players_df = all_players_df[all_players_df["TEAM_ID"] == away_id].copy()

        # Sort by MIN descending, take top 5
        home_players_df = home_players_df.sort_values("MIN", ascending=False).head(5)
        away_players_df = away_players_df.sort_values("MIN", ascending=False).head(5)

        if home_players_df.empty:
            return jsonify({"error": f"No player data found for {home_abbr}"}), 404
        if away_players_df.empty:
            return jsonify({"error": f"No player data found for {away_abbr}"}), 404

        # ── Step 2: Fetch game logs for each player ──
        def fetch_team_game_logs(players_df, team_abbr):
            """Fetch game logs for players in a DataFrame. Returns {name: games}."""
            player_games = {}
            player_meta  = []
            for _, row in players_df.iterrows():
                pid   = int(row["PLAYER_ID"])
                pname = str(row["PLAYER_NAME"])
                p_avg = {
                    "pts": safe_float(row.get("PTS")),
                    "reb": safe_float(row.get("REB")),
                    "ast": safe_float(row.get("AST")),
                    "3pm": safe_float(row.get("FG3M")),
                    "stl": safe_float(row.get("STL")),
                    "blk": safe_float(row.get("BLK")),
                    "min": safe_float(row.get("MIN")),
                }
                try:
                    time.sleep(0.6)
                    gl_ep = PlayerGameLog(
                        player_id=pid,
                        season=SEASON,
                        season_type_all_star=SEASON_TYPE,
                        timeout=20,
                    )
                    gl_df = gl_ep.player_game_log.get_data_frame()
                    games = parse_game_log(gl_df)
                    # Only include players with enough games
                    if len(games) >= 5:
                        player_games[pname] = games
                        player_meta.append({"name": pname, "team": team_abbr, "avg": p_avg, "gamesPlayed": len(games)})
                    else:
                        warnings.append(f"{pname}: only {len(games)} games — skipped")
                except Exception as ex:
                    warnings.append(f"Failed to fetch {pname}: {str(ex)}")
            return player_games, player_meta

        home_games, home_meta = fetch_team_game_logs(home_players_df, home_abbr)
        away_games, away_meta = fetch_team_game_logs(away_players_df, away_abbr)

        # ── Step 3: Generate viable legs per team ──
        home_all_legs = []
        for pname, pgames in home_games.items():
            home_all_legs.extend(generate_viable_legs(pname, pgames, window))

        away_all_legs = []
        for pname, pgames in away_games.items():
            away_all_legs.extend(generate_viable_legs(pname, pgames, window))

        if window < 10:
            warnings.append(f"Small window ({window} games) — treat recommendations as directional only")

        # ── Step 4: Compute same-team combos ──
        home_combos = compute_combos(home_all_legs, home_games, window)
        away_combos = compute_combos(away_all_legs, away_games, window)

        # ── Step 5: Cross-team combos (naive independence) ──
        # Take best legs from each team (top 4 by individual rate) and cross them
        cross_combos = {}
        h_top = sorted(home_all_legs, key=lambda x: x["individual_rate"], reverse=True)[:4]
        a_top = sorted(away_all_legs, key=lambda x: x["individual_rate"], reverse=True)[:4]
        cross_all = list(itertools.product(h_top, a_top))

        for leg_count in range(2, 6):
            cross_scored = []
            # For cross-team, take 1 from each team and optionally more from same team
            # Start with 1 home + 1 away pairs, then extend
            if leg_count == 2:
                candidates = [list(pair) for pair in cross_all]
            elif leg_count == 3:
                # 2 home + 1 away, or 1 home + 2 away
                candidates = []
                h2 = list(itertools.combinations(h_top, 2))
                for hpair in h2:
                    for a_leg in a_top:
                        candidates.append(list(hpair) + [a_leg])
                a2 = list(itertools.combinations(a_top, 2))
                for apair in a2:
                    for h_leg in h_top:
                        candidates.append([h_leg] + list(apair))
            elif leg_count == 4:
                candidates = []
                h2 = list(itertools.combinations(h_top, 2))
                a2 = list(itertools.combinations(a_top, 2))
                for hpair in h2:
                    for apair in a2:
                        candidates.append(list(hpair) + list(apair))
            elif leg_count == 5:
                candidates = []
                h3 = list(itertools.combinations(h_top, 3))
                for htrio in h3:
                    for apair in list(itertools.combinations(a_top, 2)):
                        candidates.append(list(htrio) + list(apair))
                a3 = list(itertools.combinations(a_top, 3))
                for atrio in a3:
                    for hpair in list(itertools.combinations(h_top, 2)):
                        candidates.append(list(hpair) + list(atrio))

            for combo_legs in candidates:
                naive_rate = 1.0
                for leg in combo_legs:
                    naive_rate *= leg["individual_rate"]
                cross_scored.append({
                    "legs":              combo_legs,
                    "empirical_rate":    None,   # cross-team: no empirical
                    "naive_rate":        round(naive_rate, 3),
                    "correlation_ratio": None,
                    "hits":              None,
                    "sample_size":       window,
                })

            cross_scored.sort(key=lambda x: x["naive_rate"], reverse=True)
            cross_combos[str(leg_count)] = cross_scored[:3]

        return jsonify({
            "home_team":         home_abbr,
            "away_team":         away_abbr,
            "window":            window,
            "home_players":      home_meta,
            "away_players":      away_meta,
            "home_combos":       home_combos,
            "away_combos":       away_combos,
            "cross_team_combos": cross_combos,
            "warnings":          warnings,
        })

    except Exception as e:
        return jsonify({"error": str(e), "detail": traceback.format_exc()}), 500


if __name__ == "__main__":
    print("NBA Props Lab API  →  http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
