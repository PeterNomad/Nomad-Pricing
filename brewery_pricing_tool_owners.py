"""
Brewery Pricing & Forecast Tool
"""

import math, json, os, re, uuid
from datetime import datetime, timedelta
from difflib import SequenceMatcher
import streamlit as st
import pandas as pd

st.set_page_config(
    page_title="Nomad Owners - Pricing Tool",
    page_icon="🍺",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────────────────────
# PASSWORD GATE
# Set your password in Streamlit Cloud secrets as:  APP_PASSWORD = "yourpassword"
# Or it falls back to the hardcoded default below (change before deploying).
# ─────────────────────────────────────────────────────────────────────────────
_CORRECT_PASSWORD = st.secrets.get("APP_PASSWORD", "nomad2024") if hasattr(st, "secrets") else "nomad2024"

if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

if not st.session_state["authenticated"]:
    st.markdown("## 🍺 Nomad Owners — Pricing Tool")
    st.markdown("Please enter the password to access this app.")
    col_pw, col_btn, _ = st.columns([2, 1, 3])
    pw_input = col_pw.text_input("Password", type="password", label_visibility="collapsed", placeholder="Enter password")
    if col_btn.button("Login", type="primary"):
        if pw_input == _CORRECT_PASSWORD:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password. Please try again.")
    st.stop()

# ─────────────────────────────────────────────────────────────────────────────
# CORE CALCULATION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def abv_class(abv: float) -> str:
    if abv < 0.03:      return "<3%"
    elif abv <= 0.035:  return "3%<=ABV<=3.5%"
    else:               return "ABV>3.5%"

def excise_per_package(pkg_type: str, abv: float, pkg_size_l: float, rates: dict) -> float:
    pkg_key = "Can" if pkg_type in ("Can", "4-Pack", "Case") else "Keg"
    rate = rates.get((pkg_key, abv_class(abv)), 0.0)
    return (abv - 0.0115) * pkg_size_l * rate

def compute_pricing(beer: dict, gi: dict) -> list:
    abv           = beer["abv"]
    batch_size    = beer["batch_size_l"]
    can_size      = beer["can_size_l"]
    keg_size      = beer["keg_size_l"]
    raw_mat       = beer["raw_materials"]
    target_margin = beer["base_margin"]
    royalty_pct   = beer["royalty_pct"]
    pak_tech      = beer["pak_tech"]
    cans_per_case = beer["cans_per_case"]

    excise_rates     = gi["excise_rates"]
    fixed_cost_per_l = gi["fixed_cost_per_l"]
    p                = gi["packaging"]
    channel_discounts = gi["channel_discounts"]

    raw_mat_per_l = raw_mat / batch_size if batch_size > 0 else 0
    pkg_can = (p["printed_can"] + p["can_lid"]
               + (p["pak_tech_per_can"] if pak_tech else 0)
               + p["carton_per_can"])
    pkg_keg       = p["keg_cost"]
    keg_pkg_per_l = pkg_keg / keg_size if keg_size > 0 else 0

    rows = []

    def make_row(channel, pkg_type, pkg_size_l, pkg_cost, r_and_e, excise, discount):
        cost      = excise + fixed_cost_per_l * pkg_size_l + r_and_e + pkg_cost + raw_mat_per_l * pkg_size_l
        sell_ex   = cost / (1 - target_margin) if target_margin < 1 else 0
        sell_inc  = sell_ex / (1 - royalty_pct) if royalty_pct > 0 else sell_ex
        roy_dollar = sell_inc - sell_ex
        sell_final = sell_inc * (1 - discount)
        sell_rnd   = round(sell_final)
        margin_dollar = sell_rnd - cost
        margin_pct    = margin_dollar / sell_rnd if sell_rnd > 0 else 0
        return {
            "Beer": beer["name"], "Channel": channel, "Package": pkg_type,
            "ABV": abv, "Package Size (L)": pkg_size_l,
            "Excise ($)":       round(excise, 4),
            "Fixed Cost ($)":   round(fixed_cost_per_l * pkg_size_l, 4),
            "R&E ($)":          round(r_and_e, 4),
            "Packaging ($)":    round(pkg_cost, 4),
            "Raw Materials ($)":round(raw_mat_per_l * pkg_size_l, 4),
            "Cost ($)":         round(cost, 4),
            "Sell Price ($)":   sell_rnd,
            "Margin ($)":       round(margin_dollar, 4),
            "Margin %":         round(margin_pct * 100, 2),
            "Royalty ($)":      round(roy_dollar, 4),
        }

    can_exc  = excise_per_package("Can", abv, can_size, excise_rates)
    keg_exc  = excise_per_package("Keg", abv, keg_size, excise_rates)
    case_exc = can_exc * cans_per_case
    re       = p["return_and_earn"]

    rows.append(make_row("Retail + Online", "Can",    can_size,               pkg_can,              re,              can_exc,  channel_discounts.get(("Retail + Online","Can"),   0)))
    rows.append(make_row("Retail + Online", "4-Pack", can_size*4,             pkg_can*4,            re*4,            can_exc*4,channel_discounts.get(("Retail + Online","4-Pack"),0)))
    rows.append(make_row("Retail + Online", "Case",   can_size*cans_per_case, pkg_can*cans_per_case,re*cans_per_case,case_exc, channel_discounts.get(("Retail + Online","Case"),  0)))
    rows.append(make_row("Retail + Online", "Keg",    keg_size,               pkg_keg,              0,               keg_exc,  channel_discounts.get(("Retail + Online","Keg"),   0)))
    rows.append(make_row("Wholesale",       "Case",   can_size*cans_per_case, pkg_can*cans_per_case,re*cans_per_case,case_exc, channel_discounts.get(("Wholesale","Case"),         0)))
    rows.append(make_row("Wholesale",       "Keg",    keg_size,               pkg_keg,              0,               keg_exc,  channel_discounts.get(("Wholesale","Keg"),          0)))

    for tp, ts in [("Middy",0.285),("Schooner",0.425),("Pint",0.568),("Jug",1.14)]:
        rows.append(make_row("Tap Room", tp, ts, keg_pkg_per_l*ts, 0,
                             excise_per_package("Keg", abv, ts, excise_rates),
                             channel_discounts.get(("Tap Room",tp), 0)))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# DEFAULTS
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_EXCISE_PERIODS = {
    "4 Aug 2025": {
        ("Can","<3%"): 52.87, ("Can","3%<=ABV<=3.5%"): 61.57, ("Can","ABV>3.5%"): 61.57,
        ("Keg","<3%"): 10.57, ("Keg","3%<=ABV<=3.5%"): 33.11, ("Keg","ABV>3.5%"): 43.39,
    },
    "2 Feb 2026": {
        ("Can","<3%"): 53.72, ("Can","3%<=ABV<=3.5%"): 62.56, ("Can","ABV>3.5%"): 62.56,
        ("Keg","<3%"): 10.57, ("Keg","3%<=ABV<=3.5%"): 33.11, ("Keg","ABV>3.5%"): 43.39,
    },
}

def default_general_inputs():
    return {
        "excise_periods":      DEFAULT_EXCISE_PERIODS,
        "active_excise_period":"2 Feb 2026",
        "excise_rates":        DEFAULT_EXCISE_PERIODS["2 Feb 2026"],
        "fixed_costs": {
            "rent": 170_000, "brewers": 162_060, "power": 30_000,
            "hire_et_al": 50_000, "rm": 50_000,
        },
        "annual_production_l": 168_417,
        "packaging": {
            "printed_can": 0.44, "can_lid": 0.065, "pak_tech_per_can": 0.0695,
            "carton_per_can": 0.06, "return_and_earn": 0.15, "keg_cost": 24.52,
        },
        "channel_discounts": {
            ("Retail + Online","Can"):    0.00,
            ("Retail + Online","4-Pack"): 0.10,
            ("Retail + Online","Case"):   0.20,
            ("Retail + Online","Keg"):    0.00,
            ("Wholesale","Case"):         0.25,
            ("Wholesale","Keg"):          0.35,
            ("Tap Room","Middy"):        -2.25,
            ("Tap Room","Schooner"):     -1.80,
            ("Tap Room","Pint"):         -1.70,
            ("Tap Room","Jug"):          -1.60,
        },
    }

def default_beers():
    return [
        {"name":"Rainbow Cherry",        "abv":0.060,"batch_size_l":2300,"can_size_l":0.440,"proportion_cans":0.65,"proportion_kegs":0.35,"cans_per_case":16,"keg_size_l":50,"pak_tech":False,"raw_materials":3820,   "base_margin":0.37,"royalty_pct":0.10,"active":True},
        {"name":"Rainbow Sherbet",       "abv":0.060,"batch_size_l":2300,"can_size_l":0.375,"proportion_cans":0.50,"proportion_kegs":0.50,"cans_per_case":24,"keg_size_l":50,"pak_tech":True, "raw_materials":2757,   "base_margin":0.37,"royalty_pct":0.10,"active":True},
        {"name":"Queensie Lager",        "abv":0.050,"batch_size_l":2500,"can_size_l":0.375,"proportion_cans":0.50,"proportion_kegs":0.50,"cans_per_case":16,"keg_size_l":50,"pak_tech":True, "raw_materials":1752.51,"base_margin":0.37,"royalty_pct":0.00,"active":True},
        {"name":"Surf Mist Hazy",        "abv":0.041,"batch_size_l":2200,"can_size_l":0.375,"proportion_cans":0.50,"proportion_kegs":0.50,"cans_per_case":16,"keg_size_l":50,"pak_tech":True, "raw_materials":2115,   "base_margin":0.37,"royalty_pct":0.00,"active":True},
        {"name":"Hop Symphony Ale",      "abv":0.054,"batch_size_l":2200,"can_size_l":0.375,"proportion_cans":0.50,"proportion_kegs":0.50,"cans_per_case":16,"keg_size_l":50,"pak_tech":True, "raw_materials":1667.15,"base_margin":0.37,"royalty_pct":0.00,"active":True},
        {"name":"Monsoon IPA",           "abv":0.064,"batch_size_l":2000,"can_size_l":0.375,"proportion_cans":0.50,"proportion_kegs":0.50,"cans_per_case":16,"keg_size_l":50,"pak_tech":True, "raw_materials":2069.87,"base_margin":0.37,"royalty_pct":0.00,"active":True},
        {"name":"Mountains Cold IPA",    "abv":0.063,"batch_size_l":2200,"can_size_l":0.375,"proportion_cans":0.50,"proportion_kegs":0.50,"cans_per_case":16,"keg_size_l":50,"pak_tech":True, "raw_materials":2575.58,"base_margin":0.37,"royalty_pct":0.00,"active":True},
        {"name":"Budgy Pale Ale",        "abv":0.050,"batch_size_l":2400,"can_size_l":0.375,"proportion_cans":0.50,"proportion_kegs":0.50,"cans_per_case":16,"keg_size_l":50,"pak_tech":False,"raw_materials":1470.08,"base_margin":0.37,"royalty_pct":0.00,"active":True},
        {"name":"Coastal Mid Strength",  "abv":0.033,"batch_size_l":2600,"can_size_l":0.375,"proportion_cans":0.50,"proportion_kegs":0.50,"cans_per_case":16,"keg_size_l":50,"pak_tech":True, "raw_materials":1083.44,"base_margin":0.37,"royalty_pct":0.00,"active":True},
        {"name":"Baroness Red IPA",      "abv":0.065,"batch_size_l":2000,"can_size_l":0.375,"proportion_cans":0.50,"proportion_kegs":0.50,"cans_per_case":16,"keg_size_l":50,"pak_tech":True, "raw_materials":1851.07,"base_margin":0.37,"royalty_pct":0.00,"active":True},
        {"name":"Gweilo Raspberry Waffle","abv":0.060,"batch_size_l":2250,"can_size_l":0.440,"proportion_cans":0.50,"proportion_kegs":0.50,"cans_per_case":16,"keg_size_l":50,"pak_tech":False,"raw_materials":5134.37,"base_margin":0.37,"royalty_pct":0.10,"active":True},
        {"name":"Pier 39 WC IPA",        "abv":0.067,"batch_size_l":2200,"can_size_l":0.375,"proportion_cans":0.50,"proportion_kegs":0.50,"cans_per_case":16,"keg_size_l":50,"pak_tech":True, "raw_materials":2157.36,"base_margin":0.37,"royalty_pct":0.00,"active":True},
        {"name":"New Beer 1",            "abv":0.050,"batch_size_l":2000,"can_size_l":0.375,"proportion_cans":0.50,"proportion_kegs":0.50,"cans_per_case":16,"keg_size_l":50,"pak_tech":True, "raw_materials":1500,   "base_margin":0.37,"royalty_pct":0.00,"active":False},
    ]


# ─────────────────────────────────────────────────────────────────────────────
# PERSISTENCE
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "brewery_settings.json")
BEERS_FILE    = os.path.join(BASE_DIR, "brewery_beers.json")
HISTORY_FILE  = os.path.join(BASE_DIR, "brewery_price_history.json")

