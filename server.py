"""
NBA Props Lab – local stats API
Uses nba_api (stats.nba.com) as the data source.
Run: python server.py
"""

import datetime
import heapq
import itertools
import math
import os
import pickle
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

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

# ── Harden nba_api against ConnectionResetError (10054) ─────────────────────
# stats.nba.com drops connections from requests that look like bots.
# Setting browser-like headers and the required NBA-specific tokens prevents this.
try:
    from nba_api.stats.library import http as _nba_http
    _nba_http.STATS_HEADERS = {
        "Accept":               "application/json, text/plain, */*",
        "Accept-Encoding":      "gzip, deflate, br",
        "Accept-Language":      "en-US,en;q=0.9",
        "Connection":           "keep-alive",
        "Host":                 "stats.nba.com",
        "Origin":               "https://www.nba.com",
        "Referer":              "https://www.nba.com/",
        "User-Agent":           (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "x-nba-stats-origin":   "stats",
        "x-nba-stats-token":    "true",
    }
except Exception:
    pass  # non-fatal; fall back to default headers


def nba_call(fn, retries=3, base_delay=2.0):
    """Call an nba_api endpoint factory function with exponential backoff.

    fn      — zero-argument callable that creates and returns the endpoint object
    retries — max attempts (default 3)
    Retries on ConnectionReset / RemoteDisconnected / timeout errors.
    """
    _reset_signals = ("10054", "connectionreset", "connection aborted",
                      "remotedisconnected", "timeout", "timed out")
    for attempt in range(retries):
        try:
            return fn()
        except Exception as exc:
            msg = str(exc).lower()
            is_transient = any(sig in msg for sig in _reset_signals)
            if is_transient and attempt < retries - 1:
                wait = base_delay * (2 ** attempt)   # 2 s → 4 s → ...
                time.sleep(wait)
            else:
                raise


app = Flask(__name__)
CORS(app)

SEASON = "2025-26"
SEASON_TYPE = "Regular Season"
_TEAM_ANALYSIS_DIR = "team_analysis"

_insights_cache = {}
_refresh_status = {"status": "idle", "last": None, "error": None}

_team_profiles_cache = {"data": None, "ts": 0}
_TEAM_PROFILES_TTL = 14400  # 4 hours

# (raw_field, dim_key, inverted)
_PROFILE_DIMS = [
    ("off_rating",     "off_volume",          False),
    ("pct_fga_3pt",    "three_pt_attack",     False),
    ("pct_pts_paint",  "paint_attack",        False),
    ("pace",           "pace_tempo",          False),
    ("ast_pct",        "ball_movement",       False),
    ("fta",            "ft_generation",       False),
    ("def_rating",     "def_overall",         True ),
    ("opp_fg3m",       "three_pt_defense",    True ),
    ("opp_pts_paint",  "paint_defense",       True ),
    ("dreb_pct",       "rebound_defense",     False),
    ("opp_tov",        "turnover_force",      False),
    ("opp_pts_fb",     "transition_defense",  True ),
]

_DIM_META = {
    "off_volume":          {"label": "Scoring Volume",    "metric": "OFF RTG"},
    "three_pt_attack":     {"label": "3-Pt Attack",       "metric": "% FGA 3PT"},
    "paint_attack":        {"label": "Paint Dominance",   "metric": "% PTS Paint"},
    "pace_tempo":          {"label": "Pace & Tempo",      "metric": "PACE"},
    "ball_movement":       {"label": "Ball Movement",     "metric": "AST%"},
    "ft_generation":       {"label": "FT Generation",     "metric": "FTA/g"},
    "def_overall":         {"label": "Overall Defense",   "metric": "DEF RTG"},
    "three_pt_defense":    {"label": "3-Pt Defense",      "metric": "OPP 3PM"},
    "paint_defense":       {"label": "Paint Defense",     "metric": "OPP PTS Paint"},
    "rebound_defense":     {"label": "Rebound Defense",   "metric": "DREB%"},
    "turnover_force":      {"label": "Turnover Forcing",  "metric": "OPP TOV/g"},
    "transition_defense":  {"label": "Transition Defense","metric": "OPP PTS FB"},
}

_MATCHUP_PAIRS = [
    ("three_pt_attack",  "three_pt_defense",   0.25),
    ("paint_attack",     "paint_defense",       0.25),
    ("pace_tempo",       "transition_defense",  0.20),
    ("ball_movement",    "turnover_force",      0.15),
    ("ft_generation",    "def_overall",         0.15),
]

_MATCHUP_LABELS = [
    (75, "Favorable Matchup"),
    (55, "Slight Edge"),
    (45, "Even"),
    (25, "Slight Disadvantage"),
    (0,  "Tough Matchup"),
]


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


# ── Team-profile helpers ─────────────────────────────────────────────────────

def _get_team_row(df, team_id):
    if df is None:
        return None
    m = df[df["TEAM_ID"] == team_id]
    return m.iloc[0] if not m.empty else None


def _sf(row, field, prec=1):
    if row is None:
        return 0.0
    return safe_float(row.get(field), prec)


def _pct_rank(val, all_vals, inverted=False):
    n = len(all_vals)
    if n == 0:
        return 50
    count = sum(1 for v in all_vals if (v >= val if inverted else v <= val))
    return max(1, min(100, round(count / n * 100)))


def _fetch_all_team_data():
    abbr_lookup = {int(t["id"]): t["abbreviation"] for t in nba_teams.get_teams()}

    time.sleep(0.3)
    df_base = nba_call(lambda: LeagueDashTeamStats(
        season=SEASON, per_mode_detailed="PerGame",
        measure_type_detailed_defense="Base",
        season_type_all_star=SEASON_TYPE, timeout=25,
    )).league_dash_team_stats.get_data_frame()

    time.sleep(0.4)
    df_opp = nba_call(lambda: LeagueDashTeamStats(
        season=SEASON, per_mode_detailed="PerGame",
        measure_type_detailed_defense="Opponent",
        season_type_all_star=SEASON_TYPE, timeout=25,
    )).league_dash_team_stats.get_data_frame()

    time.sleep(0.4)
    df_adv = nba_call(lambda: LeagueDashTeamStats(
        season=SEASON, per_mode_detailed="PerGame",
        measure_type_detailed_defense="Advanced",
        season_type_all_star=SEASON_TYPE, timeout=25,
    )).league_dash_team_stats.get_data_frame()

    time.sleep(0.4)
    df_scr = None
    try:
        df_scr = nba_call(lambda: LeagueDashTeamStats(
            season=SEASON, per_mode_detailed="PerGame",
            measure_type_detailed_defense="Scoring",
            season_type_all_star=SEASON_TYPE, timeout=25,
        )).league_dash_team_stats.get_data_frame()
    except Exception:
        pass

    time.sleep(0.4)
    df_def = None
    try:
        df_def = nba_call(lambda: LeagueDashTeamStats(
            season=SEASON, per_mode_detailed="PerGame",
            measure_type_detailed_defense="Defense",
            season_type_all_star=SEASON_TYPE, timeout=25,
        )).league_dash_team_stats.get_data_frame()
    except Exception:
        pass

    teams = []
    for _, r in df_base.iterrows():
        tid = int(r["TEAM_ID"])
        ro  = _get_team_row(df_opp, tid)
        ra  = _get_team_row(df_adv, tid)
        rs  = _get_team_row(df_scr, tid)
        rd  = _get_team_row(df_def, tid)
        teams.append({
            "team_id":       tid,
            "team_abbr":     abbr_lookup.get(tid, ""),
            "team_name":     str(r.get("TEAM_NAME", "")),
            "fta":           _sf(r,  "FTA"),
            "off_rating":    _sf(ra, "OFF_RATING"),
            "def_rating":    _sf(ra, "DEF_RATING"),
            "pace":          _sf(ra, "PACE"),
            "ast_pct":       _sf(ra, "AST_PCT", 3),
            "dreb_pct":      _sf(ra, "DREB_PCT", 3),
            "opp_fg3m":      _sf(ro, "OPP_FG3M"),
            "opp_tov":       _sf(ro, "OPP_TOV"),
            "opp_pts":       _sf(ro, "OPP_PTS"),
            "pct_fga_3pt":   _sf(rs, "PCT_FGA_3PT",   3),
            "pct_pts_paint": _sf(rs, "PCT_PTS_PAINT",  3),
            "pct_pts_fb":    _sf(rs, "PCT_PTS_FB",     3),
            "opp_pts_paint": _sf(rd, "OPP_PTS_PAINT"),
            "opp_pts_fb":    _sf(rd, "OPP_PTS_FB"),
        })
    return teams


def _add_percentile_ranks(teams):
    all_vals = {field: [t[field] for t in teams] for field, _, _ in _PROFILE_DIMS}
    for t in teams:
        t["pct_ranks"] = {
            dim_key: _pct_rank(t[field], all_vals[field], inv)
            for field, dim_key, inv in _PROFILE_DIMS
        }
    return teams


def _classify_off_style(off_scores):
    s = off_scores
    if s.get("three_pt_attack", 50) > 70:
        return "3-Point Heavy"
    if s.get("paint_attack", 50) > 70 and s.get("three_pt_attack", 50) < 50:
        return "Paint Dominant"
    if s.get("pace_tempo", 50) > 70:
        return "Fast Pace"
    if s.get("pace_tempo", 50) < 30:
        return "Grind / Halfcourt"
    return "Balanced"


def _build_team_profile(team):
    pr = team.get("pct_ranks", {})
    offense, defense = {}, {}
    for field, dim_key, _ in _PROFILE_DIMS[:6]:
        offense[dim_key] = {
            "score":  pr.get(dim_key, 50),
            "label":  _DIM_META[dim_key]["label"],
            "metric": _DIM_META[dim_key]["metric"],
            "value":  team.get(field, 0),
        }
    for field, dim_key, _ in _PROFILE_DIMS[6:]:
        defense[dim_key] = {
            "score":  pr.get(dim_key, 50),
            "label":  _DIM_META[dim_key]["label"],
            "metric": _DIM_META[dim_key]["metric"],
            "value":  team.get(field, 0),
        }
    off_scores = {k: v["score"] for k, v in offense.items()}
    return {
        "team":     team["team_abbr"],
        "teamName": team["team_name"],
        "offStyle": _classify_off_style(off_scores),
        "offense":  offense,
        "defense":  defense,
    }


def _compute_matchup_score(att, def_):
    breakdown = []
    weighted_sum = 0.0
    for off_dim, def_dim, weight in _MATCHUP_PAIRS:
        off_sc = att["offense"][off_dim]["score"]
        def_sc = def_["defense"][def_dim]["score"]
        delta  = off_sc - (100 - def_sc)
        weighted_sum += delta * weight
        if delta > 15:
            verdict = "Clear advantage"
        elif delta > 0:
            verdict = "Slight edge"
        elif delta < -15:
            verdict = "Clear disadvantage"
        else:
            verdict = "Even"
        breakdown.append({
            "offDim":   off_dim,
            "defDim":   def_dim,
            "offLabel": att["offense"][off_dim]["label"],
            "defLabel": def_["defense"][def_dim]["label"],
            "offScore": off_sc,
            "defScore": def_sc,
            "delta":    round(delta, 1),
            "verdict":  verdict,
        })
    score = max(0, min(100, round((weighted_sum + 100) / 2)))
    label = next(
        (lbl for thresh, lbl in _MATCHUP_LABELS if score >= thresh),
        "Tough Matchup"
    )
    return {"score": score, "label": label, "breakdown": breakdown}


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


def parse_game_log(gl_df, playoff=False):
    """Parse a PlayerGameLog DataFrame into standardized game dicts (newest-first).
    Returns list of dicts with date, opp, home, b2b, playoff, wl, min, pts, 3pm,
    fg3a, reb, oreb, dreb, ast, stl, blk, tov, pm, fgm, fga, fg_pct, ftm, fta,
    ft_pct, fg3_pct, pf, usg.
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
            "date":   date_iso,
            "opp":    opp,
            "home":   is_home,
            "b2b":    False,
            "playoff": playoff,
            "wl":     str(g.get("WL", "")),
            "min":    parse_min(g.get("MIN", 0)),
            "pts":    safe_int(g.get("PTS")),
            "3pm":    safe_int(g.get("FG3M")),
            "fg3a":   safe_int(g.get("FG3A")),
            "reb":    safe_int(g.get("REB")),
            "oreb":   safe_int(g.get("OREB")),
            "dreb":   safe_int(g.get("DREB")),
            "ast":    safe_int(g.get("AST")),
            "stl":    safe_int(g.get("STL")),
            "blk":    safe_int(g.get("BLK")),
            "tov":    safe_int(g.get("TOV")),
            "pm":     safe_int(g.get("PLUS_MINUS")),
            "fgm":    safe_int(g.get("FGM")),
            "fga":    safe_int(g.get("FGA")),
            "fg_pct": safe_float(g.get("FG_PCT"), 3),
            "ftm":    safe_int(g.get("FTM")),
            "fta":    safe_int(g.get("FTA")),
            "ft_pct": safe_float(g.get("FT_PCT"), 3),
            "fg3_pct": safe_float(g.get("FG3_PCT"), 3),
            "pf":     safe_int(g.get("PF")),
            "usg":    0,
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
        {"key": "pts", "label": "PTS", "min_avg": 1.0},
        {"key": "reb", "label": "REB", "min_avg": 1.0},
        {"key": "ast", "label": "AST", "min_avg": 1.0},
        {"key": "3pm", "label": "3PM", "min_avg": 0.5},
        {"key": "stl", "label": "STL", "min_avg": 0.5},
        {"key": "blk", "label": "BLK", "min_avg": 0.5},
    ]

    slice_games = games[:window] if len(games) >= window else games
    n = len(slice_games)
    if n == 0:
        return []

    legs = []
    for sc in stat_configs:
        k = sc["key"]
        combo = sc.get("combo")
        if combo:
            vals = [sum((g.get(c, 0) or 0) for c in combo) for g in slice_games]
        else:
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
        heap = []   # min-heap of (empirical_rate, tiebreak, combo_dict)
        tiebreak = 0

        for combo_indices in itertools.combinations(range(len(all_legs)), leg_count):
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

            combo_dict = {
                "legs":              combo_legs,
                "empirical_rate":    round(empirical_rate, 3),
                "naive_rate":        round(naive_rate, 3),
                "correlation_ratio": round(corr_ratio, 3),
                "hits":              empirical_hits,
                "sample_size":       n_shared,
            }
            if len(heap) < top_n:
                heapq.heappush(heap, (empirical_rate, tiebreak, combo_dict))
            elif empirical_rate > heap[0][0]:
                heapq.heapreplace(heap, (empirical_rate, tiebreak, combo_dict))
            tiebreak += 1

        results[str(leg_count)] = [item[2] for item in sorted(heap, key=lambda x: -x[0])]

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
            sb = nba_call(lambda d=date_str: ScoreboardV2(game_date=d, timeout=20))
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
        resp_opp = nba_call(lambda: LeagueDashTeamStats(
            season=SEASON,
            per_mode_detailed="PerGame",
            measure_type_detailed_defense="Opponent",
            season_type_all_star=SEASON_TYPE,
            timeout=20,
        ))
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
        league_avg_tov    = safe_float(df_opp["OPP_TOV"].mean(), 1)
        league_avg_stl    = safe_float(df_opp["OPP_STL"].mean(), 1)
        league_avg_blk    = safe_float(df_opp["OPP_BLK"].mean(), 1)
        league_avg_ftm    = safe_float(df_opp["OPP_FTM"].mean(), 1)
        league_avg_fta    = safe_float(df_opp["OPP_FTA"].mean(), 1)
        league_avg_oreb   = safe_float(df_opp["OPP_OREB"].mean(), 1)
        league_avg_dreb   = safe_float(df_opp["OPP_DREB"].mean(), 1)
        league_avg_pf     = safe_float(df_opp["OPP_PF"].mean(), 1)
        league_avg_fgm    = safe_float(df_opp["OPP_FGM"].mean(), 1)
        league_avg_fga    = safe_float(df_opp["OPP_FGA"].mean(), 1)

        # Call 2 — Advanced measure: DEF_RATING + PACE
        time.sleep(0.4)
        resp_adv = nba_call(lambda: LeagueDashTeamStats(
            season=SEASON,
            per_mode_detailed="PerGame",
            measure_type_detailed_defense="Advanced",
            season_type_all_star=SEASON_TYPE,
            timeout=20,
        ))
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
            "tovForced":       safe_float(r.get("OPP_TOV")),
            "stlAllowed":      safe_float(r.get("OPP_STL")),
            "blkAllowed":      safe_float(r.get("OPP_BLK")),
            "ftmAllowed":      safe_float(r.get("OPP_FTM")),
            "ftaAllowed":      safe_float(r.get("OPP_FTA")),
            "fgmAllowed":      safe_float(r.get("OPP_FGM")),
            "fgaAllowed":      safe_float(r.get("OPP_FGA")),
            "orebAllowed":     safe_float(r.get("OPP_OREB")),
            "drebAllowed":     safe_float(r.get("OPP_DREB")),
            "pfCommitted":     safe_float(r.get("OPP_PF")),
            "defRating":       def_rating,
            "pace":            pace,
            "leagueAvgPts":    league_avg_pts,
            "leagueAvgReb":    league_avg_reb,
            "leagueAvgAst":    league_avg_ast,
            "leagueAvgThrees": league_avg_threes,
            "leagueAvgTov":    league_avg_tov,
            "leagueAvgStl":    league_avg_stl,
            "leagueAvgBlk":    league_avg_blk,
            "leagueAvgFtm":    league_avg_ftm,
            "leagueAvgFta":    league_avg_fta,
            "leagueAvgOreb":   league_avg_oreb,
            "leagueAvgDreb":   league_avg_dreb,
            "leagueAvgPf":     league_avg_pf,
            "leagueAvgFgm":    league_avg_fgm,
            "leagueAvgFga":    league_avg_fga,
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
        info_ep = nba_call(lambda: CommonPlayerInfo(player_id=player_id, timeout=20))
        info_df = info_ep.common_player_info.get_data_frame()
        if info_df.empty:
            return jsonify({"error": "Player info unavailable"}), 404
        meta = info_df.iloc[0]

        full_name = meta.get("DISPLAY_FIRST_LAST", player["full_name"])
        team_abbr = meta.get("TEAM_ABBREVIATION", "")
        position  = meta.get("POSITION", "")
        jersey    = str(meta.get("JERSEY", ""))
        time.sleep(0.6)

        # 3 · full-season game log (Regular Season, newest-first)
        gl_ep = nba_call(lambda: PlayerGameLog(
            player_id=player_id,
            season=SEASON,
            season_type_all_star=SEASON_TYPE,
            timeout=20,
        ))
        gl_df = gl_ep.player_game_log.get_data_frame()

        # 3b · playoff game log (silently skip if playoffs haven't started)
        try:
            time.sleep(0.3)
            gl_po_ep = nba_call(lambda: PlayerGameLog(
                player_id=player_id,
                season=SEASON,
                season_type_all_star="Playoffs",
                timeout=20,
            ))
            gl_po_df = gl_po_ep.player_game_log.get_data_frame()
        except Exception:
            gl_po_df = None

        # 4 · parse and merge — playoffs first (most recent), then regular season
        games_rs = parse_game_log(gl_df, playoff=False)
        games_po = parse_game_log(gl_po_df, playoff=True) if gl_po_df is not None and not gl_po_df.empty else []
        # Sort combined list newest-first by date
        games = sorted(games_po + games_rs, key=lambda g: g["date"], reverse=True)

        # 5 · season averages from full game log
        if not gl_df.empty:
            pts_avg = safe_float(gl_df["PTS"].mean())
            reb_avg = safe_float(gl_df["REB"].mean())
            ast_avg = safe_float(gl_df["AST"].mean())
            stl_avg = safe_float(gl_df["STL"].mean())
            blk_avg = safe_float(gl_df["BLK"].mean())
            season_avg = {
                "pts": pts_avg,
                "3pm": safe_float(gl_df["FG3M"].mean()),
                "reb": reb_avg,
                "ast": ast_avg,
                "stl": stl_avg,
                "blk": blk_avg,
                "min": safe_float(gl_df["MIN"].apply(parse_min).mean()),
                "tov": safe_float(gl_df["TOV"].mean()),
                # Tier 1: new basic stats
                "fgm": safe_float(gl_df["FGM"].mean()),
                "fga": safe_float(gl_df["FGA"].mean()),
                "fg_pct": safe_float(gl_df["FG_PCT"].mean(), 3),
                "ftm": safe_float(gl_df["FTM"].mean()),
                "fta": safe_float(gl_df["FTA"].mean()),
                "ft_pct": safe_float(gl_df["FT_PCT"].mean(), 3),
                "fg3_pct": safe_float(gl_df["FG3_PCT"].mean(), 3),
                "pf": safe_float(gl_df["PF"].mean()),
                # Tier 1: combo stats
                "pra": safe_float(pts_avg + reb_avg + ast_avg),
                "pr":  safe_float(pts_avg + reb_avg),
                "pa":  safe_float(pts_avg + ast_avg),
                "ra":  safe_float(reb_avg + ast_avg),
                "stocks": safe_float(stl_avg + blk_avg),
                # DD2/TD3 (count from game log)
                "dd2": 0,
                "td3": 0,
                # advanced fields filled below
                "usg": 0.0,
                "ts_pct": 0.0,
                "net_rating": 0.0,
                "efg_pct": 0.0,
                "ast_pct": 0.0,
                "ast_tov": 0.0,
                "ast_ratio": 0.0,
                "oreb_pct": 0.0,
                "dreb_pct": 0.0,
                "reb_pct": 0.0,
                "tov_pct": 0.0,
                "pie": 0.0,
                "pace_player": 0.0,
            }
            # Compute DD2/TD3 from game log
            dd2_count = 0
            td3_count = 0
            for _, g in gl_df.iterrows():
                cats = sum(1 for v in [safe_int(g.get("PTS")), safe_int(g.get("REB")),
                                       safe_int(g.get("AST")), safe_int(g.get("STL")),
                                       safe_int(g.get("BLK"))] if v >= 10)
                if cats >= 2:
                    dd2_count += 1
                if cats >= 3:
                    td3_count += 1
            season_avg["dd2"] = dd2_count
            season_avg["td3"] = td3_count
        else:
            season_avg = {k: 0 for k in [
                "pts","3pm","reb","ast","stl","blk","min","tov",
                "fgm","fga","fg_pct","ftm","fta","ft_pct","fg3_pct","pf",
                "pra","pr","pa","ra","stocks","dd2","td3",
                "usg","ts_pct","net_rating","efg_pct",
                "ast_pct","ast_tov","ast_ratio","oreb_pct","dreb_pct","reb_pct","tov_pct","pie","pace_player",
            ]}

        # 6 · advanced season stats (USG%, TS%, NET_RATING)
        try:
            time.sleep(0.6)
            adv_ep = nba_call(lambda: LeagueDashPlayerStats(
                season=SEASON,
                season_type_all_star=SEASON_TYPE,
                measure_type_detailed_defense="Advanced",
                per_mode_detailed="PerGame",
                timeout=20,
            ))
            adv_df = adv_ep.league_dash_player_stats.get_data_frame()
            adv_row = adv_df[adv_df["PLAYER_ID"] == int(player_id)]
            if not adv_row.empty:
                ar = adv_row.iloc[0]
                def pct100(val):
                    """Convert decimal (0.28) to percent (28.0)."""
                    v = float(val or 0)
                    return round(v * 100 if v < 1.0 else v, 1)
                # nba_api returns these as decimals (0.28), convert to percent
                season_avg["usg"]        = pct100(ar.get("USG_PCT"))
                season_avg["ts_pct"]     = pct100(ar.get("TS_PCT"))
                season_avg["net_rating"] = safe_float(ar.get("NET_RATING"))
                # Tier 2: additional advanced stats
                season_avg["efg_pct"]    = pct100(ar.get("EFG_PCT"))
                season_avg["ast_pct"]    = pct100(ar.get("AST_PCT"))
                season_avg["ast_tov"]    = safe_float(ar.get("AST_TOV"), 2)
                season_avg["ast_ratio"]  = safe_float(ar.get("AST_RATIO"))
                season_avg["oreb_pct"]   = pct100(ar.get("OREB_PCT"))
                season_avg["dreb_pct"]   = pct100(ar.get("DREB_PCT"))
                season_avg["reb_pct"]    = pct100(ar.get("REB_PCT"))
                season_avg["tov_pct"]    = pct100(ar.get("TOV_PCT"))
                season_avg["pie"]        = pct100(ar.get("PIE"))
                season_avg["pace_player"] = safe_float(ar.get("PACE"))
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
        df_base = nba_call(lambda: LeagueDashTeamStats(
            season=SEASON,
            per_mode_detailed="PerGame",
            measure_type_detailed_defense="Base",
            season_type_all_star=SEASON_TYPE,
            timeout=25,
        )).league_dash_team_stats.get_data_frame()

        time.sleep(0.4)
        df_opp = nba_call(lambda: LeagueDashTeamStats(
            season=SEASON,
            per_mode_detailed="PerGame",
            measure_type_detailed_defense="Opponent",
            season_type_all_star=SEASON_TYPE,
            timeout=25,
        )).league_dash_team_stats.get_data_frame()

        time.sleep(0.4)
        df_adv = nba_call(lambda: LeagueDashTeamStats(
            season=SEASON,
            per_mode_detailed="PerGame",
            measure_type_detailed_defense="Advanced",
            season_type_all_star=SEASON_TYPE,
            timeout=25,
        )).league_dash_team_stats.get_data_frame()

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


@app.route("/api/team-profiles")
def team_profiles_ep():
    """Return offensive/defensive profiles for all 30 teams. Cached 4 hours."""
    global _team_profiles_cache
    now = time.time()
    if _team_profiles_cache["data"] and now - _team_profiles_cache["ts"] < _TEAM_PROFILES_TTL:
        return jsonify(_team_profiles_cache["data"])
    try:
        teams_data = _fetch_all_team_data()
        _add_percentile_ranks(teams_data)
        profiles = [_build_team_profile(t) for t in teams_data]
        by_team  = {p["team"]: p for p in profiles}
        result   = {"profiles": profiles, "by_team": by_team, "season": SEASON}
        _team_profiles_cache["data"] = result
        _team_profiles_cache["ts"]   = now
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "detail": traceback.format_exc()}), 500


@app.route("/api/matchup-strength")
def matchup_strength_ep():
    """Return matchup score + profiles for a given attacker vs defender."""
    attacker = request.args.get("attacker", "").strip().upper()
    defender = request.args.get("defender", "").strip().upper()
    if not attacker or not defender:
        return jsonify({"error": "attacker and defender parameters required"}), 400

    global _team_profiles_cache
    now = time.time()
    if not (_team_profiles_cache["data"] and now - _team_profiles_cache["ts"] < _TEAM_PROFILES_TTL):
        try:
            teams_data = _fetch_all_team_data()
            _add_percentile_ranks(teams_data)
            profiles = [_build_team_profile(t) for t in teams_data]
            by_team  = {p["team"]: p for p in profiles}
            _team_profiles_cache["data"] = {"profiles": profiles, "by_team": by_team, "season": SEASON}
            _team_profiles_cache["ts"]   = now
        except Exception as e:
            return jsonify({"error": str(e), "detail": traceback.format_exc()}), 500

    by_team     = _team_profiles_cache["data"]["by_team"]
    att_profile = by_team.get(attacker)
    def_profile = by_team.get(defender)
    if not att_profile:
        return jsonify({"error": f"Team not found: {attacker}"}), 404
    if not def_profile:
        return jsonify({"error": f"Team not found: {defender}"}), 404

    return jsonify({
        "attacker":        attacker,
        "defender":        defender,
        "matchupScore":    _compute_matchup_score(att_profile, def_profile),
        "attackerProfile": att_profile,
        "defenderProfile": def_profile,
    })


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
        base_ep = nba_call(lambda: LeagueDashPlayerStats(
            season=SEASON,
            season_type_all_star=SEASON_TYPE,
            measure_type_detailed_defense="Base",
            per_mode_detailed="PerGame",
            timeout=25,
        ), retries=2, base_delay=1.0)
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

        # ── Step 2: Fetch game logs for each player (parallel across both teams) ──
        def fetch_one_player(row, team_abbr):
            """Fetch game log for a single player. Returns (name, games, meta_dict, warn_msg)."""
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
                gl_ep = nba_call(lambda p=pid: PlayerGameLog(
                    player_id=p,
                    season=SEASON,
                    season_type_all_star=SEASON_TYPE,
                    timeout=15,
                ), retries=2, base_delay=1.0)
                gl_df = gl_ep.player_game_log.get_data_frame()
                games = parse_game_log(gl_df)
                if len(games) >= 5:
                    return pname, games, {"name": pname, "team": team_abbr, "avg": p_avg, "gamesPlayed": len(games)}, None
                else:
                    return pname, None, None, f"{pname}: only {len(games)} games — skipped"
            except Exception as ex:
                return pname, None, None, f"Failed to fetch {pname}: {str(ex)}"

        def fetch_team_game_logs_parallel(players_df, team_abbr):
            player_games = {}
            player_meta  = []
            rows = list(players_df.iterrows())
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = {pool.submit(fetch_one_player, row, team_abbr): row for _, row in rows}
                for future in as_completed(futures):
                    pname, games, meta, warn = future.result()
                    if warn:
                        warnings.append(warn)
                    elif games is not None:
                        player_games[pname] = games
                        player_meta.append(meta)
            return player_games, player_meta

        with ThreadPoolExecutor(max_workers=2) as pool:
            home_future = pool.submit(fetch_team_game_logs_parallel, home_players_df, home_abbr)
            away_future = pool.submit(fetch_team_game_logs_parallel, away_players_df, away_abbr)
            home_games, home_meta = home_future.result()
            away_games, away_meta = away_future.result()

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


_schedule_cache = {"data": None, "ts": 0}
_SCHEDULE_TTL = 7200  # 2 hours


@app.route("/api/schedule")
def schedule():
    """Return NBA schedule grouped by date (or filtered by ?date=YYYY-MM-DD).
    Uses ScheduleLeagueV2 for the full season in one call; cached 2 hours.
    """
    import pandas as pd

    global _schedule_cache
    now = time.time()

    # Refresh cache if stale
    if _schedule_cache["data"] is None or now - _schedule_cache["ts"] > _SCHEDULE_TTL:
        try:
            from nba_api.stats.endpoints import ScheduleLeagueV2
            ep = nba_call(lambda: ScheduleLeagueV2(season=SEASON, timeout=30),
                          retries=2, base_delay=1.0)
            df = ep.get_data_frames()[0]
            df["parsedDate"] = pd.to_datetime(df["gameDate"]).dt.strftime("%Y-%m-%d")
            _schedule_cache["data"] = df
            _schedule_cache["ts"] = now
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    df = _schedule_cache["data"]

    # Optional date filter
    date_filter = request.args.get("date", "").strip()
    if date_filter:
        df = df[df["parsedDate"] == date_filter]

    games = []
    for _, row in df.iterrows():
        status = int(row.get("gameStatus", 1))
        home_score = safe_int(row.get("homeTeam_score")) if status >= 2 else None
        away_score = safe_int(row.get("awayTeam_score")) if status >= 2 else None
        # Parse tip-off time (ISO string like 1900-01-01T00:00:00Z → strip date part)
        raw_time = str(row.get("gameTimeEst", ""))
        tip_et = ""
        if "T" in raw_time:
            tip_et = raw_time.split("T")[1][:5]  # "HH:MM"

        games.append({
            "gameId":        str(row.get("gameId", "")),
            "date":          row["parsedDate"],
            "statusText":    str(row.get("gameStatusText", "")),
            "status":        status,          # 1=scheduled, 2=live, 3=final
            "tipEt":         tip_et,
            "arenaName":     str(row.get("arenaName", "")),
            "arenaCity":     str(row.get("arenaCity", "")),
            "arenaState":    str(row.get("arenaState", "")),
            "homeTricode":   str(row.get("homeTeam_teamTricode", "")),
            "homeName":      str(row.get("homeTeam_teamName", "")),
            "homeCity":      str(row.get("homeTeam_teamCity", "")),
            "homeWins":      safe_int(row.get("homeTeam_wins")),
            "homeLosses":    safe_int(row.get("homeTeam_losses")),
            "homeScore":     home_score,
            "awayTricode":   str(row.get("awayTeam_teamTricode", "")),
            "awayName":      str(row.get("awayTeam_teamName", "")),
            "awayCity":      str(row.get("awayTeam_teamCity", "")),
            "awayWins":      safe_int(row.get("awayTeam_wins")),
            "awayLosses":    safe_int(row.get("awayTeam_losses")),
            "awayScore":     away_score,
        })

    # Sort by date then tip time
    games.sort(key=lambda g: (g["date"], g["tipEt"]))

    if date_filter:
        return jsonify({"date": date_filter, "games": games, "count": len(games)})

    # Group by date
    by_date = {}
    for g in games:
        by_date.setdefault(g["date"], []).append(g)
    return jsonify({"season": SEASON, "dates": by_date, "totalGames": len(games)})


# ── INSIGHTS helpers ────────────────────────────────────────────────────────

def _load_game_logs():
    if "game_logs" not in _insights_cache:
        path = os.path.join(_TEAM_ANALYSIS_DIR, "ast_game_logs_2025_26.pkl")
        with open(path, "rb") as f:
            _insights_cache["game_logs"] = pickle.load(f)
    return _insights_cache["game_logs"]


def _load_synergy(kind):
    key = f"synergy_{kind}"
    if key not in _insights_cache:
        path = os.path.join(_TEAM_ANALYSIS_DIR, f"synergy_{kind}_2025-26.csv")
        _insights_cache[key] = pd.read_csv(path)
    return _insights_cache[key]


def _derive_matchup_cols(df):
    """Add OPP_ABB, IS_HOME, PLAYER_TEAM_ABB from MATCHUP string if missing."""
    if "OPP_ABB" not in df.columns:
        df = df.copy()
        m = df["MATCHUP"].astype(str)
        df["IS_HOME"] = m.str.contains(r"vs\.", na=False, regex=True)
        df["PLAYER_TEAM_ABB"] = m.str[:3]
        df["OPP_ABB"] = m.str.split().str[-1]
    return df


def _compute_defensive_clusters():
    if "def_clusters" not in _insights_cache:
        from sklearn.cluster import KMeans
        from sklearn.preprocessing import StandardScaler

        df = _load_synergy("defense")
        ppp = df.pivot_table(index="TEAM_ABBREVIATION", columns="PLAY_TYPE", values="PPP")
        ppp = ppp.fillna(ppp.mean())
        ranked = ppp.rank(axis=0)
        X = StandardScaler().fit_transform(ranked)
        km = KMeans(n_clusters=6, random_state=42, n_init=20).fit(X)

        adv = pd.read_csv(os.path.join(_TEAM_ANALYSIS_DIR, "team_advanced_2025-26.csv"))
        adv = adv.set_index("TEAM_ABBREVIATION")
        labels = pd.Series(km.labels_, index=ppp.index)
        cluster_def = labels.to_frame("cluster").join(adv[["DEF_RATING"]])
        means = cluster_def.groupby("cluster")["DEF_RATING"].mean().sort_values()
        rank_map = {c: i for i, c in enumerate(means.index)}
        tier_names = ["Elite-DEF", "Strong-DEF", "Avg-DEF", "Avg-DEF+", "Soft-DEF", "Porous-DEF"]
        name_map = {c: tier_names[rank_map[c]] for c in means.index}
        _insights_cache["def_clusters"] = labels.map(name_map).to_dict()
    return _insights_cache["def_clusters"]


def _safe_float(v):
    try:
        f = float(v)
        return None if (f != f) else f  # NaN check
    except Exception:
        return None


def _norm_name(name):
    """Strip accents for fuzzy player-name matching (Dončić → doncic)."""
    import unicodedata
    return unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower().strip()


def _match_player(gl, player_name):
    """Return mask matching player_name against PLAYER_NAME column, accent-insensitive."""
    target = _norm_name(player_name)
    return gl["PLAYER_NAME"].map(_norm_name) == target


def _find_player_name(player_ast_avg, player_name):
    """Find the exact pkl player name that matches player_name (accent-insensitive)."""
    target = _norm_name(player_name)
    return next((n for n in player_ast_avg if _norm_name(n) == target), None)


# ── INSIGHTS endpoints ───────────────────────────────────────────────────────

@app.route("/api/analysis/player-stats")
def analysis_player_stats():
    """Season aggregates for all players in pkl + reliability profiles."""
    try:
        gl = _derive_matchup_cols(_load_game_logs())
        agg = gl.groupby(["PLAYER_ID", "PLAYER_NAME"]).agg(
            GP=("AST", "count"),
            MIN=("MIN_float", "mean"),
            AST=("AST", "mean"),
            AST_std=("AST", "std"),
            TOV=("TOV", "mean"),
            FGM=("FGM", "mean"),
            FGA=("FGA", "mean"),
            TEAM=("PLAYER_TEAM_ABB", "first"),
        ).reset_index()
        agg["reliability"] = (agg["AST"] / (agg["AST_std"] + 0.01)).round(2)

        pot_path = os.path.join(_TEAM_ANALYSIS_DIR, "player_passing_stats_2025-26.csv")
        if os.path.exists(pot_path):
            pot = pd.read_csv(pot_path)[["PLAYER_ID", "POTENTIAL_AST", "PASSES_MADE", "AST_TO_PASS_PCT"]]
            agg = agg.merge(pot, on="PLAYER_ID", how="left")
        else:
            for c in ("POTENTIAL_AST", "PASSES_MADE", "AST_TO_PASS_PCT"):
                agg[c] = None

        agg = agg.sort_values("AST", ascending=False)

        rows = []
        for _, r in agg.iterrows():
            rows.append({
                "player_id": int(r["PLAYER_ID"]),
                "player": str(r["PLAYER_NAME"]),
                "team": str(r["TEAM"]),
                "gp": int(r["GP"]),
                "min": round(float(r["MIN"]), 1),
                "ast": round(float(r["AST"]), 1),
                "ast_std": round(float(r["AST_std"]), 2) if _safe_float(r["AST_std"]) is not None else 0.0,
                "tov": round(float(r["TOV"]), 1),
                "fgm": round(float(r["FGM"]), 1),
                "fga": round(float(r["FGA"]), 1),
                "reliability": round(float(r["reliability"]), 2),
                "potential_ast": round(_safe_float(r["POTENTIAL_AST"]), 1) if _safe_float(r["POTENTIAL_AST"]) is not None else None,
                "passes_made": round(_safe_float(r["PASSES_MADE"]), 1) if _safe_float(r["PASSES_MADE"]) is not None else None,
                "ast_to_pass_pct": round(_safe_float(r["AST_TO_PASS_PCT"]), 3) if _safe_float(r["AST_TO_PASS_PCT"]) is not None else None,
            })
        return jsonify({"players": rows})
    except Exception as e:
        return jsonify({"error": str(e), "detail": traceback.format_exc()}), 500


@app.route("/api/analysis/assist-matchup")
def analysis_assist_matchup():
    """Per-opponent breakdown for a player + cluster heatmap for top players."""
    player_name = request.args.get("player", "").strip()
    try:
        gl = _derive_matchup_cols(_load_game_logs()).copy()
        player_ast_avg = gl.groupby("PLAYER_NAME")["AST"].mean().to_dict()
        gl["AST_edge"] = gl["AST"] - gl["PLAYER_NAME"].map(player_ast_avg)
        clusters = _compute_defensive_clusters()
        gl["DEF_CLUSTER"] = gl["OPP_ABB"].map(clusters).fillna("Unknown")

        adv = pd.read_csv(os.path.join(_TEAM_ANALYSIS_DIR, "team_advanced_2025-26.csv"))
        def_rating_map = adv.set_index("TEAM_ABBREVIATION")["DEF_RATING"].to_dict()

        # Part A: per-opponent for selected player
        per_opp = []
        if player_name:
            pgl = gl[_match_player(gl, player_name)]
            if not pgl.empty:
                grp = pgl.groupby("OPP_ABB").agg(
                    games=("AST", "count"),
                    avg_ast=("AST", "mean"),
                    avg_edge=("AST_edge", "mean"),
                    std_ast=("AST", "std"),
                ).reset_index()
                grp = grp[grp["games"] >= 2].sort_values("avg_edge", ascending=False)
                for _, r in grp.iterrows():
                    opp = str(r["OPP_ABB"])
                    dr = _safe_float(def_rating_map.get(opp))
                    per_opp.append({
                        "opp": opp,
                        "games": int(r["games"]),
                        "avg_ast": round(float(r["avg_ast"]), 1),
                        "avg_edge": round(float(r["avg_edge"]), 2),
                        "std_ast": round(float(r["std_ast"]), 2) if _safe_float(r["std_ast"]) is not None else 0.0,
                        "def_cluster": clusters.get(opp, "Unknown"),
                        "def_rating": round(dr, 1) if dr is not None else None,
                    })

        # Part B: cluster heatmap for top 20 + selected player
        top20 = [n for n, _ in sorted(player_ast_avg.items(), key=lambda x: -x[1])[:20]]
        if player_name:
            match = _find_player_name(player_ast_avg, player_name)
            if match and match not in top20:
                top20 = [match] + top20

        hgl = gl[gl["PLAYER_NAME"].isin(top20)]
        pivot = hgl.groupby(["PLAYER_NAME", "DEF_CLUSTER"])["AST_edge"].mean().unstack(fill_value=0)

        def row_sort(name):
            if player_name and _norm_name(name) == _norm_name(player_name):
                return -9999
            return -player_ast_avg.get(name, 0)

        ordered = sorted(pivot.index, key=row_sort)
        pivot = pivot.loc[ordered]
        col_order = ["Elite-DEF", "Strong-DEF", "Avg-DEF", "Avg-DEF+", "Soft-DEF", "Porous-DEF"]
        cols = [c for c in col_order if c in pivot.columns] + [c for c in pivot.columns if c not in col_order]
        pivot = pivot[cols]

        return jsonify({
            "per_opponent": per_opp,
            "cluster_matrix": {
                "players": list(pivot.index),
                "clusters": list(pivot.columns),
                "matrix": [[round(float(v), 2) for v in row] for row in pivot.values],
            },
        })
    except Exception as e:
        return jsonify({"error": str(e), "detail": traceback.format_exc()}), 500


@app.route("/api/analysis/coef-matrix")
def analysis_coef_matrix():
    """Ridge coefficient matrix: (top players + selected) × play-type defense."""
    player_name = request.args.get("player", "").strip()
    try:
        from sklearn.linear_model import Ridge
        from sklearn.metrics import r2_score

        gl = _derive_matchup_cols(_load_game_logs()).copy()
        player_ast_avg = gl.groupby("PLAYER_NAME")["AST"].mean().to_dict()
        gl["AST_edge"] = gl["AST"] - gl["PLAYER_NAME"].map(player_ast_avg)

        df_def = _load_synergy("defense")
        def_ppp = df_def.pivot_table(index="TEAM_ABBREVIATION", columns="PLAY_TYPE", values="PPP")
        pt_cols = ["DEF_PPP_" + c for c in def_ppp.columns]
        def_ppp.columns = pt_cols
        gl = gl.join(def_ppp, on="OPP_ABB")

        top15 = [n for n, _ in sorted(player_ast_avg.items(), key=lambda x: -x[1])[:15]]
        if player_name:
            match = _find_player_name(player_ast_avg, player_name)
            if match and match not in top15:
                top15 = [match] + top15

        def row_sort(name):
            if player_name and _norm_name(name) == _norm_name(player_name):
                return -9999
            return -player_ast_avg.get(name, 0)

        top15 = sorted(top15, key=row_sort)
        coef_rows, r2_list = [], []

        for pname in top15:
            pgl = gl[gl["PLAYER_NAME"] == pname].dropna(subset=pt_cols)
            if len(pgl) < 10:
                coef_rows.append([0.0] * len(pt_cols))
                r2_list.append(0.0)
                continue
            X = pgl[pt_cols].values
            y = pgl["AST_edge"].values
            ridge = Ridge(alpha=1.0).fit(X, y)
            pred = ridge.predict(X)
            r2 = float(r2_score(y, pred))
            coef_rows.append([round(float(c), 3) for c in ridge.coef_])
            r2_list.append(round(r2, 3))

        return jsonify({
            "players": top15,
            "play_types": [c.replace("DEF_PPP_", "") for c in pt_cols],
            "matrix": coef_rows,
            "r2": r2_list,
        })
    except Exception as e:
        return jsonify({"error": str(e), "detail": traceback.format_exc()}), 500


@app.route("/api/analysis/synergy-ranks")
def analysis_synergy_ranks():
    """30 teams × 11 play types — offensive and defensive rank matrices."""
    try:
        df_off = _load_synergy("offense")
        df_def = _load_synergy("defense")

        off_ppp = df_off.pivot_table(index="TEAM_ABBREVIATION", columns="PLAY_TYPE", values="PPP")
        def_ppp = df_def.pivot_table(index="TEAM_ABBREVIATION", columns="PLAY_TYPE", values="PPP")
        play_types = sorted(set(off_ppp.columns) & set(def_ppp.columns))
        off_ppp = off_ppp[play_types]
        def_ppp = def_ppp[play_types]

        off_ranks = off_ppp.rank(axis=0, ascending=False).fillna(15).astype(int)
        def_ranks = def_ppp.rank(axis=0, ascending=True).fillna(15).astype(int)
        teams = sorted(off_ranks.index)
        off_ranks = off_ranks.loc[teams]
        def_ranks = def_ranks.loc[teams]

        return jsonify({
            "teams": teams,
            "play_types": list(play_types),
            "offense_ranks": off_ranks.values.tolist(),
            "defense_ranks": def_ranks.values.tolist(),
            "offense_ppp": [[round(float(v), 3) for v in row] for row in off_ppp.loc[teams].values],
            "defense_ppp": [[round(float(v), 3) for v in row] for row in def_ppp.loc[teams].values],
        })
    except Exception as e:
        return jsonify({"error": str(e), "detail": traceback.format_exc()}), 500


@app.route("/api/analysis/refresh", methods=["POST"])
def analysis_refresh_ep():
    """Trigger re-execution of all analysis notebooks in background."""
    if _refresh_status["status"] == "running":
        return jsonify({"status": "already_running"})

    def _run():
        _refresh_status["status"] = "running"
        _refresh_status["error"] = None
        for nb in [
            os.path.join(_TEAM_ANALYSIS_DIR, "assist_prediction_analysis.ipynb"),
            os.path.join(_TEAM_ANALYSIS_DIR, "assist_matchup_analysis.ipynb"),
            os.path.join(_TEAM_ANALYSIS_DIR, "preliminary_synergy_team_analysis.ipynb"),
        ]:
            r = subprocess.run(
                ["jupyter", "nbconvert", "--to", "notebook", "--execute",
                 "--inplace", "--ExecutePreprocessor.timeout=600", nb],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                _refresh_status.update({"status": "error", "error": r.stderr[-500:]})
                return
        _insights_cache.clear()
        _refresh_status.update({"status": "idle", "last": datetime.datetime.now().isoformat(), "error": None})

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/analysis/refresh-status")
def analysis_refresh_status_ep():
    return jsonify(_refresh_status)


if __name__ == "__main__":
    print("NBA Props Lab API  →  http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
