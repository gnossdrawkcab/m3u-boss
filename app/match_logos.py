"""Match logos for M3U Boss.

Turns sports matchup programme titles (e.g. "2026 NBA Finals : New York Knicks
at San Antonio Spurs ᴸᶦᵛᵉ") into a single combined "Team A vs Team B" badge
image, then exposes the filename so the EPG export can drop an <icon> on the
<programme>. TiviMate (and other guides that render programme art) then show the
matchup logo in the guide.

Design goals:
  * NEVER break the export — every public entry point is wrapped so any failure
    (network, parse, Pillow) degrades to "no logo", not an exception.
  * Cache everything. Team badges + composites are cached on disk + in SQLite,
    so steady-state runs do ~zero network calls. Negative lookups are cached too
    (re-tried weekly) so unknown teams don't get hammered every export.
  * "Big leagues first, graceful skip elsewhere" — we only attempt a matchup when
    the title carries a recognised league hint (NFL/NBA/MLB/NHL/WNBA/MLS/World
    Cup/UFL/WHL…), and we only emit a logo when BOTH teams resolve to a real
    badge in the right sport. Anything ambiguous just gets no logo.

Logo source: TheSportsDB (free key "3" by default; override via the
`thesportsdb_key` setting). Badges cover every league in the user's guide,
including national teams and minor/junior leagues.
"""
from __future__ import annotations

import hashlib
import io
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
LOGO_DIR = BASE_DIR / "data" / "match_logos"      # composites, served at /logos
SRC_DIR = LOGO_DIR / "_src"                        # cached raw team badges
CACHE_DB = LOGO_DIR / "cache.db"
NEG_TTL = 7 * 24 * 3600                            # re-try unknown teams weekly
POS_TTL = 180 * 24 * 3600                           # re-verify resolved badges ~6-monthly

_HTTP_TIMEOUT = 12
_BADGE_H = 200                                      # composite badge height (px)

# Per-export-run budget so a cold cache can't stall the export forever. Cached
# lookups are free; only never-seen teams/images count against these.
_MAX_LIVE_LOOKUPS = 250
_MAX_LIVE_IMAGES = 250
_THROTTLE = 0.12                                    # seconds between live API hits


# ── League detection ─────────────────────────────────────────────────────────
# Ordered: first hint whose pattern matches the title wins. Maps to a
# TheSportsDB strSport so same-named teams across sports don't collide.
_LEAGUE_HINTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bWNBA\b", re.I), "Basketball"),
    (re.compile(r"\bNBA\b", re.I), "Basketball"),
    (re.compile(r"\bNCAA\s*(Men'?s|Women'?s)?\s*Basketball\b|\bCollege Basketball\b", re.I), "Basketball"),
    (re.compile(r"\bEuroLeague\b", re.I), "Basketball"),
    (re.compile(r"\bNFL\b|\bNational Football League\b", re.I), "American Football"),
    (re.compile(r"\bUnited Football League\b|\bUFL\b|\bXFL\b|\bCFL\b", re.I), "American Football"),
    (re.compile(r"\bNCAA\s*Football\b|\bCollege Football\b|\bCFB\b", re.I), "American Football"),
    (re.compile(r"\bMLB\b|\bMajor League Baseball\b|\bBaseball\b", re.I), "Baseball"),
    (re.compile(r"\bNHL\b|\bWHL\b|\bAHL\b|\bOHL\b|\bKHL\b|\bHockey\b", re.I), "Ice Hockey"),
    (re.compile(r"\bMLS\b|\bPremier League\b|\bEPL\b|\bLa Liga\b|\bSerie A\b|\bBundesliga\b"
                r"|\bLigue 1\b|\bChampions League\b|\bUEFA\b|\bFIFA\b"
                r"|\bSoccer\b|\bF[úu]tbol\b|\bCopa\b|\bEuro 20\d\d\b", re.I), "Soccer"),
]