# ─────────────────────────────────────────────────────────────────────────────
# GITHUB SYNC
# Streamlit Cloud's local disk is wiped and rebuilt from the GitHub repo on
# every reboot/redeploy/sleep-wake cycle. Anything saved only to local disk
# (the files above) is lost the moment that happens. To make data durable we
# write every save straight back to the GitHub repo via the Contents API, in
# addition to the local copy. Local disk is still used as a same-session
# cache and as a fallback if GitHub is unreachable.
#
# Required Streamlit Cloud secrets (App settings → Secrets):
#   GITHUB_REPO   = "yourusername/your-repo-name"
#   GITHUB_TOKEN  = "github_pat_..."   (fine-grained token, Contents: Read & Write, scoped to this repo)
#   GITHUB_BRANCH = "main"             (optional, defaults to "main")
# ─────────────────────────────────────────────────────────────────────────────
import base64
import requests

GITHUB_REPO    = st.secrets.get("GITHUB_REPO", "")        if hasattr(st, "secrets") else ""
GITHUB_TOKEN   = st.secrets.get("GITHUB_TOKEN", "")       if hasattr(st, "secrets") else ""
GITHUB_BRANCH  = st.secrets.get("GITHUB_BRANCH", "main")  if hasattr(st, "secrets") else "main"
GITHUB_ENABLED = bool(GITHUB_REPO and GITHUB_TOKEN)

def _gh_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def github_get_file(filename):
    """Fetch a file's content + sha from the repo root. Returns (content_str, sha) or (None, None)."""
    if not GITHUB_ENABLED:
        return None, None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    try:
        resp = requests.get(url, headers=_gh_headers(), params={"ref": GITHUB_BRANCH}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]
        return None, None
    except Exception:
        return None, None

@st.cache_data(ttl=20, show_spinner=False)
def github_get_file_cached(filename):
    """Short-TTL cache so repeat page views don't hammer the GitHub API."""
    return github_get_file(filename)

