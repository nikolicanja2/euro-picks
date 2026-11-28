"""EURO PICKS SportAPI worker: EuroLeague only, low-request polling.

Stores complete per-player shooting splits and DNP rows.
The v3 marker triggers a one-time full-season boxscore/DNP backfill.
Finished games are then skipped only after v3 rows exist for that game.
"""
import logging
import re
import unicodedata
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from supabase import create_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
API_KEY = os.environ.get("SPORTAPI_KEY")
SB_URL = os.environ.get("SUPABASE_URL")
SB_SERVICE = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
if not all([API_KEY, SB_URL, SB_SERVICE]):
    raise SystemExit("Missing SPORTAPI_KEY, SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")

API = "https://sportapi.ai/api/basketball"
HEADERS = {"X-Api-Key": API_KEY}
SOURCE = "sportapi"
INGESTION_VERSION = "v3_shooting_dnp_backfill"
sb = create_client(SB_URL, SB_SERVICE)


def get(path, params=None, attempts=2):
    """Make a request, retrying only transient connection/5xx errors."""
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(API + path, headers=HEADERS, params=params, timeout=(10, 45))
            if response.status_code == 429:
                raise RuntimeError("SportAPI rate limit (429); stopping this run")
            if response.status_code >= 500 and attempt < attempts:
                logging.warning("HTTP %s on %s; retrying once", response.status_code, path)
                time.sleep(2)
                continue
            response.raise_for_status()
            payload = response.json()
            if payload.get("success") is False:
                raise RuntimeError(payload.get("error") or "SportAPI success=false")
            return payload
        except (requests.Timeout, requests.ConnectionError):
            if attempt == attempts:
                raise
            time.sleep(2)
    raise RuntimeError(f"Request failed: {path}")


def deduplicate(rows, conflict):
    columns = [part.strip() for part in conflict.split(",")]
    unique = {}
    for row in rows or []:
        key = tuple(row.get(col) for col in columns)
        if any(value is None for value in key):
            continue
        if key not in unique:
            unique[key] = dict(row)
        else:
            unique[key].update({k: v for k, v in row.items() if v is not None})
    if len(unique) < len(rows or []):
        logging.info("Merged %d duplicate rows for %s", len(rows) - len(unique), conflict)
    return list(unique.values())


def upsert(table, rows, conflict="id"):
    rows = deduplicate(rows, conflict)
    if rows:
        sb.table(table).upsert(rows, on_conflict=conflict).execute()
        logging.info("Upserted %d rows into %s", len(rows), table)


def is_euroleague(league):
    name = str((league or {}).get("name") or (league or {}).get("slug") or "").strip().lower()
    return name in {"euroleague", "euro league", "turkish airlines euroleague"} or name.startswith("euroleague ")


def already_has_current_boxscore(game_id):
    """Skip only after this version's expanded player rows were ingested."""
    result = (sb.table("player_boxscores").select("player_id")
              .eq("game_id", f"sportapi:game:{game_id}")
              .eq("ingestion_version", INGESTION_VERSION)
              .limit(1).execute())
    return bool(result.data)


def as_number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None



# --- SofaScore secondary enrichment (SportAPI remains canonical) ---
SOFA_API = "https://www.sofascore.com/api/v1"
SOFA_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.sofascore.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
}
SOFA_VERSION = "sofascore_v1"
SOFA_TZ = ZoneInfo("Europe/Belgrade")


def sofa_get(path, attempts=2):
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(SOFA_API + path, headers=SOFA_HEADERS, timeout=(10, 30))
            if response.status_code in (403, 429):
                raise RuntimeError(f"SofaScore HTTP {response.status_code} for {path}")
            if response.status_code >= 500 and attempt < attempts:
                time.sleep(attempt * 2)
                continue
            response.raise_for_status()
            return response.json()
        except (requests.Timeout, requests.ConnectionError):
            if attempt == attempts:
                raise
            time.sleep(attempt * 2)
    raise RuntimeError(f"SofaScore request failed: {path}")