# Out-of-scope sports: these often use "A v B" too and would mis-resolve against
# soccer/other badges (e.g. cricket "England v South Africa" → soccer national
# teams). "World Cup" alone is ambiguous (Cricket/Rugby/FIFA), so it was dropped
# from the soccer hint above — only "FIFA" routes to soccer now. Anything here
# returns no logo (graceful skip), even if another hint would have fired.
_EXCLUDE_RE = re.compile(
    r"\b(cricket|I\.?C\.?C\.?|test match|ODI|T20|the hundred|rugby|league cup\b"
    r"|netball|GAA|hurling|tennis|ATP|WTA|golf|PGA|LPGA|NASCAR|IndyCar|F1|Formula"
    r"|UFC|MMA|boxing|darts|snooker|cycling|Tour de)\b", re.I)

# Separators between the two teams. No \b wrappers — the surrounding \s+ in
# _MATCH_RE already forces these to be standalone, whitespace-delimited tokens
# (so "at" can't match inside "Seattle"), and \b after an optional "." breaks on
# "vs." followed by a space. Order matters: longer alternatives first.
_SEP_RE = r"(?:versus|vs\.?|v\.?|@|at)"

# Strip trailing decorations so the home-team capture isn't polluted.
_TRAIL_PATTERNS = [
    re.compile(r"\s*[ᴸᶦᵛᵉ]+\s*$"),                       # superscript LIVE
    re.compile(r"\s*\bLIVE\b\s*$", re.I),
    re.compile(r"\s*-\s*#\d+\s*$"),                      # " - #696"
    re.compile(r"\s*\(\s*\d{4}-\d{2}-\d{2}[^)]*\)\s*$"), # " (2026-06-28 19:00:00)"
    re.compile(r"\s*\([^)]*\bFeed\b[^)]*\)\s*$", re.I),  # " ( TSN5 ... Feed )"
    re.compile(r"\s*@\s*\d{1,2}:\d{2}\s*(?:[ap]\.?m\.?)?\s*$", re.I),  # " @ 9:00 pm"
    # " at 06:40PM EDT on Jun 12" / " HOME 12 Jun 06:40 PM ET" (BossSports event titles)
    re.compile(r"\s+at \d{1,2}:\d{2}\s*[ap]\.?m\.?\b.*$", re.I),
    re.compile(r"\s+\b(HOME|AWAY|NATIONAL)\b\s+\d.*$"),
    # " @ Jun 10 08:30 PM ET" / " @ 10 Jun 08:30 PM" — a date(+time) after "@",
    # NOT a team (NBA/event channel names). Month-anchored so it never eats a
    # legit "Team @ Team" separator (a team name has no month token).
    re.compile(r"\s*@\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d.*$", re.I),
    re.compile(r"\s*@\s*\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?.*$", re.I),
    re.compile(r"\s*\([^)]*\)\s*$"),                     # any trailing (...) parenthetical
]

# Matchup at the END of the (cleaned) string. Team A must start at string start
# or right after a prefix delimiter (':' or '|'), which strips league prefixes
# like "2026 NBA Finals :" / "Group D:" / "US (Peacock 100) |" / "Home Feed:".
_MATCH_RE = re.compile(
    r"(?:^|[:|]\s*)"
    r"(?P<away>[A-Za-z0-9][^:|]*?)\s+"
    rf"(?P<sep>{_SEP_RE})\s+"
    r"(?P<home>[A-Za-z0-9][^:|]*?)\s*$",
    re.I,
)