def github_put_file(filename, content_str, message):
    """Create or update a file at the repo root. Returns True on success."""
    if not GITHUB_ENABLED:
        return False
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    _, sha = github_get_file(filename)   # always fetch a fresh sha, never the cached one
    payload = {
        "message": message,
        "content": base64.b64encode(content_str.encode("utf-8")).decode("utf-8"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    try:
        resp = requests.put(url, headers=_gh_headers(), json=payload, timeout=10)
        ok = resp.status_code in (200, 201)
        if ok:
            github_get_file_cached.clear()
        return ok
    except Exception:
        return False

def _ep_to_json(ep):
    return {d: {"|".join(k): v for k, v in rates.items()} for d, rates in ep.items()}

def _ep_from_json(d):
    return {ds: {tuple(k.split("|")): v for k, v in rates.items()} for ds, rates in d.items()}

def _cd_to_json(cd):
    return {"|".join(k): v for k, v in cd.items()}

def _cd_from_json(d):
    return {tuple(k.split("|")): v for k, v in d.items()}

def _gi_to_json(gi):
    return {
        "excise_periods":       _ep_to_json(gi.get("excise_periods", {})),
        "active_excise_period": gi.get("active_excise_period", ""),
        "excise_rates":        {"|".join(k): v for k, v in gi["excise_rates"].items()},
        "fixed_costs":          gi["fixed_costs"],
        "annual_production_l":  gi["annual_production_l"],
        "packaging":            gi["packaging"],
        "channel_discounts":    _cd_to_json(gi["channel_discounts"]),
    }

def _gi_from_json(d):
    return {
        "excise_periods":       _ep_from_json(d.get("excise_periods", {})),
        "active_excise_period": d.get("active_excise_period", ""),
        "excise_rates":        {tuple(k.split("|")): v for k, v in d["excise_rates"].items()},
        "fixed_costs":          d["fixed_costs"],
        "annual_production_l":  d["annual_production_l"],
        "packaging":            d["packaging"],
        "channel_discounts":    _cd_from_json(d["channel_discounts"]),
    }

def save_settings(gi, beers):
    settings_json = json.dumps(_gi_to_json(gi), indent=2)
    beers_json    = json.dumps(beers, indent=2)
    try:
        with open(SETTINGS_FILE, "w") as f: f.write(settings_json)
        with open(BEERS_FILE,    "w") as f: f.write(beers_json)
    except Exception as e:
        return str(e)
    if GITHUB_ENABLED:
        ok1 = github_put_file("brewery_settings.json", settings_json, "Update brewery_settings.json via app")
        ok2 = github_put_file("brewery_beers.json",     beers_json,    "Update brewery_beers.json via app")
        if not (ok1 and ok2):
            return "Saved locally, but GitHub sync failed — check GITHUB_REPO/GITHUB_TOKEN in secrets."
    return True

def load_settings():
    gi, beers = None, None
    raw_settings, raw_beers = (None, None)
    if GITHUB_ENABLED:
        raw_settings, _ = github_get_file_cached("brewery_settings.json")
        raw_beers,    _ = github_get_file_cached("brewery_beers.json")
    if raw_settings:
        try: gi = _gi_from_json(json.loads(raw_settings))
        except Exception: gi = None
    elif os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f: gi = _gi_from_json(json.load(f))
        except Exception: gi = None
    if raw_beers:
        try: beers = json.loads(raw_beers)
        except Exception: beers = None
    elif os.path.exists(BEERS_FILE):
        try:
            with open(BEERS_FILE) as f: beers = json.load(f)
        except Exception: beers = None
    return gi or default_general_inputs(), beers or default_beers()

def load_history():
    if GITHUB_ENABLED:
        raw, _ = github_get_file_cached("brewery_price_history.json")
        if raw:
            try: return json.loads(raw)
            except Exception: pass
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f: return json.load(f)
        except Exception: pass
    return []

def save_history(history):
    content = json.dumps(history, indent=2)
    try:
        with open(HISTORY_FILE, "w") as f: f.write(content)
    except Exception: pass
    if GITHUB_ENABLED:
        github_put_file("brewery_price_history.json", content, "Update brewery_price_history.json via app")

def record_price_snapshot(label, beers, gi):
    history = load_history()
    snap = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "label": label,
        "excise_period": gi.get("active_excise_period", ""),
        "prices": [],
    }
    for beer in beers:
        if not beer.get("active"): continue
        try:
            for r in compute_pricing(beer, gi):
                snap["prices"].append({
                    "beer": r["Beer"], "channel": r["Channel"],
                    "package": r["Package"], "sell_price": r["Sell Price ($)"],
                    "cost": r["Cost ($)"], "margin_pct": r["Margin %"],
                })
        except Exception: pass
    history.append(snap)
    save_history(history)


# ─────────────────────────────────────────────────────────────────────────────
# SESSION STATE INIT
# ─────────────────────────────────────────────────────────────────────────────
if "gi" not in st.session_state or "beers" not in st.session_state:
    gi, beers = load_settings()
    st.session_state.gi    = gi
    st.session_state.beers = beers

# Always recalculate fixed_cost_per_l
fc = st.session_state.gi["fixed_costs"]
st.session_state.gi["fixed_cost_per_l"] = (
    sum(fc.values()) / st.session_state.gi["annual_production_l"]
    if st.session_state.gi["annual_production_l"] > 0 else 0
)

# Sync active excise rates
aep = st.session_state.gi.get("active_excise_period", "")
periods = st.session_state.gi.get("excise_periods", {})
if aep in periods:
    st.session_state.gi["excise_rates"] = periods[aep]


# ─────────────────────────────────────────────────────────────────────────────
# CACHE + COMPUTE
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data
def compute_all(beers_tuple, gi_frozen):
    gi = json.loads(gi_frozen)
    gi["excise_rates"]      = {tuple(k.split("|")): v for k, v in gi["excise_rates"].items()}
    gi["channel_discounts"] = {tuple(k.split("|")): v for k, v in gi["channel_discounts"].items()}
    rows = []
    for beer in [dict(b) for b in beers_tuple]:
        if not beer.get("active"): continue
        if beer["batch_size_l"] <= 0 or beer["abv"] <= 0: continue
        try: rows.extend(compute_pricing(beer, gi))
        except Exception: pass
    return pd.DataFrame(rows)

def gi_to_frozen(gi):
    return json.dumps({
        "excise_rates":      {"|".join(k): v for k, v in gi["excise_rates"].items()},
        "channel_discounts": {"|".join(k): v for k, v in gi["channel_discounts"].items()},
        "fixed_costs":        gi["fixed_costs"],
        "annual_production_l":gi["annual_production_l"],
        "packaging":          gi["packaging"],
        "fixed_cost_per_l":   gi["fixed_cost_per_l"],
    }, sort_keys=True)

df_all = compute_all(
    tuple(tuple(sorted(b.items())) for b in st.session_state.beers),
    gi_to_frozen(st.session_state.gi)
)


# ─────────────────────────────────────────────────────────────────────────────
# VALIDATION — detect unchanged defaults when adding a beer
# ─────────────────────────────────────────────────────────────────────────────
def unchanged_fields(beer):
    """
    Only flag the beer name if it is still the placeholder 'New Beer'.
    Other fields vary legitimately between beers so value-matching against
    fixed defaults produces too many false positives.
    """
    issues = []
    if beer.get("name", "").strip().lower() == "new beer":
        issues.append("Beer Name (still set to 'New Beer')")
    return issues


# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────────────────────
st.sidebar.title("🍺 Nomad Owners")
page = st.sidebar.radio("Navigate", [
    "📊 Price Lookup", "📋 Price Lists",
    "📦 Batches & Stocktake", "📈 Stock Forecast",
], label_visibility="collapsed")

# Sidebar: show active excise period
st.sidebar.markdown("---")
st.sidebar.caption(f"Active excise period: **{st.session_state.gi.get('active_excise_period','')}**")
if GITHUB_ENABLED:
    st.sidebar.caption(f"☁️ GitHub sync: **on** ({GITHUB_REPO} @ {GITHUB_BRANCH})")
else:
    st.sidebar.caption("⚠️ GitHub sync: **off** — saves only last until the app reboots. Add `GITHUB_REPO` and `GITHUB_TOKEN` to Secrets to enable.")


# ─────────────────────────────────────────────────────────────────────────────
# EXCISE RETURN HELPERS
# ─────────────────────────────────────────────────────────────────────────────

EXCISE_HISTORY_FILE = os.path.join(BASE_DIR, "brewery_excise_history.json")

def load_excise_history():
    if GITHUB_ENABLED:
        raw, _ = github_get_file_cached("brewery_excise_history.json")
        if raw:
            try: return json.loads(raw)
            except Exception: pass
    if os.path.exists(EXCISE_HISTORY_FILE):
        try:
            with open(EXCISE_HISTORY_FILE) as f: return json.load(f)
        except Exception: pass
    return []

def save_excise_history(history):
    content = json.dumps(history, indent=2)
    try:
        with open(EXCISE_HISTORY_FILE, "w") as f: f.write(content)
    except Exception as e:
        return str(e)
    if GITHUB_ENABLED:
        if not github_put_file("brewery_excise_history.json", content, "Update brewery_excise_history.json via app"):
            return "Saved locally, but GitHub sync failed — check GITHUB_REPO/GITHUB_TOKEN in secrets."
    return True

def normalise_beer_name(name):
    """Strip brand prefixes and package suffixes for fuzzy matching."""
    n = name.lower().strip()
    for prefix in ["nomad - ", "nomad-", "gweilo - ", "gweilo-", "nomad ", "gweilo "]:
        if n.startswith(prefix):
            n = n[len(prefix):]
    n = re.sub(r'\s*-\s*(can|keg|case|single|4.pack)$', '', n)
    n = re.sub(r'\s*(can|keg|case)$', '', n)
    n = re.sub(r'^[a-z]{2,5}\s*-\s*', '', n)   # strip short codes like "QLC - "
    parts = n.split(" - ")
    if len(parts) >= 2:
        n = max(parts, key=len)
    return n.strip()

def fuzzy_match(source_name, beer_names, threshold=0.50):
    """Return (best_match_name, score) or (None, 0) if below threshold."""
    best, best_score = None, 0
    sn = normalise_beer_name(source_name)
    for b in beer_names:
        score = SequenceMatcher(None, sn, normalise_beer_name(b)).ratio()
        if score > best_score:
            best, best_score = b, score
    return (best, best_score) if best_score >= threshold else (None, 0)

SERVE_SIZE_L = {
    "jug - 1140ml": 1.140, "jug": 1.140,
    "schooner - 425ml": 0.425, "schooner": 0.425,
    "happy hour schooner": 0.425, "happy hour": 0.425,
    "pint - 570ml": 0.568, "pint - 568ml": 0.568, "pint": 0.568,
    "middy - 285ml": 0.285, "middy": 0.285,
    "growler - 1900ml": 1.900, "growler": 1.900,
    "single": "can",
    "4-pack": "4-pack",
    "case": "case", "case special": "case",
}

XERO_EXCLUDE = ["delivery", "rent", "rubbish", "note", "kegswappa",
                "total", "summary", "sales by", "other sales", "cash sales",
                "credits", "total sales"]

def parse_square_email(body_text, beer_lookup):
    """
    Parse Square email body. Returns list of dicts:
    {beer_name, source_name, package_type (can/keg), serve, qty, litres_each, total_litres, matched, score}
    beer_lookup: {beer_name: {can_size_l, cans_per_case, keg_size_l, abv}}
    """
    lines = [l.strip() for l in body_text.split("\r\n") if l.strip()]
    start = next((i for i, l in enumerate(lines) if l == "Item Sales"), 0)
    lines = lines[start:]

    parent_pat = re.compile(r'^(.+?)\s+×\s+(\d+)\s+\t\$[\d,]+\.\d+')

    def is_beer_parent(name):
        return bool(re.search(r'\s-\s(Can|Keg)$', name))

    current_parent = None
    current_is_ours = False
    results = []

    for line in lines:
        m = parent_pat.match(line)
        if not m: continue
        name = m.group(1).strip()
        qty  = int(m.group(2))

        if is_beer_parent(name):
            current_parent   = name
            current_is_ours  = ("Nomad" in name or "Gweilo" in name)
        elif current_is_ours and current_parent:
            serve_key = name.lower()
            if serve_key not in SERVE_SIZE_L: continue
            size_info = SERVE_SIZE_L[serve_key]
            pkg_type  = "keg" if re.search(r'\s-\sKeg$', current_parent) else "can"

            # Resolve litres
            matched_beer, score = fuzzy_match(current_parent,
                                              list(beer_lookup.keys()))
            beer_data = beer_lookup.get(matched_beer, {})
            can_size  = beer_data.get("can_size_l",  0.375)
            cpc       = beer_data.get("cans_per_case", 16)

            if isinstance(size_info, float):
                litres_each = size_info
            elif size_info == "can":
                litres_each = can_size
            elif size_info == "4-pack":
                litres_each = can_size * 4
            elif size_info == "case":
                litres_each = can_size * cpc
            else:
                litres_each = 0

            results.append({
                "source": "Tap Room (Square)",
                "source_name": current_parent,
                "serve": name,
                "beer_name": matched_beer,
                "match_score": round(score, 2),
                "package_type": pkg_type,
                "qty": qty,
                "litres_each": round(litres_each, 4),
                "total_litres": round(qty * litres_each, 4),
                "abv": beer_data.get("abv", None),
            })
    return results

def parse_xero_report(df_raw, beer_lookup):
    """
    Parse Xero Sales by Item report dataframe.
    Returns list of dicts similar to parse_square_email.
    """
    # Find header row
    header_row = None
    for i, row in df_raw.iterrows():
        if "Item" in str(row.values) and "Quantity" in str(row.values):
            header_row = i
            break
    if header_row is None:
        return []
    df_raw.columns = df_raw.iloc[header_row]
    df = df_raw.iloc[header_row+1:].reset_index(drop=True)

    results = []
    for _, row in df.iterrows():
        item = row.get("Item","")
        qty_raw = row.get("Quantity Sold", None)
        if not isinstance(item, str) or not item.strip(): continue
        item_l = item.lower()
        if any(kw in item_l for kw in XERO_EXCLUDE): continue
        try:
            qty = int(float(qty_raw))
        except: continue
        if qty <= 0: continue

        # Detect package
        keg_size_m = re.search(r'(\d+)l\s*keg|keg.*?(\d+)l', item_l)
        if keg_size_m:
            keg_l = int(keg_size_m.group(1) or keg_size_m.group(2))
            pkg_type = "keg"
        elif "keg" in item_l:
            keg_l = 50
            pkg_type = "keg"
        else:
            pkg_type = "can"
            keg_l = None

        matched_beer, score = fuzzy_match(item, list(beer_lookup.keys()))
        beer_data = beer_lookup.get(matched_beer, {})
        can_size  = beer_data.get("can_size_l",  0.375)
        cpc       = beer_data.get("cans_per_case", 16)

        if pkg_type == "keg":
            litres_each = keg_l if keg_l else 50
        else:
            litres_each = can_size * cpc

        results.append({
            "source": "Wholesale (Xero)",
            "source_name": item,
            "serve": "Case" if pkg_type == "can" else f"Keg {keg_l or 50}L",
            "beer_name": matched_beer,
            "match_score": round(score, 2),
            "package_type": pkg_type,
            "qty": qty,
            "litres_each": round(litres_each, 4),
            "total_litres": round(qty * litres_each, 4),
            "abv": beer_data.get("abv", None),
        })
    return results

def taxable_ethanol(beer_litres, abv):
    """
    ATO excise formula: (ABV% - 1.15%) × beer volume in litres = litres of pure ethanol
    The 1.15% deduction is a standard ATO allowance applied to all beer.
    """
    return max(abv - 0.0115, 0) * beer_litres

def compute_excise_summary(rows):
    """
    Summarise rows into the 4 excise buckets:
    Can ≤3.5%, Can >3.5%, Keg ≤3.5%, Keg >3.5%
    Values are litres of PURE ETHANOL (taxable), not beer volume.
    Formula: (ABV - 1.15%) × beer_litres
    """
    buckets = {
        ("can", "lte35"): 0.0,
        ("can", "gt35"):  0.0,
        ("keg", "lte35"): 0.0,
        ("keg", "gt35"):  0.0,
    }
    for r in rows:
        abv = r.get("abv")
        if abv is None: continue
        pkg  = r["package_type"]
        buck = "lte35" if abv <= 0.035 else "gt35"
        buckets[(pkg, buck)] += taxable_ethanol(r["total_litres"], abv)
    return buckets


# ─────────────────────────────────────────────────────────────────────────────
# INVENTORY — PERSISTENCE (batches & stocktakes)
# Follows the same local-file + GitHub-sync pattern as the other data files.
# ─────────────────────────────────────────────────────────────────────────────

BATCHES_FILE    = os.path.join(BASE_DIR, "brewery_batches.json")
STOCKTAKES_FILE = os.path.join(BASE_DIR, "brewery_stocktakes.json")

KEG_SIZES  = [10, 20, 30, 50]   # standard keg sizes in litres
CASE_SIZES = [16, 24]           # standard case sizes in cans-per-case

def _keg_field(size_l):
    """Field name for a keg size, e.g. 10 -> 'kegs_10l', 19.5 -> 'kegs_19_5l'."""
    s = str(size_l).replace(".", "_")
    return f"kegs_{s}l"

def _case_field(cans_per_case):
    """Field name for a case size, e.g. 16 -> 'cases_16', 24 -> 'cases_24'."""
    return f"cases_{int(cans_per_case)}"

def load_batches():
    if GITHUB_ENABLED:
        raw, _ = github_get_file_cached("brewery_batches.json")
        if raw:
            try: return json.loads(raw)
            except Exception: pass
    if os.path.exists(BATCHES_FILE):
        try:
            with open(BATCHES_FILE) as f: return json.load(f)
        except Exception: pass
    return []

def save_batches(batches):
    content = json.dumps(batches, indent=2)
    try:
        with open(BATCHES_FILE, "w") as f: f.write(content)
    except Exception as e:
        return str(e)
    if GITHUB_ENABLED:
        if not github_put_file("brewery_batches.json", content, "Update brewery_batches.json via app"):
            return "Saved locally, but GitHub sync failed — check GITHUB_REPO/GITHUB_TOKEN in secrets."
    return True

def load_stocktakes():
    if GITHUB_ENABLED:
        raw, _ = github_get_file_cached("brewery_stocktakes.json")
        if raw:
            try: return json.loads(raw)
            except Exception: pass
    if os.path.exists(STOCKTAKES_FILE):
        try:
            with open(STOCKTAKES_FILE) as f: return json.load(f)
        except Exception: pass
    return []

def save_stocktakes(stocktakes):
    content = json.dumps(stocktakes, indent=2)
    try:
        with open(STOCKTAKES_FILE, "w") as f: f.write(content)
    except Exception as e:
        return str(e)
    if GITHUB_ENABLED:
        if not github_put_file("brewery_stocktakes.json", content, "Update brewery_stocktakes.json via app"):
            return "Saved locally, but GitHub sync failed — check GITHUB_REPO/GITHUB_TOKEN in secrets."
    return True


# ─────────────────────────────────────────────────────────────────────────────
# INVENTORY — CORE CALCULATION HELPERS
#
# Design (agreed with Peter):
#   • SOH is depleted FIFO — the oldest active batch of a beer is drawn down
#     first, using monthly beer sales volumes from the Excise Return history.
#   • Cans and kegs are tracked as separate litre pools per batch (excise data
#     distinguishes can vs keg sales), so a can-heavy sales month depletes can
#     stock first without touching keg stock, and vice versa.
#   • A stocktake is a ground-truth correction: it overwrites a batch's SOH
#     (cans + kegs-by-size) as at the stocktake date. Only excise sales dated
#     after the most recent stocktake for a batch are deducted on top of it.
#   • The keg-size breakdown (10L/19.5L/20L/30L/50L) is only precise at
#     production and immediately after a stocktake. Between stocktakes, keg
#     depletion is applied to the aggregate keg litre pool (excise data
#     doesn't tell us which keg size was poured), so the per-size split shown
#     for a batch mid-cycle is an estimate. This is normal — stocktakes are
#     there to periodically true it back up.
# ─────────────────────────────────────────────────────────────────────────────

def batch_total_cans(batch):
    """Total cans produced, from case quantities (+ any legacy loose 'cans_produced' value)."""
    return (sum(batch.get(_case_field(c), 0) * c for c in CASE_SIZES)
            + batch.get("cans_produced", 0))  # legacy field, kept for batches recorded before case tracking

def batch_produced_can_litres(batch):
    return batch_total_cans(batch) * batch.get("can_size_l", 0.375)

def batch_produced_keg_litres(batch):
    return sum(batch.get(_keg_field(s), 0) * s for s in KEG_SIZES)

def batch_total_litres(batch):
    return batch_produced_can_litres(batch) + batch_produced_keg_litres(batch)

def get_excise_monthly_sales_by_package(beer_name):
    """
    Return {period_key: {"can": litres, "keg": litres}} for a beer, built from
    saved Excise Return history (which is itself sourced from Square/Xero/manual
    entries — i.e. actual historical sales volumes).
    """
    history = load_excise_history()
    sales = {}
    for h in history:
        pk = h.get("period_key", "")
        can_l = sum(r.get("total_litres", 0) for r in h.get("rows", [])
                    if r.get("beer_name") == beer_name and r.get("package_type") == "can")
        keg_l = sum(r.get("total_litres", 0) for r in h.get("rows", [])
                    if r.get("beer_name") == beer_name and r.get("package_type") == "keg")
        if can_l > 0 or keg_l > 0:
            entry = sales.setdefault(pk, {"can": 0.0, "keg": 0.0})
            entry["can"] += can_l
            entry["keg"] += keg_l
    return sales

def compute_soh_for_beer(beer_name, batches, stocktakes):
    """
    Compute current SOH per active batch of a beer using FIFO depletion.
    Returns a list of batch-state dicts (oldest batch first), each with:
      batch_id, batch_number, brew_date, use_by_date, can_size_l,
      soh_can_litres, soh_keg_litres, soh_cans_est, soh_kegs_by_size (dict, estimate),
      last_stocktake_date
    """
    beer_batches = sorted(
        [b for b in batches if b.get("beer_name") == beer_name and not b.get("archived", False)],
        key=lambda b: (b.get("brew_date", ""), b.get("batch_number", ""))
    )
    if not beer_batches:
        return []

    beer_stocktakes = [s for s in stocktakes if s.get("beer_name") == beer_name]

    states = []
    for b in beer_batches:
        state = {
            "batch_id":     b["batch_id"],
            "batch_number": b.get("batch_number", ""),
            "brew_date":    b.get("brew_date", ""),
            "use_by_date":  b.get("use_by_date", ""),
            "can_size_l":   b.get("can_size_l", 0.375),
            "soh_can_litres": batch_produced_can_litres(b),
            "soh_keg_litres": batch_produced_keg_litres(b),
            "soh_kegs_by_size":  {s: b.get(_keg_field(s), 0) for s in KEG_SIZES},
            "soh_cases_by_size": {c: b.get(_case_field(c), 0) for c in CASE_SIZES},
            "last_stocktake_date": None,
            "baseline_date": b.get("brew_date", ""),  # sales after this date get deducted
        }
        states.append(state)

    # Apply most recent stocktake per batch as a new baseline
    for state in states:
        matching = [s for s in beer_stocktakes if s.get("batch_id") == state["batch_id"]]
        if matching:
            latest = sorted(matching, key=lambda s: s.get("stocktake_date", ""))[-1]
            state["soh_cases_by_size"] = {c: latest.get(_case_field(c), 0) for c in CASE_SIZES}
            counted_cans = sum(state["soh_cases_by_size"][c] * c for c in CASE_SIZES) + latest.get("soh_cans", 0)
            state["soh_can_litres"] = counted_cans * state["can_size_l"]
            state["soh_kegs_by_size"] = {s: latest.get(_keg_field(s), 0) for s in KEG_SIZES}
            state["soh_keg_litres"] = sum(state["soh_kegs_by_size"][s] * s for s in KEG_SIZES)
            state["last_stocktake_date"] = latest.get("stocktake_date", "")
            state["baseline_date"] = latest.get("stocktake_date", "")

    # FIFO-deduct excise sales that occurred after each batch's baseline date,
    # walking oldest batch first, separately for cans and kegs.
    monthly_sales = get_excise_monthly_sales_by_package(beer_name)
    for period_key in sorted(monthly_sales.keys()):
        # Only apply a period's sales once per beer, to the batch pool as a whole —
        # skip periods that are clearly before every batch existed.
        can_to_deduct = monthly_sales[period_key]["can"]
        keg_to_deduct = monthly_sales[period_key]["keg"]
        period_end = period_key + "-28"  # rough month-end for comparison

        for state in states:
            if can_to_deduct <= 0:
                break
            if period_end <= state["baseline_date"]:
                continue
            take = min(state["soh_can_litres"], can_to_deduct)
            state["soh_can_litres"] -= take
            can_to_deduct -= take

        for state in states:
            if keg_to_deduct <= 0:
                break
            if period_end <= state["baseline_date"]:
                continue
            take = min(state["soh_keg_litres"], keg_to_deduct)
            state["soh_keg_litres"] -= take
            keg_to_deduct -= take

    for state in states:
        state["soh_can_litres"] = max(0.0, round(state["soh_can_litres"], 3))
        state["soh_keg_litres"] = max(0.0, round(state["soh_keg_litres"], 3))
        state["soh_cans_est"]   = round(state["soh_can_litres"] / state["can_size_l"], 1) if state["can_size_l"] else 0
        state["soh_total_litres"] = round(state["soh_can_litres"] + state["soh_keg_litres"], 3)

    return states

def weighted_avg_monthly_sales(beer_name, window_months=3, weighted=True):
    """
    Average monthly total litres sold (can + keg combined) for a beer over the
    most recent `window_months` excise periods. If weighted, more recent
    months are weighted more heavily (linear weights 1..window_months).
    Returns 0 if there's no sales history.
    """
    monthly = get_excise_monthly_sales_by_package(beer_name)
    if not monthly:
        return 0.0
    periods = sorted(monthly.keys())[-window_months:]
    if not periods:
        return 0.0
    totals = [monthly[p]["can"] + monthly[p]["keg"] for p in periods]
    if weighted:
        weights = list(range(1, len(totals) + 1))  # oldest=1 ... newest=n
        return sum(t * w for t, w in zip(totals, weights)) / sum(weights)
    return sum(totals) / len(totals)

def weighted_avg_monthly_sales_split(beer_name, window_months=3, weighted=True):
    """
    Same as weighted_avg_monthly_sales, but returns (avg_can_litres, avg_keg_litres)
    separately, using ONLY the most recent `window_months` excise periods that exist.
    Returns (None, None) if the beer has no excise history at all.
    """
    monthly = get_excise_monthly_sales_by_package(beer_name)
    if not monthly:
        return None, None
    periods = sorted(monthly.keys())[-window_months:]
    if not periods:
        return None, None
    can_totals = [monthly[p]["can"] for p in periods]
    keg_totals = [monthly[p]["keg"] for p in periods]
    if weighted:
        weights = list(range(1, len(periods) + 1))
        avg_can = sum(t * w for t, w in zip(can_totals, weights)) / sum(weights)
        avg_keg = sum(t * w for t, w in zip(keg_totals, weights)) / sum(weights)
    else:
        avg_can = sum(can_totals) / len(can_totals)
        avg_keg = sum(keg_totals) / len(keg_totals)
    return avg_can, avg_keg

def excise_has_any_data(beer_name, package_type):
    """Whether the beer has ANY recorded excise sales (ever, any period) for cans or kegs."""
    history = load_excise_history()
    for h in history:
        for r in h.get("rows", []):
            if r.get("beer_name") == beer_name and r.get("package_type") == package_type and r.get("total_litres", 0) > 0:
                return True
    return False

def soh_trend_monthly_rate(beer_name, batches, stocktakes, field_name, window_months, today):
    """
    Infer a monthly consumption rate for one package field (e.g. 'cases_16' or
    'kegs_50l') from stocktake-to-stocktake count drops, per batch (a batch's
    production quantity at brew_date counts as the first data point). Only
    decreasing intervals count as consumption (an increase usually means a new
    batch's production landed, not a stock-take correction). Only intervals
    ending within the last `window_months` are included.
    Returns (monthly_rate_or_None, latest_known_qty_sum).
    """
    beer_batches = [b for b in batches if b.get("beer_name") == beer_name and not b.get("archived", False)]
    total_rate   = 0.0
    any_interval = False
    latest_qty_sum = 0.0
    cutoff = today - timedelta(days=int(window_months * 30.44))

    for b in beer_batches:
        points = []
        try:
            d0 = datetime.strptime(b.get("brew_date",""), "%Y-%m-%d").date()
            points.append((d0, b.get(field_name, 0)))
        except Exception:
            pass
        for s in stocktakes:
            if s.get("batch_id") != b["batch_id"]:
                continue
            try:
                d = datetime.strptime(s.get("stocktake_date",""), "%Y-%m-%d").date()
                points.append((d, s.get(field_name, 0)))
            except Exception:
                continue
        if not points:
            continue
        points.sort(key=lambda p: p[0])
        latest_qty_sum += points[-1][1]

        # Average this batch's own interval rates first (multiple intervals for one
        # batch describe the same ongoing depletion, not additional consumption),
        # then add the batch's average to the beer-wide total below.
        batch_interval_rates = []
        for i in range(len(points) - 1):
            d1, q1 = points[i]
            d2, q2 = points[i + 1]
            if d2 <= cutoff:
                continue  # this interval is entirely outside the lookback window
            days = (d2 - d1).days
            if days <= 0 or q1 <= q2:
                continue  # no elapsed time, or count went up (new production, not consumption)
            batch_interval_rates.append((q1 - q2) / days * 30.44)

        if batch_interval_rates:
            total_rate += sum(batch_interval_rates) / len(batch_interval_rates)
            any_interval = True

    if not any_interval:
        return None, latest_qty_sum
    return round(total_rate, 2), latest_qty_sum

def compute_package_summary_rows(beer_name, batches, stocktakes, window_months, weighted,
                                  stock_alert_months, ubd_alert_months, today, method="excise"):
    """
    Build one summary row per package size (case size / keg size) that this beer's
    batches have actually produced a positive quantity of — expressed in package
    units (cases / kegs) rather than litres.

    method="excise": sales rate comes from recorded Excise Return history. Litres
      remaining / avg-monthly-litres for the can pool (or keg pool) are allocated
      across sizes in proportion to each size's share of the last known baseline
      mix, so every case size shares the same "months remaining", and likewise
      for every keg size.
    method="soh": sales rate is inferred per package size directly from stocktake
      count drops over time — no allocation needed, since stocktakes already
      record each size separately. More responsive when Excise Return data is
      thin, but noisier (depends on how often stocktakes are done).

    Either way, if there's genuinely no usable data for a pool, Avg Monthly Qty
    and Months Remaining are left blank ("—") rather than shown as 0.
    """
    states = compute_soh_for_beer(beer_name, batches, stocktakes)
    if not states:
        return []

    can_size_l = states[-1]["can_size_l"] or 0.375
    ubd_months, ubd_date = months_to_earliest_use_by(beer_name, batches, today)
    ubd_alert = ubd_months is not None and ubd_months <= ubd_alert_months
    rows = []

    if method == "excise":
        total_soh_can = sum(s["soh_can_litres"] for s in states)
        total_soh_keg = sum(s["soh_keg_litres"] for s in states)
        baseline_can_total = sum(
            sum(s["soh_cases_by_size"].get(c, 0) * c * s["can_size_l"] for c in CASE_SIZES) for s in states
        )
        baseline_keg_total = sum(
            sum(s["soh_kegs_by_size"].get(k, 0) * k for k in KEG_SIZES) for s in states
        )
        ratio_can = (total_soh_can / baseline_can_total) if baseline_can_total > 0 else 0.0
        ratio_keg = (total_soh_keg / baseline_keg_total) if baseline_keg_total > 0 else 0.0

        have_can_data = excise_has_any_data(beer_name, "can")
        have_keg_data = excise_has_any_data(beer_name, "keg")
        avg_can_l, avg_keg_l = weighted_avg_monthly_sales_split(beer_name, window_months, weighted)
        if not have_can_data: avg_can_l = None
        if not have_keg_data: avg_keg_l = None
        months_can = round(total_soh_can / avg_can_l, 1) if avg_can_l else None
        months_keg = round(total_soh_keg / avg_keg_l, 1) if avg_keg_l else None

        for c in CASE_SIZES:
            baseline_qty = sum(s["soh_cases_by_size"].get(c, 0) for s in states)
            if baseline_qty <= 0:
                continue
            current_qty = round(baseline_qty * ratio_can, 1)
            if avg_can_l:
                size_litres_baseline = sum(s["soh_cases_by_size"].get(c, 0) * c * s["can_size_l"] for s in states)
                frac = (size_litres_baseline / baseline_can_total) if baseline_can_total > 0 else 0
                avg_qty_month = round((avg_can_l * frac) / (c * can_size_l), 1) if can_size_l > 0 else None
            else:
                avg_qty_month = None
            stock_alert = months_can is not None and months_can <= stock_alert_months
            rows.append(_pkg_row(beer_name, f"{c}-pack cases", current_qty, avg_qty_month,
                                  months_can, ubd_date, ubd_months, stock_alert, ubd_alert))

        for k in KEG_SIZES:
            baseline_qty = sum(s["soh_kegs_by_size"].get(k, 0) for s in states)
            if baseline_qty <= 0:
                continue
            current_qty = round(baseline_qty * ratio_keg, 1)
            if avg_keg_l:
                frac = (baseline_qty * k / baseline_keg_total) if baseline_keg_total > 0 else 0
                avg_qty_month = round((avg_keg_l * frac) / k, 1)
            else:
                avg_qty_month = None
            stock_alert = months_keg is not None and months_keg <= stock_alert_months
            rows.append(_pkg_row(beer_name, f"{k}L kegs", current_qty, avg_qty_month,
                                  months_keg, ubd_date, ubd_months, stock_alert, ubd_alert))

    else:  # method == "soh"
        for c in CASE_SIZES:
            baseline_qty = sum(s["soh_cases_by_size"].get(c, 0) for s in states)
            if baseline_qty <= 0:
                continue
            rate, latest_qty = soh_trend_monthly_rate(beer_name, batches, stocktakes, _case_field(c), window_months, today)
            months = round(latest_qty / rate, 1) if rate else None
            stock_alert = months is not None and months <= stock_alert_months
            rows.append(_pkg_row(beer_name, f"{c}-pack cases", round(latest_qty, 1), rate,
                                  months, ubd_date, ubd_months, stock_alert, ubd_alert))

        for k in KEG_SIZES:
            baseline_qty = sum(s["soh_kegs_by_size"].get(k, 0) for s in states)
            if baseline_qty <= 0:
                continue
            rate, latest_qty = soh_trend_monthly_rate(beer_name, batches, stocktakes, _keg_field(k), window_months, today)
            months = round(latest_qty / rate, 1) if rate else None
            stock_alert = months is not None and months <= stock_alert_months
            rows.append(_pkg_row(beer_name, f"{k}L kegs", round(latest_qty, 1), rate,
                                  months, ubd_date, ubd_months, stock_alert, ubd_alert))

    return rows

def _pkg_row(beer_name, package_label, qty_on_hand, avg_qty_month, months_remaining,
             ubd_date, ubd_months, stock_alert, ubd_alert):
    return {
        "Beer": beer_name, "Package": package_label,
        "Qty On Hand": qty_on_hand,
        "Avg Monthly Qty": avg_qty_month if avg_qty_month is not None else "—",
        "Months Remaining (forecast)": months_remaining if months_remaining is not None else "—",
        "Earliest Use-By": ubd_date.strftime("%Y-%m-%d") if ubd_date else "—",
        "Months to Use-By": ubd_months if ubd_months is not None else "—",
        "⚠️ Stock Low": "🔴" if stock_alert else "",
        "⚠️ Use-By Soon": "🔴" if ubd_alert else "",
    }

def months_to_earliest_use_by(beer_name, batches, today=None):
    """Months from today to the earliest use-by date among active, non-empty batches."""
    today = today or datetime.today().date()
    dates = []
    for b in batches:
        if b.get("beer_name") != beer_name or b.get("archived", False):
            continue
        ubd = b.get("use_by_date", "")
        if not ubd:
            continue
        try:
            d = datetime.strptime(ubd, "%Y-%m-%d").date()
            dates.append(d)
        except Exception:
            continue
    if not dates:
        return None, None
    earliest = min(dates)
    months = (earliest - today).days / 30.44
    return round(months, 1), earliest


# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# PAGE: PRICE LOOKUP  (owners view — price only, no cost/margin detail)
# ─────────────────────────────────────────────────────────────────────────────
if page == "📊 Price Lookup":
    st.title("📊 Price Lookup")
    st.caption("Select a beer, channel, and package to see the price.")

    beer_names = sorted(df_all["Beer"].unique()) if not df_all.empty else []
    pkgs_by_ch = {
        "Retail + Online": ["Can","4-Pack","Case","Keg"],
        "Wholesale":       ["Case","Keg"],
        "Tap Room":        ["Middy","Schooner","Pint","Jug"],
    }
    c1, c2, c3 = st.columns(3)
    sel_beer    = c1.selectbox("Beer Name",       beer_names or ["No beers loaded"])
    sel_channel = c2.selectbox("Sales Channel",   list(pkgs_by_ch.keys()))
    sel_pkg     = c3.selectbox("Package / Serve", pkgs_by_ch[sel_channel])

    if not df_all.empty and sel_beer in beer_names:
        row = df_all[(df_all["Beer"]==sel_beer)&(df_all["Channel"]==sel_channel)&(df_all["Package"]==sel_pkg)]
        if not row.empty:
            r = row.iloc[0]
            st.markdown("---")
            st.metric("Sell Price", f"${r['Sell Price ($)']:.2f}")
            st.markdown(
                f"**ABV:** {r['ABV']*100:.1f}%  |  **Package size:** {r['Package Size (L)']:.3f} L"
            )
        else:
            st.warning("No pricing found for this combination.")


# ─────────────────────────────────────────────────────────────────────────────
# PAGE: PRICE LISTS  (owners view — price only, no margin detail)
# ─────────────────────────────────────────────────────────────────────────────
elif page == "📋 Price Lists":
    st.title("📋 Price Lists")
    tab1, tab2, tab3 = st.tabs(["🛒 Retail + Online","🚚 Wholesale","🍺 Tap Room"])

    price_cols = ["Beer","ABV","Package","Sell Price ($)"]

    def price_table(df, channel, pkgs):
        sub = df[df["Channel"]==channel][price_cols]
        sub = sub[sub["Package"].isin(pkgs)]
        pp  = sub.pivot_table(index=["Beer","ABV"], columns="Package", values="Sell Price ($)", aggfunc="first")
        return pp.reindex(columns=[p for p in pkgs if p in pp.columns])

    with tab1:
        if not df_all.empty:
            pp = price_table(df_all,"Retail + Online",["Can","4-Pack","Case","Keg"])
            st.subheader("Sell Prices ($)"); st.dataframe(pp.style.format("${:.2f}"), use_container_width=True)
            st.download_button("⬇️ Download CSV", df_all[df_all["Channel"]=="Retail + Online"][price_cols].to_csv(index=False),"retail_prices.csv","text/csv")
    with tab2:
        if not df_all.empty:
            pp = price_table(df_all,"Wholesale",["Case","Keg"])
            st.subheader("Sell Prices ($)"); st.dataframe(pp.style.format("${:.2f}"), use_container_width=True)
            st.download_button("⬇️ Download CSV", df_all[df_all["Channel"]=="Wholesale"][price_cols].to_csv(index=False),"wholesale_prices.csv","text/csv")
    with tab3:
        if not df_all.empty:
            pp = price_table(df_all,"Tap Room",["Middy","Schooner","Pint","Jug"])
            st.subheader("Sell Prices ($)"); st.dataframe(pp.style.format("${:.2f}"), use_container_width=True)
            st.download_button("⬇️ Download CSV", df_all[df_all["Channel"]=="Tap Room"][price_cols].to_csv(index=False),"taproom_prices.csv","text/csv")

    st.markdown("---")
    if not df_all.empty:
        st.download_button("⬇️ Download All Pricing (CSV)", df_all[price_cols].to_csv(index=False),"all_pricing.csv","text/csv")


# ─────────────────────────────────────────────────────────────────────────────
# PAGE: BATCHES & STOCKTAKE
# ─────────────────────────────────────────────────────────────────────────────
elif page == "📦 Batches & Stocktake":
    st.title("📦 Batches & Stocktake")
    st.caption("Record each completed brew, and periodically true up stock levels with a physical count.")

    all_beer_names = sorted(set(b["name"] for b in st.session_state.beers))
    beer_can_size  = {b["name"]: b.get("can_size_l", 0.375) for b in st.session_state.beers}
    beer_abv       = {b["name"]: b.get("abv", 0.050) for b in st.session_state.beers}

    batches    = load_batches()
    stocktakes = load_stocktakes()

    # ── 1. Record a completed batch ──────────────────────────────────────────
    with st.expander("➕ Record a Completed Batch", expanded=not batches):
        # Beer picker lives outside the form so ABV / can size defaults below
        # (and the batch dropdown further down) update immediately on change.
        b_beer = st.selectbox("Beer", all_beer_names or ["No beers configured"], key="nb_beer_select")

        with st.form("add_batch_form"):
            c1, c2, c3 = st.columns(3)
            c1.markdown(f"**Beer:** {b_beer}")
            b_number = c2.text_input("Batch Number", placeholder="e.g. B-2026-014")
            b_abv    = c3.number_input(
                "Actual ABV", value=float(beer_abv.get(b_beer, 0.050)),
                min_value=0.0, max_value=0.20, step=0.001, format="%.3f",
                key=f"nb_abv_{b_beer}",
                help="Defaults to this beer's ABV from Beer Inputs — adjust if this batch tested differently."
            )

            c1, c2 = st.columns(2)
            b_brew_date   = c1.date_input("Brew / Completion Date", value=datetime.today())
            b_use_by_date = c2.date_input("Use By Date", value=datetime.today())

            st.markdown("**Cans**")
            b_can_size = st.number_input(
                "Can Size (L)", value=float(beer_can_size.get(b_beer, 0.375)),
                step=0.005, format="%.3f", key=f"nb_cansize_{b_beer}"
            )

            st.markdown("**Cases (by case size)**")
            case_cols = st.columns(len(CASE_SIZES))
            b_cases = {}
            for col, csize in zip(case_cols, CASE_SIZES):
                b_cases[csize] = col.number_input(f"{csize}-pack cases", value=0, min_value=0, step=1, key=f"nb_case_{csize}")

            st.markdown("**Kegs (by size)**")
            keg_cols = st.columns(len(KEG_SIZES))
            b_kegs = {}
            for col, size in zip(keg_cols, KEG_SIZES):
                b_kegs[size] = col.number_input(f"{size}L", value=0, min_value=0, step=1, key=f"nb_keg_{size}")

            b_notes = st.text_area("Notes (optional)", placeholder="e.g. short pour, extra keg from top-up brew")

            if st.form_submit_button("➕ Add Batch", type="primary"):
                if not b_number.strip():
                    st.error("Please enter a batch number.")
                else:
                    new_batch = {
                        "batch_id": f"{b_beer}__{b_number.strip()}",
                        "beer_name": b_beer,
                        "batch_number": b_number.strip(),
                        "actual_abv": b_abv,
                        "brew_date": b_brew_date.strftime("%Y-%m-%d"),
                        "use_by_date": b_use_by_date.strftime("%Y-%m-%d"),
                        "can_size_l": b_can_size,
                        "notes": b_notes,
                        "archived": False,
                    }
                    for size in KEG_SIZES:
                        new_batch[_keg_field(size)] = b_kegs[size]
                    for csize in CASE_SIZES:
                        new_batch[_case_field(csize)] = b_cases[csize]
                    batches.append(new_batch)
                    result = save_batches(batches)
                    if result is True:
                        st.success(f"Added batch **{b_number}** for **{b_beer}**.")
                        st.rerun()
                    else:
                        st.error(f"Save issue: {result}")

    st.markdown("---")

    # ── 2. Existing batches, grouped by beer ─────────────────────────────────
    st.subheader("Batches on Record")
    active_batches_view = [b for b in batches if not b.get("archived", False)]
    if not active_batches_view:
        st.info("No batches recorded yet.")
    else:
        for beer_name in sorted(set(b["beer_name"] for b in active_batches_view)):
            beer_batches = [b for b in active_batches_view if b["beer_name"] == beer_name]
            soh_states = compute_soh_for_beer(beer_name, batches, stocktakes)
            soh_by_id  = {s["batch_id"]: s for s in soh_states}
            st.markdown(f"**{beer_name}**")
            for b in sorted(beer_batches, key=lambda x: x.get("brew_date", "")):
                s = soh_by_id.get(b["batch_id"], {})
                label = (
                    f"{b['batch_number']}  |  Brewed {b.get('brew_date','—')}  |  "
                    f"Use by {b.get('use_by_date','—')}  |  "
                    f"Est. SOH: {s.get('soh_cans_est',0):.0f} cans + {s.get('soh_keg_litres',0):.0f}L kegs"
                )
                with st.expander(label):
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Produced (cans)", int(batch_total_cans(b)))
                    c2.metric("Produced (keg L)", f"{batch_produced_keg_litres(b):.0f} L")
                    c3.metric("Actual ABV", f"{b.get('actual_abv',0)*100:.1f}%")
                    case_summary = ", ".join(f"{b.get(_case_field(c2s),0)}× {c2s}-pack" for c2s in CASE_SIZES if b.get(_case_field(c2s), 0))
                    keg_summary = ", ".join(f"{b.get(_keg_field(s2),0)}× {s2}L" for s2 in KEG_SIZES if b.get(_keg_field(s2), 0))
                    st.caption(f"Cases produced: {case_summary or 'none'}  |  Kegs produced: {keg_summary or 'none'}")
                    if s.get("last_stocktake_date"):
                        st.caption(f"Last stocktake: {s['last_stocktake_date']}")
                    else:
                        st.caption("No stocktake recorded yet — SOH is a full FIFO estimate from production.")
                    if b.get("notes"):
                        st.caption(f"Notes: {b['notes']}")

                    st.markdown("---")
                    st.markdown("**✏️ Edit this batch**")
                    bid = b["batch_id"]
                    with st.form(f"edit_batch_{bid}"):
                        e1, e2, e3 = st.columns(3)
                        edit_beer = e1.selectbox(
                            "Beer", all_beer_names,
                            index=all_beer_names.index(b["beer_name"]) if b["beer_name"] in all_beer_names else 0,
                            key=f"eb_beer_{bid}"
                        )
                        edit_number = e2.text_input("Batch Number", value=b.get("batch_number",""), key=f"eb_num_{bid}")
                        edit_abv = e3.number_input(
                            "Actual ABV", value=float(b.get("actual_abv", 0.05)),
                            min_value=0.0, max_value=0.20, step=0.001, format="%.3f", key=f"eb_abv_{bid}"
                        )
                        e1, e2 = st.columns(2)
                        try:
                            brew_default = datetime.strptime(b.get("brew_date",""), "%Y-%m-%d")
                        except Exception:
                            brew_default = datetime.today()
                        try:
                            useby_default = datetime.strptime(b.get("use_by_date",""), "%Y-%m-%d")
                        except Exception:
                            useby_default = datetime.today()
                        edit_brew_date   = e1.date_input("Brew / Completion Date", value=brew_default, key=f"eb_brew_{bid}")
                        edit_use_by_date = e2.date_input("Use By Date",             value=useby_default, key=f"eb_useby_{bid}")

                        edit_can_size = st.number_input(
                            "Can Size (L)", value=float(b.get("can_size_l", 0.375)),
                            step=0.005, format="%.3f", key=f"eb_cansize_{bid}"
                        )
                        edit_case_cols = st.columns(len(CASE_SIZES))
                        edit_cases = {}
                        for col, csize in zip(edit_case_cols, CASE_SIZES):
                            edit_cases[csize] = col.number_input(
                                f"{csize}-pack cases", value=int(b.get(_case_field(csize), 0)),
                                min_value=0, step=1, key=f"eb_case_{csize}_{bid}"
                            )
                        edit_keg_cols = st.columns(len(KEG_SIZES))
                        edit_kegs = {}
                        for col, size in zip(edit_keg_cols, KEG_SIZES):
                            edit_kegs[size] = col.number_input(
                                f"{size}L", value=int(b.get(_keg_field(size), 0)),
                                min_value=0, step=1, key=f"eb_keg_{size}_{bid}"
                            )
                        edit_notes = st.text_area("Notes", value=b.get("notes",""), key=f"eb_notes_{bid}")

                        if st.form_submit_button("💾 Save Changes"):
                            if not edit_number.strip():
                                st.error("Batch number can't be blank.")
                            else:
                                new_bid = f"{edit_beer}__{edit_number.strip()}"
                                for bb in batches:
                                    if bb["batch_id"] == bid:
                                        bb["batch_id"]     = new_bid
                                        bb["beer_name"]    = edit_beer
                                        bb["batch_number"] = edit_number.strip()
                                        bb["actual_abv"]   = edit_abv
                                        bb["brew_date"]    = edit_brew_date.strftime("%Y-%m-%d")
                                        bb["use_by_date"]  = edit_use_by_date.strftime("%Y-%m-%d")
                                        bb["can_size_l"]   = edit_can_size
                                        bb["notes"]        = edit_notes
                                        for csize in CASE_SIZES:
                                            bb[_case_field(csize)] = edit_cases[csize]
                                        for size in KEG_SIZES:
                                            bb[_keg_field(size)] = edit_kegs[size]
                                if new_bid != bid:
                                    # keep linked stocktakes pointing at the right batch
                                    for st_rec in stocktakes:
                                        if st_rec.get("batch_id") == bid:
                                            st_rec["batch_id"] = new_bid
                                    save_stocktakes(stocktakes)
                                result = save_batches(batches)
                                if result is True:
                                    st.success("Batch updated.")
                                    st.rerun()
                                else:
                                    st.error(f"Save issue: {result}")

                    c1, c2 = st.columns(2)
                    if c1.button("🗄️ Archive batch (no longer in stock)", key=f"arch_{bid}"):
                        for bb in batches:
                            if bb["batch_id"] == bid:
                                bb["archived"] = True
                        save_batches(batches)
                        st.rerun()
                    if c2.button("🗑️ Delete batch permanently", key=f"delbtn_{bid}"):
                        st.session_state[f"confirm_delbatch_{bid}"] = True
                        st.rerun()
                    if st.session_state.get(f"confirm_delbatch_{bid}"):
                        confirmed_b = st.checkbox(
                            f"Yes, permanently delete batch **{b['batch_number']}** and any stocktakes recorded against it — this cannot be undone",
                            key=f"chkdel_{bid}"
                        )
                        if confirmed_b:
                            batches[:]    = [bb for bb in batches if bb["batch_id"] != bid]
                            stocktakes[:] = [s for s in stocktakes if s.get("batch_id") != bid]
                            save_batches(batches)
                            save_stocktakes(stocktakes)
                            st.session_state.pop(f"confirm_delbatch_{bid}", None)
                            st.success("Batch deleted.")
                            st.rerun()

    st.markdown("---")

    # ── 3. Record a stocktake ─────────────────────────────────────────────────
    st.subheader("📋 Record a Stocktake")
    st.caption("Enter what was actually counted for a specific batch. This overwrites the calculated SOH from that date forward.")

    active_beers_with_batches = sorted(set(b["beer_name"] for b in active_batches_view))
    if not active_beers_with_batches:
        st.info("Add a batch above before recording a stocktake.")
    else:
        # Beer + Batch pickers live outside the form so choosing a different
        # beer immediately narrows the batch list, and choosing a different
        # batch immediately refreshes the pre-filled counts below.
        c1, c2 = st.columns(2)
        st_beer = c1.selectbox("Beer", active_beers_with_batches, key="st_beer_select")
        beer_batch_options = [b for b in active_batches_view if b["beer_name"] == st_beer]
        st_batch = c2.selectbox(
            "Batch",
            beer_batch_options,
            format_func=lambda b: f"{b['batch_number']} (brewed {b.get('brew_date','—')})",
            key=f"st_batch_select_{st_beer}"
        )

        # Look up the most recently recorded counts for this batch (last stocktake,
        # or the produced quantities if no stocktake has been done yet).
        soh_states  = compute_soh_for_beer(st_beer, active_batches_view, stocktakes)
        soh_by_id   = {s2["batch_id"]: s2 for s2 in soh_states}
        batch_state = soh_by_id.get(st_batch["batch_id"], {})
        last_date   = batch_state.get("last_stocktake_date")
        if last_date:
            st.caption(f"📅 Values below are pre-filled from the last stocktake, recorded **{last_date}**.")
        else:
            st.caption(f"📅 No stocktake recorded yet for this batch — values below are pre-filled from production, brewed **{st_batch.get('brew_date','—')}**.")

        default_cases = batch_state.get("soh_cases_by_size", {c: st_batch.get(_case_field(c), 0) for c in CASE_SIZES})
        default_kegs  = batch_state.get("soh_kegs_by_size",  {s: st_batch.get(_keg_field(s), 0) for s in KEG_SIZES})

        with st.form("stocktake_form"):
            st_date = st.date_input("Stocktake Date", value=datetime.today())

            st.markdown("**Counted Cases (by case size)**")
            st_case_cols = st.columns(len(CASE_SIZES))
            st_cases = {}
            for col, csize in zip(st_case_cols, CASE_SIZES):
                st_cases[csize] = col.number_input(
                    f"{csize}-pack cases", value=int(default_cases.get(csize, 0)),
                    min_value=0, step=1, key=f"st_case_{csize}_{st_batch['batch_id']}"
                )

            st.markdown("**Counted Kegs (by size)**")
            keg_cols2 = st.columns(len(KEG_SIZES))
            st_kegs = {}
            for col, size in zip(keg_cols2, KEG_SIZES):
                st_kegs[size] = col.number_input(
                    f"{size}L ", value=int(default_kegs.get(size, 0)),
                    min_value=0, step=1, key=f"st_keg_{size}_{st_batch['batch_id']}"
                )

            st_notes = st.text_area("Notes (optional)", key=f"st_notes_{st_batch['batch_id']}")

            if st.form_submit_button("💾 Save Stocktake", type="primary"):
                new_st = {
                    "stocktake_id": f"{st_batch['batch_id']}__{st_date.strftime('%Y-%m-%d')}__{uuid.uuid4().hex[:8]}",
                    "beer_name": st_beer,
                    "batch_id": st_batch["batch_id"],
                    "stocktake_date": st_date.strftime("%Y-%m-%d"),
                    "notes": st_notes,
                }
                for size in KEG_SIZES:
                    new_st[_keg_field(size)] = st_kegs[size]
                for csize in CASE_SIZES:
                    new_st[_case_field(csize)] = st_cases[csize]
                stocktakes.append(new_st)
                result = save_stocktakes(stocktakes)
                if result is True:
                    st.success(f"Stocktake recorded for {st_beer} — {st_batch['batch_number']}.")
                    st.rerun()
                else:
                    st.error(f"Save issue: {result}")

    st.markdown("---")
    if stocktakes:
        st.subheader("Stocktake History")
        want_cols = (["stocktake_date", "beer_name", "batch_id"] +
                     [_case_field(c) for c in CASE_SIZES] +
                     [_keg_field(s) for s in KEG_SIZES] + ["notes"])
        st_df = pd.DataFrame(stocktakes)
        for col in want_cols:
            if col not in st_df.columns:
                st_df[col] = 0
        st_df = st_df[want_cols]
        st.dataframe(st_df.sort_values("stocktake_date", ascending=False), hide_index=True, use_container_width=True)

        st.markdown("**✏️ Edit or delete a stocktake entry**")
        batch_num_by_id = {b["batch_id"]: b["batch_number"] for b in batches}
        for idx, srec in enumerate(sorted(stocktakes, key=lambda x: x.get("stocktake_date",""), reverse=True)):
            # idx makes widget keys collision-proof even if two records somehow
            # share a stocktake_id (e.g. from data saved before this was fixed).
            uid = f"{srec.get('stocktake_id','notake')}_{idx}"
            batch_label = batch_num_by_id.get(srec.get("batch_id"), srec.get("batch_id","—"))
            with st.expander(f"{srec.get('stocktake_date','—')}  |  {srec.get('beer_name','—')}  |  {batch_label}"):
                with st.form(f"edit_stocktake_{uid}"):
                    e1, e2 = st.columns(2)
                    try:
                        st_date_default = datetime.strptime(srec.get("stocktake_date",""), "%Y-%m-%d")
                    except Exception:
                        st_date_default = datetime.today()
                    edit_st_date = e1.date_input("Stocktake Date", value=st_date_default, key=f"est_date_{uid}")
                    e2.text_input("Batch", value=batch_label, disabled=True, key=f"est_batch_{uid}")

                    st.markdown("**Counted Cases**")
                    ecase_cols = st.columns(len(CASE_SIZES))
                    edit_st_cases = {}
                    for col, csize in zip(ecase_cols, CASE_SIZES):
                        edit_st_cases[csize] = col.number_input(
                            f"{csize}-pack cases", value=int(srec.get(_case_field(csize), 0)),
                            min_value=0, step=1, key=f"est_case_{csize}_{uid}"
                        )
                    st.markdown("**Counted Kegs**")
                    ekeg_cols = st.columns(len(KEG_SIZES))
                    edit_st_kegs = {}
                    for col, size in zip(ekeg_cols, KEG_SIZES):
                        edit_st_kegs[size] = col.number_input(
                            f"{size}L", value=int(srec.get(_keg_field(size), 0)),
                            min_value=0, step=1, key=f"est_keg_{size}_{uid}"
                        )
                    edit_st_notes = st.text_area("Notes", value=srec.get("notes",""), key=f"est_notes_{uid}")

                    if st.form_submit_button("💾 Save Changes"):
                        for s2 in stocktakes:
                            if s2 is srec:  # match this exact record, not just a (possibly duplicated) id
                                s2["stocktake_date"] = edit_st_date.strftime("%Y-%m-%d")
                                s2["notes"] = edit_st_notes
                                for csize in CASE_SIZES:
                                    s2[_case_field(csize)] = edit_st_cases[csize]
                                for size in KEG_SIZES:
                                    s2[_keg_field(size)] = edit_st_kegs[size]
                        result = save_stocktakes(stocktakes)
                        if result is True:
                            st.success("Stocktake updated.")
                            st.rerun()
                        else:
                            st.error(f"Save issue: {result}")

                if st.button("🗑️ Delete this stocktake", key=f"delst_{uid}"):
                    st.session_state[f"confirm_delst_{uid}"] = True
                    st.rerun()
                if st.session_state.get(f"confirm_delst_{uid}"):
                    confirmed_s = st.checkbox(
                        "Yes, permanently delete this stocktake entry — this cannot be undone",
                        key=f"chkdelst_{uid}"
                    )
                    if confirmed_s:
                        stocktakes[:] = [s for s in stocktakes if s is not srec]
                        save_stocktakes(stocktakes)
                        st.session_state.pop(f"confirm_delst_{uid}", None)
                        st.success("Stocktake deleted.")
                        st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# PAGE: STOCK FORECAST
# ─────────────────────────────────────────────────────────────────────────────
elif page == "📈 Stock Forecast":
    st.title("📈 Stock Forecast")
    st.caption(
        "Estimated stock on hand (SOH) and projected runout, based on recorded batches, "
        "stocktakes, and historical excise sales. "
        "Alerts fire when ≤ 2 months of forecast stock remains, or ≤ 3 months to use-by date."
    )

    batches    = load_batches()
    stocktakes = load_stocktakes()
    active_batches = [b for b in batches if not b.get("archived", False)]
    active_beer_names = sorted(set(b["beer_name"] for b in active_batches))

    if not active_batches:
        st.info("No active batches found. Record batches in 📦 Batches & Stocktake first.")
        st.stop()

    # ── Forecast settings ─────────────────────────────────────────────────────
    with st.expander("⚙️ Forecast Settings", expanded=False):
        rate_method_label = st.radio(
            "Sales rate calculation method",
            ["Excise Return (recorded sales)", "Stocktake Trend (SOH-based)"],
            index=0, key="fc_method", horizontal=True,
            help=(
                "Excise Return: uses actual sales volumes saved in 🧾 Excise Return. "
                "Stocktake Trend: infers a sales rate from how much stock counts have "
                "dropped between stocktakes — useful when Excise Return data is thin, "
                "but noisier and depends on how often stocktakes are done."
            )
        )
        rate_method = "excise" if rate_method_label.startswith("Excise") else "soh"

        fc1, fc2, fc3 = st.columns(3)
        forecast_window = fc1.number_input(
            "Months of history to use", value=3, min_value=1, max_value=12, step=1,
            key="fc_window",
            help="How many recent months to average for the forecast (excise periods, or stocktake intervals)"
        )
        use_weighted = fc1.checkbox(
            "Weighted average (recent months count more)", value=True, key="fc_weighted",
            help="Excise Return method only — recent months get higher weight"
        )
        stock_alert_months = fc2.number_input(
            "Stock alert threshold (months)", value=2.0, min_value=0.5, max_value=6.0,
            step=0.5, key="fc_alert_months",
            help="Show a warning when forecast months remaining ≤ this value"
        )
        ubd_alert_months = fc3.number_input(
            "Use-by alert threshold (months)", value=3.0, min_value=0.5, max_value=6.0,
            step=0.5, key="fc_ubd_months",
            help="Show a warning when use-by date is within this many months"
        )

    st.markdown("---")

    # ── Build forecast for every active beer ─────────────────────────────────
    today = datetime.today().date()
    alert_rows   = []
    summary_rows = []

    for beer_name in active_beer_names:
        pkg_rows = compute_package_summary_rows(
            beer_name, active_batches, stocktakes,
            int(forecast_window), use_weighted, stock_alert_months, ubd_alert_months, today,
            method=rate_method
        )
        if not pkg_rows:
            continue
        summary_rows.extend(pkg_rows)

        low_pkgs = [r["Package"] for r in pkg_rows if r["⚠️ Stock Low"]]
        ubd_flag = any(r["⚠️ Use-By Soon"] for r in pkg_rows)
        if low_pkgs or ubd_flag:
            reasons = []
            if low_pkgs:
                # months remaining is the same across all sizes sharing a pool (can/keg),
                # so just report the low-stock line once per pool affected
                seen_months = {r["Package"]: r["Months Remaining (forecast)"] for r in pkg_rows if r["⚠️ Stock Low"]}
                for pkg, months in seen_months.items():
                    reasons.append(f"{pkg}: only ~{months} months of forecast stock left")
            if ubd_flag:
                ubd_row = next(r for r in pkg_rows if r["⚠️ Use-By Soon"])
                reasons.append(f"earliest use-by is in ~{ubd_row['Months to Use-By']} months ({ubd_row['Earliest Use-By']})")
            alert_rows.append(f"**{beer_name}** — " + "; ".join(reasons))

    if alert_rows:
        st.warning("### 🚨 Alerts\n\n" + "\n\n".join(alert_rows))
    else:
        st.success("No stock or use-by alerts at current thresholds.")

    st.markdown("---")
    st.subheader("Summary — All Beers, by Package Size")
    if rate_method == "excise":
        st.caption(
            "**Method: Excise Return.** Only package sizes with a positive quantity recorded in at least one "
            "batch are shown. Months remaining is shared across all case sizes (from the can pool) and across "
            "all keg sizes (from the keg pool), since Excise Return data doesn't distinguish which size was sold. "
            "Blank (—) means no Excise Return sales have been recorded for that pool."
        )
    else:
        st.caption(
            "**Method: Stocktake Trend.** Sales rate is inferred per package size from stock count drops between "
            "stocktakes (or from production if only one stocktake exists). Blank (—) means there aren't yet two "
            "data points to infer a trend from — record another stocktake to populate it."
        )
    if summary_rows:
        st.dataframe(pd.DataFrame(summary_rows), hide_index=True, use_container_width=True)

    st.markdown("---")
    st.subheader("Batch-Level Detail")
    sel_forecast_beer = st.selectbox("View batch breakdown for", active_beer_names)
    detail_states = compute_soh_for_beer(sel_forecast_beer, active_batches, stocktakes)
    if detail_states:
        detail_rows = [{
            "Batch": s["batch_number"],
            "Brewed": s["brew_date"],
            "Use By": s["use_by_date"],
            "Last Stocktake": s["last_stocktake_date"] or "—",
            "Est. Cans Left": s["soh_cans_est"],
            "Est. Keg Litres Left": s["soh_keg_litres"],
            "Est. Total Litres Left": s["soh_total_litres"],
        } for s in detail_states]
        st.dataframe(pd.DataFrame(detail_rows), hide_index=True, use_container_width=True)

    st.caption(
        "Forecast method: monthly sales volumes come from saved Excise Return history. "
        "SOH is depleted FIFO (oldest batch first), separately for cans and kegs. "
        "A stocktake resets the baseline for a batch; only sales dated after the most "
        "recent stocktake are deducted on top of it."
    )