def sofa_norm(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", value.lower())


def sofa_num(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(match.group()) if match else None


def sofa_stat(stats, *keys):
    for key in keys:
        if stats.get(key) is not None:
            return sofa_num(stats[key])
    return None


def sofa_line_row(game_id, player_id, team_id, line, dnp_reason=None):
    stats = line.get("statistics") or {}
    p2m, p2a = sofa_stat(stats, "twoPointsMade"), sofa_stat(stats, "twoPointsAttempted")
    p3m, p3a = sofa_stat(stats, "threePointsMade", "threePointersMade"), sofa_stat(stats, "threePointsAttempted", "threePointersAttempted")
    fgm = p2m + p3m if p2m is not None and p3m is not None else sofa_stat(stats, "fieldGoalsMade", "fgm")
    fga = p2a + p3a if p2a is not None and p3a is not None else sofa_stat(stats, "fieldGoalsAttempted", "fga")
    raw_minutes = stats.get("minutesPlayed") or stats.get("minutes")
    minutes = sofa_num(raw_minutes)
    if isinstance(raw_minutes, str) and ":" in raw_minutes:
        try:
            mm, ss = raw_minutes.split(":", 1)
            minutes = int(mm) + int(ss) / 60
        except ValueError:
            pass
    player = line.get("player") or {}
    return {
        "game_id": game_id, "player_id": player_id, "team_id": team_id,
        "minutes": minutes,
        "pts": sofa_stat(stats, "points", "pts"),
        "reb": sofa_stat(stats, "rebounds", "totalRebounds", "reb"),
        "ast": sofa_stat(stats, "assists", "ast"),
        "threes_made": p3m, "stl": sofa_stat(stats, "steals", "stl"),
        "blk": sofa_stat(stats, "blocks", "blk"), "tov": sofa_stat(stats, "turnovers", "tov"),
        "pir": sofa_stat(stats, "efficiency", "PIR", "pir"),
        "fgm": fgm, "fga": fga, "tpm": p3m, "tpa": p3a,
        "ftm": sofa_stat(stats, "freeThrowsMade", "ftm"),
        "fta": sofa_stat(stats, "freeThrowsAttempted", "fta"),
        "oreb": sofa_stat(stats, "offensiveRebounds", "oreb"),
        "dreb": sofa_stat(stats, "defensiveRebounds", "dreb"),
        "pf": sofa_stat(stats, "personalFouls", "fouls", "pf"),
        "plus_minus": sofa_stat(stats, "plusMinus", "plus_minus"),
        "starter": not bool(line.get("substitute", False)),
        "position": player.get("position") or line.get("position"),
        "jersey": line.get("shirtNumber") or line.get("jerseyNumber"),
        "dnp_reason": dnp_reason, "ingestion_version": SOFA_VERSION, "source": "sofascore",
    }


def run_sofascore_enrichment():
    """Best-effort SofaScore enrichment; never blocks successful SportAPI ingestion."""
    today = datetime.now(SOFA_TZ).date()
    dates = [(today + timedelta(days=i)).isoformat() for i in range(-7, 3)]
    start = datetime.combine(today - timedelta(days=8), datetime.min.time(), SOFA_TZ).astimezone(timezone.utc)
    end = datetime.combine(today + timedelta(days=3), datetime.max.time(), SOFA_TZ).astimezone(timezone.utc)

    games = (sb.table("games").select("id,start_time,home_team_id,away_team_id")
             .gte("start_time", start.isoformat()).lte("start_time", end.isoformat()).limit(1000).execute().data or [])
    team_ids = list({tid for game in games for tid in (game.get("home_team_id"), game.get("away_team_id")) if tid})
    teams = sb.table("teams").select("id,name").in_("id", team_ids).limit(1000).execute().data or [] if team_ids else []
    team_names = {t["id"]: sofa_norm(t.get("name")) for t in teams}
    game_map = {}
    for game in games:
        home, away = team_names.get(game.get("home_team_id")), team_names.get(game.get("away_team_id"))
        if not home or not away or not game.get("start_time"):
            continue
        try:
            dt = datetime.fromisoformat(str(game["start_time"]).replace("Z", "+00:00"))
            local_day = dt.astimezone(SOFA_TZ).date().isoformat()
        except (TypeError, ValueError):
            continue
        game_map[(local_day, home, away)] = game
        game_map[(local_day, away, home)] = game

    player_map = {}
    offset = 0
    while True:
        page = sb.table("players").select("id,full_name").range(offset, offset + 999).execute().data or []
        for player in page:
            key = sofa_norm(player.get("full_name"))
            if key:
                player_map.setdefault(key, player["id"])
        if len(page) < 1000:
            break
        offset += 1000

    events = {}
    for day in dates:
        payload = sofa_get(f"/sport/basketball/scheduled-events/{day}")
        for event in payload.get("events", []) or []:
            tournament = event.get("tournament") or {}
            unique = tournament.get("uniqueTournament") or {}
            labels = [sofa_norm(tournament.get("name")), sofa_norm(unique.get("name")),
                      sofa_norm(tournament.get("slug")), sofa_norm(unique.get("slug"))]
            if event.get("id") and any("euroleague" in label for label in labels):
                events[str(event["id"])] = event

    rows, matched, unmatched = [], 0, 0
    for event_id, event in events.items():
        status = (event.get("status") or {}).get("type", "")
        if status not in ("finished", "after_overtime", "ended"):
            continue
        timestamp = event.get("startTimestamp")
        home = sofa_norm((event.get("homeTeam") or {}).get("name"))
        away = sofa_norm((event.get("awayTeam") or {}).get("name"))
        if not timestamp or not home or not away:
            continue
        start_dt = datetime.fromtimestamp(int(timestamp), timezone.utc)
        day = start_dt.astimezone(SOFA_TZ).date().isoformat()
        game = game_map.get((day, home, away)) or game_map.get((day, away, home))
        if not game:
            unmatched += 1
            logging.info("SofaScore event %s not matched to SportAPI game; skipped", event_id)
            continue
        try:
            payload = sofa_get(f"/event/{event_id}/lineups")
        except Exception as exc:
            logging.warning("SofaScore lineups unavailable for event %s: %s", event_id, exc)
            continue
        sides = payload.get("event") or payload
        for side, team_field in (("home", "home_team_id"), ("away", "away_team_id")):
            side_data = sides.get(side) or {}
            team_id = game.get(team_field)
            for line in side_data.get("players", []) or []:
                player = line.get("player") or {}
                player_id = player_map.get(sofa_norm(player.get("name")))
                if player_id:
                    rows.append(sofa_line_row(game["id"], player_id, team_id, line))
            # Only explicit missingPlayers entries count as DNP; omitted players do not.
            for missing in side_data.get("missingPlayers", []) or []:
                player = missing.get("player") or missing
                player_id = player_map.get(sofa_norm(player.get("name")))
                if player_id:
                    reason = missing.get("reason") or missing.get("type") or "SofaScore missing player"
                    rows.append(sofa_line_row(game["id"], player_id, team_id,
                                              {"player": player, "statistics": {}}, str(reason)))
        matched += 1
        time.sleep(0.4)

    unique = {(r["game_id"], r["player_id"], r["source"]): r for r in rows}
    if unique:
        sb.table("player_boxscores").upsert(list(unique.values()), on_conflict="game_id,player_id,source").execute()
    logging.info("SofaScore enrichment: %d rows, %d matched games, %d unmatched events", len(unique), matched, unmatched)


def main():
    # GitHub Actions cron is UTC and does not support Europe/Belgrade DST.
    # The workflow schedules 22:00 and 23:00 UTC; only the run that lands
    # at 00:xx in Belgrade is allowed to perform the refresh. Manual runs pass.
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    local_now = datetime.now(ZoneInfo("Europe/Belgrade"))
    if event_name == "schedule" and local_now.hour != 0:
        logging.info(
            "Skipping duplicate DST schedule run; Belgrade local time is %s",
            local_now.isoformat(),
        )
        return

    leagues = get("/leagues").get("data", []) or []
    euro_leagues = [league for league in leagues if is_euroleague(league)]
    euro_ids = {str(league.get("id")) for league in euro_leagues if league.get("id") is not None}
    if not euro_ids:
        logging.warning("SportAPI did not return a recognisable EuroLeague competition; no games ingested")
        logging.info("Available competition names: %s", [x.get("name") for x in leagues])
        return
    logging.info("EuroLeague competitions selected: %s", [x.get("name") for x in euro_leagues])

    today = local_now.date()
    # Scheduled games use a short rolling window. Finished games are read for
    # the whole current season so the v3 marker can repair historical gaps once.
    wanted_dates = {
        (today + timedelta(days=offset)).isoformat()
        for offset in range(-7, 3)
    }
    games_by_id = {}

    # Use the league's own schedule/results endpoint instead of the global
    # date endpoint, which returned an empty data array for every requested day.
    # The league endpoint supports separate finished and scheduled lists.
    for league in euro_leagues:
        slug = league.get("slug") or "euroleague"
        season = league.get("current_season")
        for status in ("finished", "scheduled"):
            params = {"status": status, "limit": 200}
            if season:
                params["season"] = str(season)
            payload = get(f"/leagues/{slug}/games", params)
            data = payload.get("data", []) or []
            logging.info("EuroLeague %s endpoint (%s): API returned %d games", status, season, len(data))
            for game in data:
                if game.get("id") is None:
                    continue
                start_value = game.get("start") or game.get("date")
                if start_value:
                    try:
                        start_dt = datetime.fromisoformat(str(start_value).replace("Z", "+00:00"))
                        if start_dt.tzinfo is None:
                            start_dt = start_dt.replace(tzinfo=ZoneInfo("UTC"))
                        local_day = start_dt.astimezone(ZoneInfo("Europe/Belgrade")).date().isoformat()
                        if status == "scheduled" and local_day not in wanted_dates:
                            continue
                    except (TypeError, ValueError):
                        logging.warning("Could not parse game date %r for game %s", start_value, game.get("id"))
                        continue
                games_by_id[str(game["id"])] = game

    season_by_league = {str(x.get("id")): x.get("current_season") for x in euro_leagues}
    competition_rows, season_rows, team_rows, game_rows = [], [], [], []
    for league in euro_leagues:
        lid = str(league["id"])
        cid = f"sportapi:competition:{lid}"
        competition_rows.append({
            "id": cid, "name": league.get("name") or league.get("slug"),
            "country": league.get("country"), "gender": league.get("gender"),
            "level": league.get("type"), "source": SOURCE,
            "source_competition_id": lid, "active": True,
        })
        season = league.get("current_season")
        if season:
            season_rows.append({
                "id": f"sportapi:season:{lid}:{season}", "competition_id": cid,
                "season_name": str(season), "source": SOURCE,
            })

    finished = []
    for game in games_by_id.values():
        league = game.get("league") or {}
        lid = str(league.get("id"))
        season = game.get("season") or season_by_league.get(lid)
        season_id = f"sportapi:season:{lid}:{season}" if season else None
        home, away = game.get("home") or {}, game.get("away") or {}
        for team in (home, away):
            if team.get("id") is not None:
                team_rows.append({
                    "id": f"sportapi:team:{team['id']}",
                    "name": team.get("name") or str(team["id"]),
                    "country": None, "source": SOURCE, "source_team_id": str(team["id"]),
                })
        game_rows.append({
            "id": f"sportapi:game:{game['id']}", "competition_season_id": season_id,
            "start_time": game.get("start"),
            "home_team_id": f"sportapi:team:{home['id']}" if home.get("id") is not None else None,
            "away_team_id": f"sportapi:team:{away['id']}" if away.get("id") is not None else None,
            "home_score": home.get("score"), "away_score": away.get("score"),
            "status": game.get("status"), "source": SOURCE,
        })
        if str(game.get("status", "")).upper() in ("FT", "AOT") and game.get("has_boxscore"):
            finished.append(game)

    upsert("competitions", competition_rows)
    upsert("competition_seasons", season_rows)
    upsert("teams", team_rows)
    upsert("games", game_rows)
    logging.info("EuroLeague: %d games found across four dates; %d finished games advertise box scores", len(game_rows), len(finished))

    player_rows, box_rows = [], []
    fetched_games = skipped_games = dnp_count = 0
    for game in finished:
        if already_has_current_boxscore(game["id"]):
            skipped_games += 1
            continue

        payload = get(f"/games/{game['id']}/players")
        sides = payload.get("data") or {}
        game_rows_for_box = []
        game_dnp = 0

        for side in ("home", "away"):
            team_id = (game.get(side) or {}).get("id")
            for line in sides.get(side, []) or []:
                player = line.get("player") or {}
                pid = player.get("id")
                if pid is None:
                    logging.warning("Skipping player row without player ID in game %s", game["id"])
                    continue

                player_key = f"sportapi:player:{pid}"
                player_rows.append({
                    "id": player_key,
                    "full_name": player.get("name") or player.get("slug") or str(pid),
                    "source": SOURCE, "source_player_id": str(pid),
                })

                dnp_reason = line.get("dnp")
                is_dnp = bool(dnp_reason)
                if is_dnp:
                    game_dnp += 1

                game_rows_for_box.append({
                    "game_id": f"sportapi:game:{game['id']}",
                    "player_id": player_key,
                    "team_id": f"sportapi:team:{team_id}" if team_id is not None else None,
                    "minutes": as_number(line.get("minutes")),
                    "pts": as_number(line.get("pts")),
                    "reb": as_number(line.get("reb")),
                    "ast": as_number(line.get("ast")),
                    "threes_made": as_number(line.get("tpm")),
                    "stl": as_number(line.get("stl")),
                    "blk": as_number(line.get("blk")),
                    "tov": as_number(line.get("tov")),
                    "pir": None,
                    "fgm": as_number(line.get("fgm")),
                    "fga": as_number(line.get("fga")),
                    "tpm": as_number(line.get("tpm")),
                    "tpa": as_number(line.get("tpa")),
                    "ftm": as_number(line.get("ftm")),
                    "fta": as_number(line.get("fta")),
                    "oreb": as_number(line.get("oreb")),
                    "dreb": as_number(line.get("dreb")),
                    "pf": as_number(line.get("pf")),
                    "plus_minus": as_number(line.get("plus_minus")),
                    "starter": line.get("starter"),
                    "position": player.get("position"),
                    "jersey": player.get("jersey"),
                    "dnp_reason": str(dnp_reason) if is_dnp else None,
                    "ingestion_version": INGESTION_VERSION,
                    "source": SOURCE,
                })

        if game_rows_for_box:
            box_rows.extend(game_rows_for_box)
            fetched_games += 1
            dnp_count += game_dnp
            logging.info("Game %s: %d player rows, %d DNP", game["id"], len(game_rows_for_box), game_dnp)
        else:
            logging.warning("No player rows returned for game %s; it may be retried tomorrow", game["id"])

    upsert("players", player_rows)
    upsert("player_boxscores", box_rows, "game_id,player_id,source")
    logging.info("Fetched expanded boxscores for %d games; skipped %d already %s games", fetched_games, skipped_games, INGESTION_VERSION)
    logging.info("Upserted %d player rows; DNP rows: %d", len(deduplicate(player_rows, "id")), dnp_count)

    try:
        run_sofascore_enrichment()
    except Exception as exc:
        # SofaScore is secondary: a block or schema/API change must not undo SportAPI data.
        logging.exception("SofaScore enrichment failed; SportAPI refresh remains successful: %s", exc)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logging.exception("Ingestion failed: %s", exc)
        sys.exit(1)