# Sport-scoped team maps: nickname / abbreviation (normalised) -> full team name.
# Feeds use bare nicknames ("Tigers at Yankees") or abbrevs ("HOU at DET");
# searching those directly mis-resolves (e.g. "Tigers" -> a German basketball
# club, "Twins" -> a Dutch baseball club), so we expand to the full name FIRST,
# then search. Scoped per sport so shared nicknames (Cardinals, Giants, Rangers,
# Kings, Panthers, Jets) resolve to the right team for the title's league. Full
# "City Nickname" inputs aren't in these maps — they resolve fine by direct
# search. Keys are _norm()'d (lowercase, spaces).
_MLB = {
    "orioles":"Baltimore Orioles","red sox":"Boston Red Sox","yankees":"New York Yankees",
    "rays":"Tampa Bay Rays","blue jays":"Toronto Blue Jays","white sox":"Chicago White Sox",
    "guardians":"Cleveland Guardians","tigers":"Detroit Tigers","royals":"Kansas City Royals",
    "twins":"Minnesota Twins","astros":"Houston Astros","angels":"Los Angeles Angels",
    "athletics":"Athletics","mariners":"Seattle Mariners","rangers":"Texas Rangers",
    "braves":"Atlanta Braves","marlins":"Miami Marlins","mets":"New York Mets",
    "phillies":"Philadelphia Phillies","nationals":"Washington Nationals","cubs":"Chicago Cubs",
    "reds":"Cincinnati Reds","brewers":"Milwaukee Brewers","pirates":"Pittsburgh Pirates",
    "cardinals":"St. Louis Cardinals","diamondbacks":"Arizona Diamondbacks","dbacks":"Arizona Diamondbacks",
    "rockies":"Colorado Rockies","dodgers":"Los Angeles Dodgers","padres":"San Diego Padres",
    "giants":"San Francisco Giants",
    "bal":"Baltimore Orioles","bos":"Boston Red Sox","nyy":"New York Yankees","tb":"Tampa Bay Rays",
    "tbr":"Tampa Bay Rays","tor":"Toronto Blue Jays","cws":"Chicago White Sox","chw":"Chicago White Sox",
    "cle":"Cleveland Guardians","det":"Detroit Tigers","kc":"Kansas City Royals","kcr":"Kansas City Royals",
    "min":"Minnesota Twins","hou":"Houston Astros","laa":"Los Angeles Angels","ath":"Athletics",
    "oak":"Athletics","sea":"Seattle Mariners","tex":"Texas Rangers","atl":"Atlanta Braves",
    "mia":"Miami Marlins","nym":"New York Mets","phi":"Philadelphia Phillies","wsh":"Washington Nationals",
    "was":"Washington Nationals","chc":"Chicago Cubs","cin":"Cincinnati Reds","mil":"Milwaukee Brewers",
    "pit":"Pittsburgh Pirates","stl":"St. Louis Cardinals","ari":"Arizona Diamondbacks",
    "col":"Colorado Rockies","lad":"Los Angeles Dodgers","sd":"San Diego Padres","sdp":"San Diego Padres",
    "sf":"San Francisco Giants","sfg":"San Francisco Giants",
}
_NFL = {
    "cardinals":"Arizona Cardinals","falcons":"Atlanta Falcons","ravens":"Baltimore Ravens",
    "bills":"Buffalo Bills","panthers":"Carolina Panthers","bears":"Chicago Bears",
    "bengals":"Cincinnati Bengals","browns":"Cleveland Browns","cowboys":"Dallas Cowboys",
    "broncos":"Denver Broncos","lions":"Detroit Lions","packers":"Green Bay Packers",
    "texans":"Houston Texans","colts":"Indianapolis Colts","jaguars":"Jacksonville Jaguars",
    "jags":"Jacksonville Jaguars","chiefs":"Kansas City Chiefs","raiders":"Las Vegas Raiders",
    "chargers":"Los Angeles Chargers","rams":"Los Angeles Rams","dolphins":"Miami Dolphins",
    "vikings":"Minnesota Vikings","patriots":"New England Patriots","saints":"New Orleans Saints",
    "giants":"New York Giants","jets":"New York Jets","eagles":"Philadelphia Eagles",
    "steelers":"Pittsburgh Steelers","49ers":"San Francisco 49ers","niners":"San Francisco 49ers",
    "seahawks":"Seattle Seahawks","buccaneers":"Tampa Bay Buccaneers","bucs":"Tampa Bay Buccaneers",
    "titans":"Tennessee Titans","commanders":"Washington Commanders",
}
_NBA = {
    "hawks":"Atlanta Hawks","celtics":"Boston Celtics","nets":"Brooklyn Nets",
    "hornets":"Charlotte Hornets","bulls":"Chicago Bulls","cavaliers":"Cleveland Cavaliers",
    "cavs":"Cleveland Cavaliers","mavericks":"Dallas Mavericks","mavs":"Dallas Mavericks",
    "nuggets":"Denver Nuggets","pistons":"Detroit Pistons","warriors":"Golden State Warriors",
    "rockets":"Houston Rockets","pacers":"Indiana Pacers","clippers":"Los Angeles Clippers",
    "lakers":"Los Angeles Lakers","grizzlies":"Memphis Grizzlies","heat":"Miami Heat",
    "bucks":"Milwaukee Bucks","timberwolves":"Minnesota Timberwolves","wolves":"Minnesota Timberwolves",
    "pelicans":"New Orleans Pelicans","knicks":"New York Knicks","thunder":"Oklahoma City Thunder",
    "magic":"Orlando Magic","76ers":"Philadelphia 76ers","sixers":"Philadelphia 76ers",
    "suns":"Phoenix Suns","trail blazers":"Portland Trail Blazers","blazers":"Portland Trail Blazers",
    "kings":"Sacramento Kings","spurs":"San Antonio Spurs","raptors":"Toronto Raptors",
    "jazz":"Utah Jazz","wizards":"Washington Wizards",
}
_NHL = {
    "ducks":"Anaheim Ducks","bruins":"Boston Bruins","sabres":"Buffalo Sabres",
    "flames":"Calgary Flames","hurricanes":"Carolina Hurricanes","canes":"Carolina Hurricanes",
    "blackhawks":"Chicago Blackhawks","avalanche":"Colorado Avalanche","avs":"Colorado Avalanche",
    "blue jackets":"Columbus Blue Jackets","stars":"Dallas Stars","red wings":"Detroit Red Wings",
    "oilers":"Edmonton Oilers","panthers":"Florida Panthers","kings":"Los Angeles Kings",
    "wild":"Minnesota Wild","canadiens":"Montreal Canadiens","habs":"Montreal Canadiens",
    "predators":"Nashville Predators","preds":"Nashville Predators","devils":"New Jersey Devils",
    "islanders":"New York Islanders","rangers":"New York Rangers","senators":"Ottawa Senators",
    "sens":"Ottawa Senators","flyers":"Philadelphia Flyers","penguins":"Pittsburgh Penguins",
    "pens":"Pittsburgh Penguins","sharks":"San Jose Sharks","kraken":"Seattle Kraken",
    "blues":"St. Louis Blues","lightning":"Tampa Bay Lightning","bolts":"Tampa Bay Lightning",
    "maple leafs":"Toronto Maple Leafs","leafs":"Toronto Maple Leafs","mammoth":"Utah Mammoth",
    "canucks":"Vancouver Canucks","golden knights":"Vegas Golden Knights","capitals":"Washington Capitals",
    "caps":"Washington Capitals","jets":"Winnipeg Jets",
}
_TEAMS: dict[str, dict[str, str]] = {
    "Baseball": _MLB, "American Football": _NFL, "Basketball": _NBA, "Ice Hockey": _NHL,
}


# ── tiny SQLite cache ────────────────────────────────────────────────────────
def _conn() -> sqlite3.Connection:
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(CACHE_DB), timeout=15)
    con.execute("PRAGMA busy_timeout=15000")
    con.execute("CREATE TABLE IF NOT EXISTS badges("
                "q TEXT, sport TEXT, url TEXT, ok INT, ts INT, PRIMARY KEY(q,sport))")
    con.execute("CREATE TABLE IF NOT EXISTS composites("
                "key TEXT PRIMARY KEY, filename TEXT, ts INT)")
    return con


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


# ── matchup parsing ──────────────────────────────────────────────────────────
def league_sport(title: str) -> Optional[str]:
    if _EXCLUDE_RE.search(title):
        return None  # out-of-scope sport — don't risk a wrong-sport badge
    for pat, sport in _LEAGUE_HINTS:
        if pat.search(title):
            return sport
    return None


def parse_matchup(title: str, sport_hint: Optional[str] = None) -> Optional[tuple[str, str, str, str]]:
    """Return (away, home, sep_symbol, sport) or None.

    sport_hint lets the caller supply the sport from context (e.g. the channel
    is in a "Sports | MLB" group / has a BossSports.MLB.* id) when the title
    itself carries no league word ("Marlins at Pirates ᴸᶦᵛᵉ"). A title league
    hint still wins. sep_symbol is the divider ("@" for away-at-home, else "vs").
    """
    if not title:
        return None
    if _EXCLUDE_RE.search(title):
        return None  # out-of-scope sport even if a hint was supplied
    sport = league_sport(title) or sport_hint
    if not sport:
        return None  # graceful skip: no recognised league and no hint
    cleaned = title
    changed = True
    while changed:
        changed = False
        for pat in _TRAIL_PATTERNS:
            new = pat.sub("", cleaned).strip()
            if new != cleaned:
                cleaned, changed = new, True
    m = _MATCH_RE.search(cleaned)
    if not m:
        return None
    away = m.group("away").strip(" .-")
    home = m.group("home").strip(" .-")
    sep_raw = m.group("sep").lower().strip(". ")
    if len(away) < 2 or len(home) < 2 or _norm(away) == _norm(home):
        return None
    sep = "@" if sep_raw in ("@", "at") else "vs"
    return away, home, sep, sport


def _expand_team(name: str, sport: str) -> str:
    """Expand a bare nickname/abbrev to the full team name within `sport`, so the
    TheSportsDB search hits the right club. Full "City Nickname" inputs pass
    through unchanged (they resolve by direct search)."""
    m = _TEAMS.get(sport or "", {})
    if not m:
        return name
    nq = _norm(name)
    if nq in m:
        return m[nq]
    ab = re.sub(r"[^a-z0-9]", "", name.lower())  # bare abbrev like "det"/"nyy"
    if ab in m and ab != nq:
        return m[ab]
    parts = nq.split()                            # nickname = last 1-2 words
    for n in (2, 1):
        if len(parts) >= n:
            key = " ".join(parts[-n:])
            if key in m:
                return m[key]
    return name


# ── TheSportsDB resolution ───────────────────────────────────────────────────
SETTINGS_DB = BASE_DIR / "data" / "sources.db"


def _setting(key: str, default: str = "") -> str:
    """Read a settings-table value straight from sources.db (avoids importing
    main.py, which would be a circular import)."""
    try:
        con = sqlite3.connect(str(SETTINGS_DB), timeout=10)
        con.execute("PRAGMA busy_timeout=10000")
        try:
            row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return (row[0] if row and row[0] is not None else default)
        finally:
            con.close()
    except Exception:
        return default


def _api_key() -> str:
    return (_setting("thesportsdb_key", "3") or "3").strip() or "3"


_run_lookups = 0  # reset per export run via reset_run_budget()
_run_images = 0


def reset_run_budget():
    global _run_lookups, _run_images
    _run_lookups = 0
    _run_images = 0


def _resolve_badge(name: str, sport: str) -> Optional[str]:
    """name+sport → badge image URL (or None). Cached (incl. negatives)."""
    global _run_lookups
    q = _expand_team(name, sport)
    nq = _norm(q)
    if not nq:
        return None
    con = _conn()
    try:
        row = con.execute("SELECT url, ok, ts FROM badges WHERE q=? AND sport=?",
                          (nq, sport)).fetchone()
        now = int(time.time())
        stale_positive = None  # keep a fresh-enough resolved url to fall back on
        if row:
            url, ok, ts = row
            if ok and (now - ts < POS_TTL):
                return url or None
            if ok and url:
                # Positive expired — re-verify if budget allows, but never
                # regress to None just because the TTL lapsed.
                stale_positive = url
            elif not ok and now - ts < NEG_TTL:
                return None  # cached negative still fresh
        if _run_lookups >= _MAX_LIVE_LOOKUPS:
            return stale_positive
        _run_lookups += 1
        time.sleep(_THROTTLE)
        url = _api_search_badge(q, sport)
        # Don't downgrade a previously-resolved badge to a negative just because
        # a re-verify lookup came back empty (transient API hiccup) — keep it.
        if not url and stale_positive:
            url = stale_positive
        con.execute("INSERT OR REPLACE INTO badges(q,sport,url,ok,ts) VALUES(?,?,?,?,?)",
                    (nq, sport, url or "", 1 if url else 0, now))
        con.commit()
        return url
    except Exception:
        return None
    finally:
        con.close()


def _api_search_badge(name: str, sport: str) -> Optional[str]:
    url = (f"https://www.thesportsdb.com/api/v1/json/{_api_key()}"
           f"/searchteams.php?t={requests.utils.quote(name)}")
    r = requests.get(url, timeout=_HTTP_TIMEOUT, headers={"User-Agent": "m3u-boss"})
    r.raise_for_status()
    teams = (r.json() or {}).get("teams") or []
    if not teams:
        return None
    nq = _norm(name)
    def badge(t):
        return (t.get("strBadge") or t.get("strTeamBadge") or "").strip() or None
    # STRICT: when we know the sport, only consider same-sport teams. No
    # cross-sport fallback — a wrong-sport badge (Detroit Tigers -> Tübingen
    # basketball) is worse than no logo.
    pool = [t for t in teams if (t.get("strSport") or "") == sport] if sport else teams
    if not pool:
        return None
    for t in pool:                                   # exact full-name match
        if _norm(t.get("strTeam") or "") == nq:
            return badge(t)
    for t in pool:                                   # containment fallback
        nt = _norm(t.get("strTeam") or "")
        if nt and (nq in nt or nt in nq):
            return badge(t)
    return None  # no confident same-sport match -> skip


# ── image fetch + composite ──────────────────────────────────────────────────
def _fetch_badge_image(url: str):
    global _run_images
    from PIL import Image
    SRC_DIR.mkdir(parents=True, exist_ok=True)
    cache = SRC_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".png")
    if cache.exists():
        return Image.open(cache).convert("RGBA")
    if _run_images >= _MAX_LIVE_IMAGES:
        raise RuntimeError("image budget exhausted")
    _run_images += 1
    r = requests.get(url, timeout=_HTTP_TIMEOUT, headers={"User-Agent": "m3u-boss"})
    r.raise_for_status()
    img = Image.open(io.BytesIO(r.content)).convert("RGBA")
    try:
        img.save(cache)
    except Exception:
        pass
    return img


def _load_font(size: int):
    from PIL import ImageFont
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size)  # Pillow >= 10
    except Exception:
        return ImageFont.load_default()


def _compose(away_url: str, home_url: str, sep: str) -> Optional[bytes]:
    from PIL import Image, ImageDraw
    a = _fetch_badge_image(away_url)
    b = _fetch_badge_image(home_url)

    def scale(im):
        w = max(1, int(im.width * _BADGE_H / max(1, im.height)))
        return im.resize((w, _BADGE_H), Image.LANCZOS)

    a, b = scale(a), scale(b)
    pad, gap = 36, 96
    W = a.width + b.width + gap + pad * 2
    H = _BADGE_H + pad * 2
    canvas = Image.new("RGBA", (W, H), (17, 19, 26, 255))
    canvas.alpha_composite(a, (pad, pad))
    canvas.alpha_composite(b, (pad + a.width + gap, pad))
    d = ImageDraw.Draw(canvas)
    label = "VS" if sep == "vs" else "@"
    f = _load_font(58)
    d.text((pad + a.width + gap // 2, H // 2), label, fill=(240, 240, 245, 255),
           anchor="mm", font=f)
    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


# ── public API ───────────────────────────────────────────────────────────────
def match_logo_filename(title: str, sport_hint: Optional[str] = None) -> Optional[str]:
    """Title → composite logo filename (under LOGO_DIR), building+caching as
    needed. sport_hint supplies the sport from channel context when the title
    has no league word. Returns None when the title isn't a recognised matchup
    or either team can't be resolved. Never raises."""
    try:
        parsed = parse_matchup(title, sport_hint)
        if not parsed:
            return None
        away, home, sep, sport = parsed
        away_url = _resolve_badge(away, sport)
        home_url = _resolve_badge(home, sport)
        if not away_url or not home_url:
            return None  # graceful skip: don't guess
        key = hashlib.sha1(f"{away_url}|{home_url}|{sep}".encode()).hexdigest()[:20]
        fname = f"{key}.png"
        out_path = LOGO_DIR / fname

        con = _conn()
        try:
            row = con.execute("SELECT filename FROM composites WHERE key=?", (key,)).fetchone()
            if row and (LOGO_DIR / row[0]).exists():
                return row[0]
            data = _compose(away_url, home_url, sep)
            if not data:
                return None
            LOGO_DIR.mkdir(parents=True, exist_ok=True)
            tmp = out_path.with_suffix(".png.tmp")
            tmp.write_bytes(data)
            tmp.replace(out_path)
            con.execute("INSERT OR REPLACE INTO composites(key,filename,ts) VALUES(?,?,?)",
                        (key, fname, int(time.time())))
            con.commit()
            return fname
        finally:
            con.close()
    except Exception:
        return None


def clear_cache(negatives_only: bool = True) -> int:
    """Drop cached badge lookups so they get retried on the next export.

    negatives_only=True removes only the cached *failures* (ok=0); composites
    and resolved badges stay. negatives_only=False also forgets resolved badges
    (the composite PNGs on disk are kept and rebuilt on demand). Returns the
    number of badge rows removed. Never raises."""
    try:
        con = _conn()
        try:
            if negatives_only:
                cur = con.execute("DELETE FROM badges WHERE ok=0")
            else:
                cur = con.execute("DELETE FROM badges")
            con.commit()
            return cur.rowcount or 0
        finally:
            con.close()
    except Exception:
        return 0


def set_override(team: str, sport: str, badge_url: str) -> bool:
    """Pin a team's badge URL manually, overriding (or seeding) the resolver
    cache for the cases where TheSportsDB search can't find a team or picks the
    wrong one. Stored as a positive cache entry so it's used immediately and
    survives a negatives-only cache clear. Returns False on bad input."""
    sport = (sport or "").strip()
    url = (badge_url or "").strip()
    nq = _norm(_expand_team(team or "", sport))
    if not nq or not sport or not url:
        return False
    try:
        con = _conn()
        try:
            # Far-future ts keeps the positive permanently "fresh" so the
            # periodic re-verify (POS_TTL) never clobbers a manual override.
            forever = int(time.time()) + 100 * 365 * 24 * 3600
            con.execute("INSERT OR REPLACE INTO badges(q,sport,url,ok,ts) VALUES(?,?,?,?,?)",
                        (nq, sport, url, 1, forever))
            con.commit()
            return True
        finally:
            con.close()
    except Exception:
        return False


def match_logo_diagnose(title: str, sport_hint: Optional[str] = None) -> dict:
    """Explain whether a title yields a composite logo, and if not, WHY.

    Returns a dict with a ``status`` of:
      - ``ok``            both teams resolved → composite available
      - ``not_matchup``   title isn't a recognised league matchup (or sport is
                          out of scope / no league word + no hint)
      - ``unresolved``    parsed as a matchup but a team badge couldn't be found
    Plus parsed fields (away/home/sport) and the list of unresolved teams.
    Never raises — diagnostics must not break an export."""
    try:
        parsed = parse_matchup(title, sport_hint)
        if not parsed:
            return {"status": "not_matchup", "title": title}
        away, home, sep, sport = parsed
        away_url = _resolve_badge(away, sport)
        home_url = _resolve_badge(home, sport)
        missing = [t for t, u in ((away, away_url), (home, home_url)) if not u]
        return {
            "status": "ok" if not missing else "unresolved",
            "title": title, "away": away, "home": home, "sport": sport,
            "missing": missing,
        }
    except Exception as exc:
        return {"status": "error", "title": title, "error": str(exc)}
