"""
Brewery Pricing & Forecast Tool
"""

import math, json, os, re
from datetime import datetime
from difflib import SequenceMatcher
import streamlit as st
import pandas as pd

st.set_page_config(
    page_title="Brewery Pricing Tool",
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
    st.markdown("## 🍺 Brewery Pricing Tool")
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
st.sidebar.title("🍺 Brewery Pricing Tool")
page = st.sidebar.radio("Navigate", [
    "📊 Price Lookup", "📋 Price Lists", "⚙️ General Inputs",
    "🍺 Beer Inputs", "🔍 What-If Analysis", "📜 Price History",
    "🧾 Excise Return", "📦 Batches & Stocktake", "📈 Stock Forecast",
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

KEG_SIZES = [10, 19.5, 20, 30, 50]  # standard keg sizes in litres

def _keg_field(size_l):
    """Field name for a keg size, e.g. 10 -> 'kegs_10l', 19.5 -> 'kegs_19_5l'."""
    s = str(size_l).replace(".", "_")
    return f"kegs_{s}l"

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

def batch_produced_can_litres(batch):
    return batch.get("cans_produced", 0) * batch.get("can_size_l", 0.375)

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
            "soh_kegs_by_size": {s: b.get(_keg_field(s), 0) for s in KEG_SIZES},
            "last_stocktake_date": None,
            "baseline_date": b.get("brew_date", ""),  # sales after this date get deducted
        }
        states.append(state)

    # Apply most recent stocktake per batch as a new baseline
    for state in states:
        matching = [s for s in beer_stocktakes if s.get("batch_id") == state["batch_id"]]
        if matching:
            latest = sorted(matching, key=lambda s: s.get("stocktake_date", ""))[-1]
            state["soh_can_litres"] = latest.get("soh_cans", 0) * state["can_size_l"]
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
# PAGE: PRICE LOOKUP
# ─────────────────────────────────────────────────────────────────────────────
if page == "📊 Price Lookup":
    st.title("📊 Price Lookup")
    st.caption("Select a beer, channel, and package to see full pricing detail.")

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
            c1,c2,c3,c4 = st.columns(4)
            c1.metric("Sell Price", f"${r['Sell Price ($)']:.2f}")
            c2.metric("Cost",       f"${r['Cost ($)']:.4f}")
            c3.metric("Margin $",   f"${r['Margin ($)']:.4f}")
            c4.metric("Margin %",   f"{r['Margin %']:.1f}%")

            st.markdown("##### Cost Breakdown")
            cols_order = ["Excise ($)","Fixed Cost ($)","R&E ($)","Packaging ($)","Raw Materials ($)","Cost ($)"]
            labels     = ["Excise","Fixed Costs","Return & Earn","Packaging","Raw Materials","Total Cost"]
            bd = pd.DataFrame({
                "Component":     labels,
                "Amount ($)":    [r[c] for c in cols_order],
                "% of Sell Price": [r[c]/r["Sell Price ($)"]*100 if r["Sell Price ($)"] else 0 for c in cols_order],
            })
            bd["Amount ($)"]      = bd["Amount ($)"].apply(lambda x: f"${x:.4f}")
            bd["% of Sell Price"] = bd["% of Sell Price"].apply(lambda x: f"{x:.1f}%")
            st.dataframe(bd, hide_index=True, use_container_width=True)

            if r["Royalty ($)"] > 0:
                st.info(f"Royalty: ${r['Royalty ($)']:.4f} per package (included in sell price)")
            st.markdown(
                f"**ABV:** {r['ABV']*100:.1f}%  |  **Package size:** {r['Package Size (L)']:.3f} L  "
                f"|  **Excise category:** {abv_class(r['ABV'])}  "
                f"|  **Excise period:** {st.session_state.gi.get('active_excise_period','')}"
            )
        else:
            st.warning("No pricing found for this combination.")


# ─────────────────────────────────────────────────────────────────────────────
# PAGE: PRICE LISTS
# ─────────────────────────────────────────────────────────────────────────────
elif page == "📋 Price Lists":
    st.title("📋 Price Lists")
    tab1, tab2, tab3 = st.tabs(["🛒 Retail + Online","🚚 Wholesale","🍺 Tap Room"])

    def price_table(df, channel, pkgs):
        sub = df[df["Channel"]==channel][["Beer","ABV","Package","Margin %","Sell Price ($)"]]
        sub = sub[sub["Package"].isin(pkgs)]
        pp  = sub.pivot_table(index=["Beer","ABV"], columns="Package", values="Sell Price ($)", aggfunc="first")
        pm  = sub.pivot_table(index=["Beer","ABV"], columns="Package", values="Margin %",      aggfunc="first")
        return (pp.reindex(columns=[p for p in pkgs if p in pp.columns]),
                pm.reindex(columns=[p for p in pkgs if p in pm.columns]))

    with tab1:
        if not df_all.empty:
            pp, pm = price_table(df_all,"Retail + Online",["Can","4-Pack","Case","Keg"])
            st.subheader("Sell Prices ($)"); st.dataframe(pp.style.format("${:.2f}"), use_container_width=True)
            st.subheader("Margin %");        st.dataframe(pm.style.format("{:.1f}%"), use_container_width=True)
            st.download_button("⬇️ Download CSV", df_all[df_all["Channel"]=="Retail + Online"].to_csv(index=False),"retail_prices.csv","text/csv")
    with tab2:
        if not df_all.empty:
            pp, pm = price_table(df_all,"Wholesale",["Case","Keg"])
            st.subheader("Sell Prices ($)"); st.dataframe(pp.style.format("${:.2f}"), use_container_width=True)
            st.subheader("Margin %");        st.dataframe(pm.style.format("{:.1f}%"), use_container_width=True)
            st.download_button("⬇️ Download CSV", df_all[df_all["Channel"]=="Wholesale"].to_csv(index=False),"wholesale_prices.csv","text/csv")
    with tab3:
        if not df_all.empty:
            pp, pm = price_table(df_all,"Tap Room",["Middy","Schooner","Pint","Jug"])
            st.subheader("Sell Prices ($)"); st.dataframe(pp.style.format("${:.2f}"), use_container_width=True)
            st.subheader("Margin %");        st.dataframe(pm.style.format("{:.1f}%"), use_container_width=True)
            st.download_button("⬇️ Download CSV", df_all[df_all["Channel"]=="Tap Room"].to_csv(index=False),"taproom_prices.csv","text/csv")

    st.markdown("---")
    if not df_all.empty:
        st.download_button("⬇️ Download All Pricing (CSV)", df_all.to_csv(index=False),"all_pricing.csv","text/csv")


# ─────────────────────────────────────────────────────────────────────────────
# PAGE: GENERAL INPUTS
# ─────────────────────────────────────────────────────────────────────────────
elif page == "⚙️ General Inputs":
    st.title("⚙️ General Inputs")
    gi = st.session_state.gi

    # ── Excise Rates ──────────────────────────────────────────────────────────
    st.subheader("🏛 Australian Beer Excise Rates ($/L of pure alcohol)")
    st.caption(
        "Rates are updated by the ATO every 6 months, typically in February and August. "
        "Check the latest rates at [ato.gov.au](https://www.ato.gov.au/businesses-and-organisations/"
        "income-deductions-and-concessions/excise-duty/excise-duty-rates) and enter them below. "
        "Note: the ATO website blocks automated access so rates must be entered manually."
    )

    if "excise_periods" not in gi:
        gi["excise_periods"] = {k: dict(v) for k, v in DEFAULT_EXCISE_PERIODS.items()}
    periods      = gi["excise_periods"]
    period_dates = sorted(periods.keys())

    active = gi.get("active_excise_period", period_dates[-1] if period_dates else "")
    if active not in period_dates and period_dates:
        active = period_dates[-1]

    # ── Period selector + add/delete ─────────────────────────────────────────
    col_sel, col_add, col_del = st.columns([3,1,1])
    with col_sel:
        selected_date = st.selectbox("View / Edit Period", period_dates,
                                     index=period_dates.index(active) if active in period_dates else 0,
                                     key="excise_date_select")
    with col_add:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("➕ Add Period", use_container_width=True):
            st.session_state["adding_excise"] = True
    with col_del:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🗑️ Delete", disabled=len(period_dates)<=1, use_container_width=True,
                     help="Cannot delete the only remaining period"):
            st.session_state["confirm_del_period"] = selected_date
            st.rerun()

    if st.session_state.get("confirm_del_period") == selected_date:
        st.warning(f"⚠️ You are about to delete the excise period **{selected_date}**. Tick to confirm:")
        confirmed_ep = st.checkbox(f"Yes, delete period {selected_date} — this cannot be undone", key="chk_ep")
        if st.button("Cancel delete", key="cancel_ep"):
            st.session_state.pop("confirm_del_period", None)
            st.rerun()
        if confirmed_ep:
            del periods[selected_date]
            gi["excise_periods"]       = periods
            gi["active_excise_period"] = sorted(periods.keys())[-1]
            gi["excise_rates"]         = periods[gi["active_excise_period"]]
            st.session_state.gi = gi
            st.session_state.pop("confirm_del_period", None)
            compute_all.clear()
            st.rerun()

    # ── Add new period form ───────────────────────────────────────────────────
    if st.session_state.get("adding_excise"):
        with st.form("add_excise_form"):
            st.markdown("**Enter details for the new excise period**")
            new_label = st.text_input("Effective Date Label", placeholder="e.g. 4 Aug 2026")
            latest = periods[sorted(periods.keys())[-1]] if periods else {}
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("*Cans ($/L pure alcohol)*")
                nc_lt3 = st.number_input("Can <3%",    value=float(latest.get(("Can","<3%"),52.87)),           step=0.01,format="%.2f",key="np_c_lt3")
                nc_mid = st.number_input("Can 3–3.5%", value=float(latest.get(("Can","3%<=ABV<=3.5%"),61.57)), step=0.01,format="%.2f",key="np_c_mid")
                nc_gt  = st.number_input("Can >3.5%",  value=float(latest.get(("Can","ABV>3.5%"),61.57)),      step=0.01,format="%.2f",key="np_c_gt")
            with c2:
                st.markdown("*Kegs ($/L pure alcohol)*")
                nk_lt3 = st.number_input("Keg <3%",    value=float(latest.get(("Keg","<3%"),10.57)),           step=0.01,format="%.2f",key="np_k_lt3")
                nk_mid = st.number_input("Keg 3–3.5%", value=float(latest.get(("Keg","3%<=ABV<=3.5%"),33.11)), step=0.01,format="%.2f",key="np_k_mid")
                nk_gt  = st.number_input("Keg >3.5%",  value=float(latest.get(("Keg","ABV>3.5%"),43.39)),      step=0.01,format="%.2f",key="np_k_gt")
            sb1, sb2 = st.columns(2)
            if sb1.form_submit_button("✅ Add", type="primary"):
                if new_label.strip():
                    periods[new_label.strip()] = {
                        ("Can","<3%"): nc_lt3, ("Can","3%<=ABV<=3.5%"): nc_mid, ("Can","ABV>3.5%"): nc_gt,
                        ("Keg","<3%"): nk_lt3, ("Keg","3%<=ABV<=3.5%"): nk_mid, ("Keg","ABV>3.5%"): nk_gt,
                    }
                    gi["excise_periods"] = periods
                    st.session_state["adding_excise"] = False
                    compute_all.clear()
                    st.rerun()
                else:
                    st.error("Please enter a date label.")
            if sb2.form_submit_button("Cancel"):
                st.session_state["adding_excise"] = False
                st.rerun()

    # ── Edit selected period's rates ──────────────────────────────────────────
    st.markdown(f"**Rates for period: {selected_date}**")
    rates = periods.get(selected_date, {})
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("*Cans ($/L pure alcohol)*")
        rates[("Can","<3%")]           = st.number_input("Can <3% ABV",    value=float(rates.get(("Can","<3%"),52.87)),           step=0.01,format="%.2f",key=f"r_c_lt3_{selected_date}")
        rates[("Can","3%<=ABV<=3.5%")] = st.number_input("Can 3–3.5% ABV", value=float(rates.get(("Can","3%<=ABV<=3.5%"),61.57)), step=0.01,format="%.2f",key=f"r_c_mid_{selected_date}")
        rates[("Can","ABV>3.5%")]      = st.number_input("Can >3.5% ABV",  value=float(rates.get(("Can","ABV>3.5%"),61.57)),      step=0.01,format="%.2f",key=f"r_c_gt_{selected_date}")
    with c2:
        st.markdown("*Kegs ($/L pure alcohol)*")
        rates[("Keg","<3%")]           = st.number_input("Keg <3% ABV",    value=float(rates.get(("Keg","<3%"),10.57)),           step=0.01,format="%.2f",key=f"r_k_lt3_{selected_date}")
        rates[("Keg","3%<=ABV<=3.5%")] = st.number_input("Keg 3–3.5% ABV", value=float(rates.get(("Keg","3%<=ABV<=3.5%"),33.11)), step=0.01,format="%.2f",key=f"r_k_mid_{selected_date}")
        rates[("Keg","ABV>3.5%")]      = st.number_input("Keg >3.5% ABV",  value=float(rates.get(("Keg","ABV>3.5%"),43.39)),      step=0.01,format="%.2f",key=f"r_k_gt_{selected_date}")
    periods[selected_date] = rates
    gi["excise_periods"]   = periods

    # ── Active period selector ────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("**Active period used in all price calculations:**")
    new_active = st.selectbox("Active Excise Period", sorted(periods.keys()),
                              index=sorted(periods.keys()).index(active) if active in periods else 0,
                              key="active_period_select")
    gi["active_excise_period"] = new_active
    gi["excise_rates"]         = periods[new_active]
    compute_all.clear()

    st.markdown("---")

    # ── Fixed Costs ───────────────────────────────────────────────────────────
    st.subheader("Fixed Costs (Annual, $)")
    fc = gi["fixed_costs"]
    c1,c2,c3 = st.columns(3)
    with c1:
        fc["rent"]      = st.number_input("Rent",            value=int(fc["rent"]),      step=1000)
        fc["brewers"]   = st.number_input("Brewers (Wages)", value=int(fc["brewers"]),   step=1000)
    with c2:
        fc["power"]     = st.number_input("Power",           value=int(fc["power"]),     step=500)
        fc["hire_et_al"]= st.number_input("Hire et al",      value=int(fc["hire_et_al"]),step=500)
    with c3:
        fc["rm"]        = st.number_input("Repairs & Maint.",value=int(fc["rm"]),        step=500)
    total_fc   = sum(fc.values())
    annual_prod = st.number_input("Annual Production (L)", value=int(gi["annual_production_l"]), step=1000)
    gi["annual_production_l"] = annual_prod
    fcp_l = total_fc / annual_prod if annual_prod > 0 else 0
    gi["fixed_cost_per_l"] = fcp_l
    ca, cb = st.columns(2)
    ca.metric("Total Fixed Costs",    f"${total_fc:,.0f}")
    cb.metric("Fixed Cost per Litre", f"${fcp_l:.4f}")

    st.markdown("---")

    # ── Packaging ─────────────────────────────────────────────────────────────
    st.subheader("Packaging Costs ($ per unit)")
    p = gi["packaging"]
    c1,c2,c3 = st.columns(3)
    with c1:
        p["printed_can"]     = st.number_input("Printed Can",        value=float(p["printed_can"]),     step=0.001,format="%.4f")
        p["can_lid"]         = st.number_input("Can Lid",            value=float(p["can_lid"]),         step=0.001,format="%.4f")
    with c2:
        p["pak_tech_per_can"]= st.number_input("Pak-Tech (per can)", value=float(p["pak_tech_per_can"]),step=0.001,format="%.4f")
        p["carton_per_can"]  = st.number_input("Carton (per can)",   value=float(p["carton_per_can"]),  step=0.001,format="%.4f")
    with c3:
        p["return_and_earn"] = st.number_input("Return & Earn",      value=float(p["return_and_earn"]), step=0.01, format="%.3f")
        p["keg_cost"]        = st.number_input("Keg cost ($)",       value=float(p["keg_cost"]),        step=0.01, format="%.2f")
    base_can = p["printed_can"] + p["can_lid"] + p["carton_per_can"]
    st.info(f"Packaging per can (excl. Pak-Tech & R&E): ${base_can:.4f}  |  With Pak-Tech: ${base_can+p['pak_tech_per_can']:.4f}  |  R&E is a separate cost line")

    st.markdown("---")

    # ── Channel Discounts ─────────────────────────────────────────────────────
    st.subheader("Channel / Pack Size Adjustments")
    st.caption("Retail/Wholesale: proportion discount off full price (0.25 = 25% off). Tap Room: multiplier adjustment where negative values raise the customer price.")
    cd = gi["channel_discounts"]
    c1,c2,c3 = st.columns(3)
    with c1:
        st.markdown("**Retail + Online**")
        cd[("Retail + Online","Can")]    = st.number_input("Can",    value=float(cd.get(("Retail + Online","Can"),   0)),    step=0.01,format="%.2f",key="ro_can")
        cd[("Retail + Online","4-Pack")] = st.number_input("4-Pack", value=float(cd.get(("Retail + Online","4-Pack"),0.10)),step=0.01,format="%.2f",key="ro_4p")
        cd[("Retail + Online","Case")]   = st.number_input("Case",   value=float(cd.get(("Retail + Online","Case"),  0.20)),step=0.01,format="%.2f",key="ro_case")
        cd[("Retail + Online","Keg")]    = st.number_input("Keg",    value=float(cd.get(("Retail + Online","Keg"),   0)),    step=0.01,format="%.2f",key="ro_keg")
    with c2:
        st.markdown("**Wholesale**")
        cd[("Wholesale","Case")] = st.number_input("Case", value=float(cd.get(("Wholesale","Case"),0.25)),step=0.01,format="%.2f",key="ws_case")
        cd[("Wholesale","Keg")]  = st.number_input("Keg",  value=float(cd.get(("Wholesale","Keg"), 0.35)),step=0.01,format="%.2f",key="ws_keg")
    with c3:
        st.markdown("**Tap Room**")
        cd[("Tap Room","Middy")]   = st.number_input("Middy",    value=float(cd.get(("Tap Room","Middy"),   -2.25)),step=0.05,format="%.2f",key="tr_middy")
        cd[("Tap Room","Schooner")]= st.number_input("Schooner", value=float(cd.get(("Tap Room","Schooner"),-1.80)),step=0.05,format="%.2f",key="tr_sch")
        cd[("Tap Room","Pint")]    = st.number_input("Pint",     value=float(cd.get(("Tap Room","Pint"),    -1.70)),step=0.05,format="%.2f",key="tr_pint")
        cd[("Tap Room","Jug")]     = st.number_input("Jug",      value=float(cd.get(("Tap Room","Jug"),     -1.60)),step=0.05,format="%.2f",key="tr_jug")

    st.session_state.gi = gi
    compute_all.clear()

    st.markdown("---")
    st.subheader("💾 Save Settings")
    st.caption("Changes apply immediately in this session. Save to persist across refreshes. Snapshot records current prices to Price History.")

    c_save, c_snap_label, c_snap_btn, c_reset = st.columns([1,2,1,1])
    with c_save:
        if st.button("💾 Save", type="primary", use_container_width=True):
            result = save_settings(st.session_state.gi, st.session_state.beers)
            if result is True:
                st.success("✅ Saved!")
            else:
                st.error(f"Save failed: {result}")
    with c_snap_label:
        snap_label = st.text_input("Snapshot label", placeholder="e.g. Post Aug 2025 excise update", label_visibility="collapsed")
    with c_snap_btn:
        if st.button("📸 Save Snapshot", use_container_width=True):
            lbl = snap_label.strip() or f"Snapshot {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            record_price_snapshot(lbl, st.session_state.beers, st.session_state.gi)
            st.success(f"Snapshot '{lbl}' saved.")
    with c_reset:
        if st.button("↩️ Reset Defaults", use_container_width=True):
            st.session_state.gi    = default_general_inputs()
            st.session_state.beers = default_beers()
            compute_all.clear()
            st.success("Reset to defaults.")
            st.rerun()

    st.markdown("---")
    st.subheader("☁️ Data Sync")
    if GITHUB_ENABLED:
        st.success(
            f"GitHub sync is **on**. Every Save/Snapshot writes straight to "
            f"`{GITHUB_REPO}` (branch `{GITHUB_BRANCH}`), so your data survives app reboots and redeploys "
            f"without any manual steps."
        )
    else:
        st.warning(
            "GitHub sync is **off**. Anything saved right now only lives on this container's disk and "
            "will be lost on the next reboot/redeploy. Add `GITHUB_REPO` and `GITHUB_TOKEN` "
            "(and optionally `GITHUB_BRANCH`) to this app's Secrets in Streamlit Cloud to turn it on. "
            "Until then, use the manual downloads below and upload them to your repo yourself."
        )
    st.caption("Manual backups (optional extra safety net even with GitHub sync on):")
    col_dl1, col_dl2, col_dl3, col_dl4 = st.columns(4)
    with col_dl1:
        settings_json = json.dumps(_gi_to_json(st.session_state.gi), indent=2)
        st.download_button("⬇️ Settings", data=settings_json, file_name="brewery_settings.json",
                           mime="application/json", use_container_width=True)
    with col_dl2:
        beers_json = json.dumps(st.session_state.beers, indent=2)
        st.download_button("⬇️ Beers", data=beers_json, file_name="brewery_beers.json",
                           mime="application/json", use_container_width=True)
    with col_dl3:
        price_hist_json = json.dumps(load_history(), indent=2)
        st.download_button("⬇️ Price History", data=price_hist_json, file_name="brewery_price_history.json",
                           mime="application/json", use_container_width=True)
    with col_dl4:
        excise_hist_json = json.dumps(load_excise_history(), indent=2)
        st.download_button("⬇️ Excise History", data=excise_hist_json, file_name="brewery_excise_history.json",
                           mime="application/json", use_container_width=True)


# ─────────────────────────────────────────────────────────────────────────────
# PAGE: BEER INPUTS
# ─────────────────────────────────────────────────────────────────────────────
elif page == "🍺 Beer Inputs":
    st.title("🍺 Beer Inputs")
    st.caption("Add, edit, or deactivate beers. Prices recalculate instantly.")
    beers = st.session_state.beers

    # ── Add new beer — no st.form so warnings persist across reruns ──────────
    with st.expander("➕ Add a New Beer"):
        c1,c2,c3 = st.columns(3)
        new_name     = c1.text_input("Beer Name", value=st.session_state.get("nb_name","New Beer"), key="nb_name")
        new_abv      = c2.number_input("ABV", value=st.session_state.get("nb_abv",0.050), min_value=0.0, max_value=0.20, step=0.001, format="%.3f", key="nb_abv")
        new_batch    = c3.number_input("Batch Size (L)", value=st.session_state.get("nb_batch",2000), step=100, key="nb_batch")
        c1,c2,c3 = st.columns(3)
        new_can_size = c1.number_input("Can Size (L)", value=st.session_state.get("nb_can_size",0.375), step=0.005, format="%.3f", key="nb_can_size")
        new_can_prop = c2.number_input("Proportion Cans", value=st.session_state.get("nb_can_prop",0.50), min_value=0.0, max_value=1.0, step=0.05, key="nb_can_prop")
        new_keg_prop = c3.number_input("Proportion Kegs", value=st.session_state.get("nb_keg_prop",0.50), min_value=0.0, max_value=1.0, step=0.05, key="nb_keg_prop")
        c1,c2,c3 = st.columns(3)
        new_cpc      = c1.number_input("Cans per Case", value=st.session_state.get("nb_cpc",16), step=1, key="nb_cpc")
        new_raw      = c2.number_input("Raw Materials ($/batch)", value=st.session_state.get("nb_raw",1500.0), step=50.0, key="nb_raw")
        new_margin   = c3.number_input("Target Margin %", value=st.session_state.get("nb_margin",37.0), step=1.0, key="nb_margin") / 100
        c1,c2,c3 = st.columns(3)
        new_royalty  = c1.number_input("Royalty %", value=st.session_state.get("nb_royalty",0.0), step=1.0, key="nb_royalty") / 100
        new_pak_tech = c2.checkbox("Uses Pak-Tech", value=st.session_state.get("nb_pak_tech",False), key="nb_pak_tech")

        new_beer = {
            "name": new_name, "abv": new_abv, "batch_size_l": new_batch,
            "can_size_l": new_can_size, "proportion_cans": new_can_prop,
            "proportion_kegs": new_keg_prop, "cans_per_case": new_cpc,
            "keg_size_l": 50, "pak_tech": new_pak_tech,
            "raw_materials": new_raw, "base_margin": new_margin,
            "royalty_pct": new_royalty, "active": True,
        }

        # Show any pending unchanged-defaults warning
        unchanged = unchanged_fields(new_beer)
        if unchanged and st.session_state.get("nb_warn_shown"):
            warn_lines = "\n".join(f"- **{f}**" for f in unchanged)
            st.warning(
                f"⚠️ **The following fields still have their default values** — please check before adding:\n\n"
                + warn_lines
                + "\n\nClick **Add Beer** again to confirm, or update the values above."
            )

        prop_ok = abs(new_can_prop + new_keg_prop - 1.0) <= 0.001
        if not prop_ok:
            st.error(f"⚠️ Proportion Cans ({new_can_prop}) + Kegs ({new_keg_prop}) = {new_can_prop+new_keg_prop:.2f} — must equal 1.0")

        if st.button("➕ Add Beer", type="primary", disabled=not prop_ok):
            if unchanged and not st.session_state.get("nb_warn_shown"):
                # First click — show warning, don't add yet
                st.session_state["nb_warn_shown"] = True
                st.rerun()
            else:
                # Either no warnings, or user clicked Add a second time to confirm
                beers.append(new_beer)
                compute_all.clear()
                save_settings(st.session_state.gi, beers)
                # Clear the add-beer session state
                for k in ["nb_name","nb_abv","nb_batch","nb_can_size","nb_can_prop",
                          "nb_keg_prop","nb_cpc","nb_raw","nb_margin","nb_royalty",
                          "nb_pak_tech","nb_warn_shown"]:
                    st.session_state.pop(k, None)
                st.success(f"Added **{new_name}**!")
                st.rerun()

    st.markdown("---")

    for i, beer in enumerate(beers):
        label = f"{'✅' if beer['active'] else '⬜'} {beer['name']}  (ABV {beer['abv']*100:.1f}%)"
        with st.expander(label):
            with st.form(f"beer_{i}"):
                c1,c2,c3 = st.columns(3)
                beer["name"]           = c1.text_input("Beer Name",          value=beer["name"],              key=f"n_{i}")
                beer["abv"]            = c2.number_input("ABV",              value=beer["abv"],  min_value=0.0, max_value=0.20, step=0.001, format="%.3f", key=f"abv_{i}")
                beer["batch_size_l"]   = c3.number_input("Batch Size (L)",   value=int(beer["batch_size_l"]), step=100, key=f"bs_{i}")
                c1,c2,c3 = st.columns(3)
                beer["can_size_l"]     = c1.number_input("Can Size (L)",     value=beer["can_size_l"],     step=0.005,format="%.3f",key=f"cs_{i}")
                beer["proportion_cans"]= c2.number_input("Proportion Cans",  value=beer["proportion_cans"],min_value=0.0,max_value=1.0,step=0.05,key=f"pc_{i}")
                beer["proportion_kegs"]= c3.number_input("Proportion Kegs",  value=beer["proportion_kegs"],min_value=0.0,max_value=1.0,step=0.05,key=f"pk_{i}")
                c1,c2,c3 = st.columns(3)
                beer["cans_per_case"]  = c1.number_input("Cans per Case",    value=int(beer["cans_per_case"]),step=1,key=f"cpc_{i}")
                beer["raw_materials"]  = c2.number_input("Raw Materials ($/batch)",value=float(beer["raw_materials"]),step=50.0,key=f"rm_{i}")
                beer["base_margin"]    = c3.number_input("Target Margin %",  value=beer["base_margin"]*100,step=1.0,key=f"bm_{i}") / 100
                c1,c2,c3 = st.columns(3)
                beer["royalty_pct"]    = c1.number_input("Royalty %",        value=beer["royalty_pct"]*100,step=1.0,key=f"rp_{i}") / 100
                beer["pak_tech"]       = c2.checkbox("Uses Pak-Tech",        value=beer["pak_tech"], key=f"pt_{i}")
                beer["active"]         = c3.checkbox("Active",               value=beer["active"],   key=f"act_{i}")
                c1, c2 = st.columns([1,5])
                if c1.form_submit_button("💾 Save"):
                    if abs(beer["proportion_cans"]+beer["proportion_kegs"]-1.0) > 0.001:
                        st.error(f"Proportion Cans + Kegs = {beer['proportion_cans']+beer['proportion_kegs']:.2f} — must equal 1.0")
                    else:
                        beers[i] = beer
                        compute_all.clear()
                        save_settings(st.session_state.gi, beers)
                        st.success("Saved!")
                        st.rerun()

            # Delete with confirm checkbox — outside form so it persists
            confirm_key = f"confirm_del_{i}"
            c_del, c_chk, _ = st.columns([1, 2, 3])
            if c_del.button(f"🗑️ Delete", key=f"del_{i}"):
                st.session_state[confirm_key] = True
                st.rerun()
            if st.session_state.get(confirm_key):
                confirmed = c_chk.checkbox(
                    f"Confirm delete **{beer['name']}** — cannot be undone",
                    key=f"chk_{i}"
                )
                if confirmed:
                    beers.pop(i)
                    compute_all.clear()
                    save_settings(st.session_state.gi, beers)
                    st.session_state.pop(confirm_key, None)
                    st.rerun()

    st.session_state.beers = beers


# ─────────────────────────────────────────────────────────────────────────────
# PAGE: WHAT-IF ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────
elif page == "🔍 What-If Analysis":
    st.title("🔍 What-If Analysis")
    st.caption("Explore how price and margin change for any beer without touching live pricing.")

    active_beers = [b for b in st.session_state.beers if b.get("active")]
    if not active_beers:
        st.warning("No active beers to analyse.")
        st.stop()

    gi = st.session_state.gi
    sel_name  = st.selectbox("Select Beer", [b["name"] for b in active_beers])
    base_beer = next(b for b in active_beers if b["name"] == sel_name)

    st.markdown("---")
    st.markdown("### Adjust Parameters")
    st.caption("Sliders and inputs below are sandboxed — they do not affect live pricing.")

    c1,c2,c3 = st.columns(3)
    with c1:
        wi_abv   = st.number_input("ABV",                    value=base_beer["abv"],          min_value=0.0,max_value=0.20,step=0.001,format="%.3f")
        wi_batch = st.number_input("Batch Size (L)",         value=float(base_beer["batch_size_l"]),step=100.0)
        wi_raw   = st.number_input("Raw Materials ($/batch)",value=float(base_beer["raw_materials"]),step=50.0)
    with c2:
        wi_margin   = st.number_input("Target Margin %",    value=base_beer["base_margin"]*100,step=0.5) / 100
        wi_royalty  = st.number_input("Royalty %",          value=base_beer["royalty_pct"]*100,step=0.5) / 100
        wi_can_size = st.number_input("Can Size (L)",       value=base_beer["can_size_l"],    step=0.005,format="%.3f")
        wi_cpc      = st.number_input("Cans per Case",      value=int(base_beer["cans_per_case"]), step=1, min_value=1)
    with c3:
        wi_can_prop = st.number_input("Proportion Cans",    value=base_beer["proportion_cans"],min_value=0.0,max_value=1.0,step=0.05)
        wi_keg_prop = st.number_input("Proportion Kegs",    value=base_beer["proportion_kegs"],min_value=0.0,max_value=1.0,step=0.05)
        wi_pak_tech = st.checkbox("Uses Pak-Tech",          value=base_beer["pak_tech"])

    period_dates = sorted(gi.get("excise_periods",{}).keys())
    wi_period = st.selectbox("Excise Period to Model", period_dates,
                             index=period_dates.index(gi.get("active_excise_period","")) if gi.get("active_excise_period","") in period_dates else 0)

    if abs(wi_can_prop + wi_keg_prop - 1.0) > 0.001:
        st.error(f"Proportion Cans + Kegs = {wi_can_prop+wi_keg_prop:.2f} — should equal 1.0")
        st.stop()

    wi_beer = {**base_beer,
        "abv": wi_abv, "batch_size_l": wi_batch, "raw_materials": wi_raw,
        "base_margin": wi_margin, "royalty_pct": wi_royalty,
        "can_size_l": wi_can_size, "cans_per_case": wi_cpc,
        "proportion_cans": wi_can_prop,
        "proportion_kegs": wi_keg_prop, "pak_tech": wi_pak_tech,
    }
    wi_gi = {**gi,
        "excise_rates": gi["excise_periods"].get(wi_period, gi["excise_rates"]),
    }
    fc = wi_gi["fixed_costs"]
    wi_gi["fixed_cost_per_l"] = sum(fc.values()) / wi_gi["annual_production_l"] if wi_gi["annual_production_l"] > 0 else 0

    try:
        wi_rows   = compute_pricing(wi_beer, wi_gi)
        base_gi   = {**gi, "excise_rates": gi["excise_periods"].get(gi.get("active_excise_period",""), gi["excise_rates"])}
        base_gi["fixed_cost_per_l"] = gi["fixed_cost_per_l"]
        base_rows = compute_pricing(base_beer, base_gi)

        wi_df   = pd.DataFrame(wi_rows)
        base_df = pd.DataFrame(base_rows)
        merged  = wi_df.merge(base_df, on=["Channel","Package"], suffixes=(" WI"," Base"))
        merged["Price Δ ($)"]   = merged["Sell Price ($) WI"]  - merged["Sell Price ($) Base"]
        merged["Margin Δ (pp)"] = merged["Margin % WI"]        - merged["Margin % Base"]

        st.markdown("---")
        channels = ["Retail + Online","Wholesale","Tap Room"]
        for tab, ch in zip(st.tabs(channels), channels):
            with tab:
                sub = merged[merged["Channel"]==ch][[
                    "Package",
                    "Cost ($) Base","Cost ($) WI",
                    "Sell Price ($) Base","Sell Price ($) WI","Price Δ ($)",
                    "Margin % Base","Margin % WI","Margin Δ (pp)",
                ]].set_index("Package").rename(columns={
                    "Cost ($) Base":"Cost (Current)","Cost ($) WI":"Cost (What-If)",
                    "Sell Price ($) Base":"Price (Current)","Sell Price ($) WI":"Price (What-If)",
                    "Margin % Base":"Margin (Current)","Margin % WI":"Margin (What-If)",
                })
                def col_d(v):
                    if isinstance(v,(int,float)): return "color: green" if v>0 else ("color: red" if v<0 else "")
                    return ""
                st.dataframe(sub.style.format({
                    "Cost (Current)":"${:.4f}","Cost (What-If)":"${:.4f}",
                    "Price (Current)":"${:.0f}","Price (What-If)":"${:.0f}","Price Δ ($)":"${:+.2f}",
                    "Margin (Current)":"{:.1f}%","Margin (What-If)":"{:.1f}%","Margin Δ (pp)":"{:+.1f}pp",
                }).map(col_d, subset=["Price Δ ($)","Margin Δ (pp)"]), use_container_width=True)

        # Summary callout for retail can
        bc = base_df[(base_df["Channel"]=="Retail + Online")&(base_df["Package"]=="Can")]
        wc = wi_df[(wi_df["Channel"]=="Retail + Online")&(wi_df["Package"]=="Can")]
        if not bc.empty and not wc.empty:
            bc, wc = bc.iloc[0], wc.iloc[0]
            st.markdown("---")
            st.markdown("**Retail Can summary**")
            c1,c2,c3,c4 = st.columns(4)
            c1.metric("Current Price",  f"${bc['Sell Price ($)']:.0f}")
            c2.metric("What-If Price",  f"${wc['Sell Price ($)']:.0f}", delta=f"${wc['Sell Price ($)']-bc['Sell Price ($)']:+.0f}")
            c3.metric("Current Margin", f"{bc['Margin %']:.1f}%")
            c4.metric("What-If Margin", f"{wc['Margin %']:.1f}%",       delta=f"{wc['Margin %']-bc['Margin %']:+.1f}pp")

    except Exception as e:
        st.error(f"Calculation error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# PAGE: PRICE HISTORY
# ─────────────────────────────────────────────────────────────────────────────
elif page == "📜 Price History":
    st.title("📜 Price History")
    st.caption("Snapshots of pricing saved over time. Record a new snapshot from **⚙️ General Inputs**.")

    history = load_history()
    if not history:
        st.info("No price history yet. Go to **⚙️ General Inputs → Save Snapshot** to record your first entry.")
        st.stop()

    snap_labels = [f"{s['timestamp']}  —  {s['label']}" for s in history]
    c1, c2 = st.columns([4,1])
    sel_idx = c1.selectbox("Select Snapshot", range(len(snap_labels)), format_func=lambda i: snap_labels[i])
    with c2:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🗑️ Delete Snapshot", use_container_width=True):
            st.session_state["confirm_del_snap"] = sel_idx
            st.rerun()

    if st.session_state.get("confirm_del_snap") == sel_idx:
        st.warning(f"⚠️ You are about to delete snapshot **{history[sel_idx]['label']}**. Tick to confirm:")
        confirmed_snap = st.checkbox("Yes, delete this snapshot — this cannot be undone", key="chk_snap")
        if st.button("Cancel delete", key="cancel_snap"):
            st.session_state.pop("confirm_del_snap", None)
            st.rerun()
        if confirmed_snap:
            history.pop(sel_idx)
            save_history(history)
            st.session_state.pop("confirm_del_snap", None)
            st.success("Snapshot deleted.")
            st.rerun()

    snap = history[sel_idx]
    st.markdown(f"**{snap['label']}**  |  Recorded: {snap['timestamp']}  |  Excise period: {snap.get('excise_period','—')}")

    df_snap = pd.DataFrame(snap["prices"])
    if df_snap.empty:
        st.warning("No price data in this snapshot.")
        st.stop()

    # Merge with current prices for delta comparison
    if not df_all.empty:
        cur = df_all[["Beer","Channel","Package","Sell Price ($)","Margin %"]].copy()
        cur.columns = ["beer","channel","package","cur_price","cur_margin"]
        df_snap = df_snap.merge(cur, on=["beer","channel","package"], how="left")
        df_snap["Price Δ ($)"]   = df_snap["sell_price"] - df_snap["cur_price"]
        df_snap["Margin Δ (pp)"] = df_snap["margin_pct"] - df_snap["cur_margin"]
        show_delta = True
    else:
        show_delta = False

    channels = ["Retail + Online","Wholesale","Tap Room"]
    for tab, ch in zip(st.tabs(channels), channels):
        with tab:
            sub = df_snap[df_snap["channel"]==ch]
            if sub.empty: continue
            pp = sub.pivot_table(index="beer", columns="package", values="sell_price", aggfunc="first")
            pm = sub.pivot_table(index="beer", columns="package", values="margin_pct",  aggfunc="first")
            st.markdown("**Snapshot Prices ($)**")
            st.dataframe(pp.style.format("${:.2f}"), use_container_width=True)
            st.markdown("**Snapshot Margin %**")
            st.dataframe(pm.style.format("{:.1f}%"), use_container_width=True)
            if show_delta:
                pd_delta = sub.pivot_table(index="beer", columns="package", values="Price Δ ($)", aggfunc="first")
                def col_d(v):
                    if isinstance(v,(int,float)): return "color: green" if v>0 else ("color: red" if v<0 else "")
                    return ""
                st.markdown("**vs Current Prices ($)**")
                st.dataframe(pd_delta.style.format("${:+.2f}").map(col_d), use_container_width=True)

    # Compare two snapshots
    if len(history) >= 2:
        st.markdown("---")
        st.subheader("Compare Two Snapshots")
        c1, c2 = st.columns(2)
        idx_a = c1.selectbox("Snapshot A", range(len(snap_labels)), format_func=lambda i: snap_labels[i], key="cmp_a")
        idx_b = c2.selectbox("Snapshot B", range(len(snap_labels)), index=min(1,len(snap_labels)-1), format_func=lambda i: snap_labels[i], key="cmp_b")
        if idx_a != idx_b:
            df_a = pd.DataFrame(history[idx_a]["prices"])
            df_b = pd.DataFrame(history[idx_b]["prices"])
            merged = df_a.merge(df_b, on=["beer","channel","package"], suffixes=(" A"," B"))
            merged["Price Δ ($)"]   = merged["sell_price B"] - merged["sell_price A"]
            merged["Margin Δ (pp)"] = merged["margin_pct B"] - merged["margin_pct A"]
            for tab, ch in zip(st.tabs(channels), channels):
                with tab:
                    sub = merged[merged["channel"]==ch][["beer","package","sell_price A","sell_price B","Price Δ ($)","margin_pct A","margin_pct B","Margin Δ (pp)"]].set_index(["beer","package"])
                    def col_d2(v):
                        if isinstance(v,(int,float)): return "color: green" if v>0 else ("color: red" if v<0 else "")
                        return ""
                    st.dataframe(sub.style.format({
                        "sell_price A":"${:.2f}","sell_price B":"${:.2f}","Price Δ ($)":"${:+.2f}",
                        "margin_pct A":"{:.1f}%","margin_pct B":"{:.1f}%","Margin Δ (pp)":"{:+.1f}pp",
                    }).map(col_d2, subset=["Price Δ ($)","Margin Δ (pp)"]), use_container_width=True)

    st.markdown("---")
    all_hist_rows = [r for s in history for r in s["prices"]]
    if all_hist_rows:
        st.download_button("⬇️ Export Full History (CSV)",
                           pd.DataFrame(all_hist_rows).to_csv(index=False),
                           "price_history.csv","text/csv")


# ─────────────────────────────────────────────────────────────────────────────
# PAGE: EXCISE RETURN
# ─────────────────────────────────────────────────────────────────────────────
elif page == "🧾 Excise Return":
    st.title("🧾 Excise Return")
    st.caption("Calculate monthly beer volume by ABV category for ATO excise reporting.")

    # Build beer lookup from current beer list
    beer_lookup = {
        b["name"]: {
            "abv":          b["abv"],
            "can_size_l":   b["can_size_l"],
            "cans_per_case":b["cans_per_case"],
            "keg_size_l":   b.get("keg_size_l", 50),
        }
        for b in st.session_state.beers if b.get("active")
    }
    beer_names_list = list(beer_lookup.keys())

    # ── Period selector ───────────────────────────────────────────────────────
    st.subheader("1. Select Period")
    col_y, col_m, _ = st.columns([1,1,4])
    now = datetime.now()
    sel_year  = col_y.number_input("Year",  value=now.year, min_value=2020, max_value=2030, step=1)
    months    = ["January","February","March","April","May","June",
                 "July","August","September","October","November","December"]
    sel_month = col_m.selectbox("Month", months, index=now.month-2 if now.month > 1 else 11)
    period_key = f"{sel_year}-{months.index(sel_month)+1:02d}"
    period_label = f"{sel_month} {sel_year}"

    st.markdown("---")

    # ── Session state for this period's rows ──────────────────────────────────
    rows_key = f"excise_rows_{period_key}"
    if rows_key not in st.session_state:
        st.session_state[rows_key] = []

    all_rows = st.session_state[rows_key]

    # ── Upload Square email ───────────────────────────────────────────────────
    st.subheader("2. Upload Square Tap Room Report (optional)")
    st.caption("Upload the Square Sales Report email saved as a .msg file.")
    sq_file = st.file_uploader("Square .msg file", type=["msg"], key=f"sq_{period_key}")
    if sq_file:
        try:
            import extract_msg, tempfile
            with tempfile.NamedTemporaryFile(delete=False, suffix=".msg") as tmp:
                tmp.write(sq_file.read())
                tmp_path = tmp.name
            msg = extract_msg.Message(tmp_path)
            os.unlink(tmp_path)
            sq_rows = parse_square_email(msg.body, beer_lookup)
            if sq_rows:
                # Remove any existing Square rows and replace
                all_rows = [r for r in all_rows if r["source"] != "Tap Room (Square)"]
                all_rows.extend(sq_rows)
                st.session_state[rows_key] = all_rows
                st.success(f"✅ Extracted {len(sq_rows)} line items from Square report.")
            else:
                st.warning("No Nomad/Gweilo beer items found in this email.")
        except Exception as e:
            st.error(f"Could not parse Square email: {e}")

    st.markdown("---")

    # ── Upload Xero report ────────────────────────────────────────────────────
    st.subheader("3. Upload Xero Wholesale Report (optional)")
    st.caption("Upload the Xero 'Sales by Item' report as an .xls or .xlsx file.")
    xero_file = st.file_uploader("Xero .xls / .xlsx file",
                                  type=["xls","xlsx"], key=f"xero_{period_key}")
    if xero_file:
        try:
            engine = "xlrd" if xero_file.name.endswith(".xls") else "openpyxl"
            df_raw = pd.read_excel(xero_file, engine=engine, header=None)
            xero_rows = parse_xero_report(df_raw, beer_lookup)
            if xero_rows:
                all_rows = [r for r in all_rows if r["source"] != "Wholesale (Xero)"]
                all_rows.extend(xero_rows)
                st.session_state[rows_key] = all_rows
                st.success(f"✅ Extracted {len(xero_rows)} line items from Xero report.")
            else:
                st.warning("No beer items found in this Xero report.")
        except Exception as e:
            st.error(f"Could not parse Xero file: {e}")

    st.markdown("---")

    # ── Review & edit parsed rows ─────────────────────────────────────────────
    st.subheader("4. Review, Edit & Add Manual Entries")

    if all_rows:
        # Check for unmatched or low-confidence rows
        low_conf = [r for r in all_rows if r["beer_name"] is None or r["match_score"] < 0.6]
        if low_conf:
            st.warning(
                f"⚠️ **{len(low_conf)} rows could not be confidently matched** to a beer in your list. "
                "Please review and assign them below, or delete if not applicable."
            )

    # Show editable table of all rows
    if all_rows:
        st.markdown("**Current entries** — edit beer assignment, quantities or litres, or delete rows:")
        for idx, row in enumerate(list(all_rows)):
            conf_color = "🟢" if row["match_score"] >= 0.75 else ("🟡" if row["match_score"] >= 0.50 else "🔴")
            with st.expander(
                f"{conf_color} {row['source_name']}  |  {row['serve']}  "
                f"|  qty={row['qty']}  |  {row['total_litres']:.3f} L  "
                f"|  → {row['beer_name'] or '⚠️ UNMATCHED'}",
                expanded=(row["beer_name"] is None or row["match_score"] < 0.6)
            ):
                c1, c2, c3, c4 = st.columns([2,1,1,1])
                new_beer = c1.selectbox(
                    "Beer",
                    ["— unmatched —"] + beer_names_list,
                    index=(beer_names_list.index(row["beer_name"]) + 1
                           if row["beer_name"] in beer_names_list else 0),
                    key=f"eb_{period_key}_{idx}"
                )
                new_qty = c2.number_input("Qty", value=int(row["qty"]),
                                          min_value=0, step=1,
                                          key=f"eq_{period_key}_{idx}")
                new_le  = c3.number_input("Litres each", value=float(row["litres_each"]),
                                          min_value=0.0, step=0.001, format="%.4f",
                                          key=f"el_{period_key}_{idx}")
                if c4.button("🗑️", key=f"ed_{period_key}_{idx}", help="Delete this row"):
                    all_rows.pop(idx)
                    st.session_state[rows_key] = all_rows
                    st.rerun()

                # Apply edits live
                resolved_beer = new_beer if new_beer != "— unmatched —" else None
                all_rows[idx]["beer_name"]    = resolved_beer
                all_rows[idx]["qty"]          = new_qty
                all_rows[idx]["litres_each"]  = new_le
                all_rows[idx]["total_litres"] = round(new_qty * new_le, 4)
                if resolved_beer and resolved_beer in beer_lookup:
                    all_rows[idx]["abv"] = beer_lookup[resolved_beer]["abv"]
                st.session_state[rows_key] = all_rows

    # Add manual row
    st.markdown("---")
    with st.expander("➕ Add Manual Entry"):
        mc1, mc2, mc3, mc4, mc5 = st.columns([2,1,1,1,1])
        man_beer  = mc1.selectbox("Beer", ["— select —"] + beer_names_list, key=f"mb_{period_key}")
        man_src   = mc2.selectbox("Source", ["Tap Room","Wholesale","Other"], key=f"ms_{period_key}")
        man_serve = mc3.text_input("Serve/Package", "Keg 50L", key=f"msrv_{period_key}")
        man_qty   = mc4.number_input("Qty", value=1, min_value=1, step=1, key=f"mq_{period_key}")
        man_le    = mc5.number_input("Litres each", value=50.0, min_value=0.0,
                                     step=0.5, format="%.3f", key=f"mle_{period_key}")
        if st.button("➕ Add Row", key=f"madd_{period_key}"):
            if man_beer != "— select —":
                pkg_type = "keg" if "keg" in man_serve.lower() else "can"
                abv = beer_lookup.get(man_beer, {}).get("abv", None)
                all_rows.append({
                    "source": man_src,
                    "source_name": f"Manual: {man_beer}",
                    "serve": man_serve,
                    "beer_name": man_beer,
                    "match_score": 1.0,
                    "package_type": pkg_type,
                    "qty": man_qty,
                    "litres_each": man_le,
                    "total_litres": round(man_qty * man_le, 4),
                    "abv": abv,
                })
                st.session_state[rows_key] = all_rows
                st.success(f"Added {man_beer} — {man_serve} × {man_qty}")
                st.rerun()

    st.markdown("---")

    # ── Excise Summary ────────────────────────────────────────────────────────
    st.subheader("5. Excise Summary")

    matched_rows = [r for r in all_rows if r["beer_name"] and r["abv"] is not None]
    unmatched_count = len([r for r in all_rows if not r["beer_name"] or r["abv"] is None])

    if not matched_rows:
        st.info("No matched rows yet. Upload files or add manual entries above.")
    else:
        if unmatched_count:
            st.warning(f"⚠️ {unmatched_count} rows are excluded from the summary (unmatched — assign a beer above).")

        buckets = compute_excise_summary(matched_rows)

        # Summary table
        st.caption("**Taxable pure ethanol litres** = (ABV% − 1.15%) × beer volume. Enter these figures in your ATO excise return.")
        summary_df = pd.DataFrame([
            {"Package": "Can", "ABV Category": "≤ 3.5%", "Taxable Ethanol (L)": round(buckets[("can","lte35")], 3)},
            {"Package": "Can", "ABV Category": "> 3.5%", "Taxable Ethanol (L)": round(buckets[("can","gt35")],  3)},
            {"Package": "Keg", "ABV Category": "≤ 3.5%", "Taxable Ethanol (L)": round(buckets[("keg","lte35")], 3)},
            {"Package": "Keg", "ABV Category": "> 3.5%", "Taxable Ethanol (L)": round(buckets[("keg","gt35")],  3)},
        ])
        summary_df["Taxable Ethanol (L)"] = summary_df["Taxable Ethanol (L)"].apply(lambda x: f"{x:,.3f}")
        st.dataframe(summary_df, hide_index=True, use_container_width=False)

        # Beer-level breakdown
        st.markdown("##### Volume by Beer")
        beer_summary = {}
        for r in matched_rows:
            abv = r["abv"]
            key = (r["beer_name"], r["package_type"],
                   "≤3.5%" if abv <= 0.035 else ">3.5%")
            if key not in beer_summary:
                beer_summary[key] = {"beer_litres": 0.0, "ethanol_litres": 0.0}
            beer_summary[key]["beer_litres"]    += r["total_litres"]
            beer_summary[key]["ethanol_litres"] += taxable_ethanol(r["total_litres"], abv)

        brows = []
        for (beer, pkg, cat), vols in sorted(beer_summary.items()):
            abv_val = beer_lookup.get(beer, {}).get("abv", 0)
            brows.append({
                "Beer": beer, "Package": pkg.title(),
                "ABV": f"{abv_val*100:.1f}%", "Category": cat,
                "Beer Volume (L)": round(vols["beer_litres"], 3),
                "Taxable Ethanol (L)": round(vols["ethanol_litres"], 3),
            })
        st.dataframe(pd.DataFrame(brows), hide_index=True, use_container_width=True)
        st.caption("Taxable Ethanol (L) = (ABV% − 1.15%) × Beer Volume. This is the figure submitted to the ATO.")

        # ── Save to history ───────────────────────────────────────────────────
        st.markdown("---")
        col_save, col_dl, _ = st.columns([1,1,4])
        if col_save.button("💾 Save to Excise History", type="primary"):
            history = load_excise_history()
            # Remove any existing entry for this period
            history = [h for h in history if h["period_key"] != period_key]
            history.append({
                "period_key":   period_key,
                "period_label": period_label,
                "saved_at":     datetime.now().strftime("%Y-%m-%d %H:%M"),
                "buckets": {"|".join(k): v for k, v in buckets.items()},
                "rows":    matched_rows,
                "beer_summary": brows,
                "note": "Buckets and Taxable Ethanol (L) = (ABV% - 1.15%) x beer volume",
            })
            result = save_excise_history(history)
            if result is True:
                st.success(f"✅ Saved excise return for {period_label}.")
            else:
                st.error(f"Save issue: {result}")

        # CSV download of detail rows
        detail_df = pd.DataFrame(matched_rows)[
            ["source","beer_name","package_type","serve","qty","litres_each","total_litres","abv"]
        ]
        col_dl.download_button(
            "⬇️ Download Detail CSV",
            data=detail_df.to_csv(index=False),
            file_name=f"excise_{period_key}.csv",
            mime="text/csv",
        )

    st.markdown("---")

    # ── Historical excise returns ─────────────────────────────────────────────
    st.subheader("📋 Saved Excise Returns")
    history = load_excise_history()
    if not history:
        st.caption("No saved returns yet.")
    else:
        hist_labels = [f"{h['period_label']}  (saved {h['saved_at']})" for h in history]
        sel_hist = st.selectbox("View saved return", range(len(hist_labels)),
                                format_func=lambda i: hist_labels[i])
        h = history[sel_hist]
        st.markdown(f"**{h['period_label']}** — saved {h['saved_at']}")

        buckets_h = {tuple(k.split("|")): v for k, v in h["buckets"].items()}
        hist_df = pd.DataFrame([
            {"Package":"Can","ABV Category":"≤ 3.5%","Total Litres": f"{buckets_h.get(('can','lte35'),0):,.3f}"},
            {"Package":"Can","ABV Category":"> 3.5%","Total Litres": f"{buckets_h.get(('can','gt35'),0):,.3f}"},
            {"Package":"Keg","ABV Category":"≤ 3.5%","Total Litres": f"{buckets_h.get(('keg','lte35'),0):,.3f}"},
            {"Package":"Keg","ABV Category":"> 3.5%","Total Litres": f"{buckets_h.get(('keg','gt35'),0):,.3f}"},
        ])
        st.dataframe(hist_df, hide_index=True, use_container_width=False)

        if h.get("beer_summary"):
            st.markdown("##### By Beer")
            st.dataframe(pd.DataFrame(h["beer_summary"]), hide_index=True, use_container_width=True)

        if h.get("rows"):
            dl_df = pd.DataFrame(h["rows"])[
                ["source","beer_name","package_type","serve","qty","litres_each","total_litres","abv"]
            ]
            st.download_button(
                "⬇️ Download Detail CSV",
                data=dl_df.to_csv(index=False),
                file_name=f"excise_{h['period_key']}.csv",
                mime="text/csv",
                key=f"hist_dl_{sel_hist}",
            )


# ─────────────────────────────────────────────────────────────────────────────
# PAGE: BATCHES & STOCKTAKE
# ─────────────────────────────────────────────────────────────────────────────
elif page == "📦 Batches & Stocktake":
    st.title("📦 Batches & Stocktake")
    st.caption("Record each completed brew, and periodically true up stock levels with a physical count.")

    all_beer_names = sorted(set(b["name"] for b in st.session_state.beers))
    beer_can_size  = {b["name"]: b.get("can_size_l", 0.375) for b in st.session_state.beers}

    batches    = load_batches()
    stocktakes = load_stocktakes()

    # ── 1. Record a completed batch ──────────────────────────────────────────
    with st.expander("➕ Record a Completed Batch", expanded=not batches):
        with st.form("add_batch_form"):
            c1, c2, c3 = st.columns(3)
            b_beer   = c1.selectbox("Beer", all_beer_names or ["No beers configured"])
            b_number = c2.text_input("Batch Number", placeholder="e.g. B-2026-014")
            b_abv    = c3.number_input("Actual ABV", value=0.050, min_value=0.0, max_value=0.20, step=0.001, format="%.3f")

            c1, c2 = st.columns(2)
            b_brew_date   = c1.date_input("Brew / Completion Date", value=datetime.today())
            b_use_by_date = c2.date_input("Use By Date", value=datetime.today())

            st.markdown("**Cans**")
            c1, c2 = st.columns(2)
            b_can_size = c1.number_input("Can Size (L)", value=float(beer_can_size.get(b_beer, 0.375)), step=0.005, format="%.3f")
            b_cans     = c2.number_input("Cans Produced", value=0, min_value=0, step=1)

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
                        "cans_produced": b_cans,
                        "notes": b_notes,
                        "archived": False,
                    }
                    for size in KEG_SIZES:
                        new_batch[_keg_field(size)] = b_kegs[size]
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
                    c1.metric("Produced (cans)", int(b.get("cans_produced", 0)))
                    c2.metric("Produced (keg L)", f"{batch_produced_keg_litres(b):.0f} L")
                    c3.metric("Actual ABV", f"{b.get('actual_abv',0)*100:.1f}%")
                    keg_summary = ", ".join(f"{b.get(_keg_field(s2),0)}× {s2}L" for s2 in KEG_SIZES if b.get(_keg_field(s2), 0))
                    st.caption(f"Kegs produced: {keg_summary or 'none'}")
                    if s.get("last_stocktake_date"):
                        st.caption(f"Last stocktake: {s['last_stocktake_date']}")
                    else:
                        st.caption("No stocktake recorded yet — SOH is a full FIFO estimate from production.")
                    if b.get("notes"):
                        st.caption(f"Notes: {b['notes']}")
                    if st.button("🗄️ Archive batch (no longer in stock)", key=f"arch_{b['batch_id']}"):
                        for bb in batches:
                            if bb["batch_id"] == b["batch_id"]:
                                bb["archived"] = True
                        save_batches(batches)
                        st.rerun()

    st.markdown("---")

    # ── 3. Record a stocktake ─────────────────────────────────────────────────
    st.subheader("📋 Record a Stocktake")
    st.caption("Enter what was actually counted for a specific batch. This overwrites the calculated SOH from that date forward.")

    active_beers_with_batches = sorted(set(b["beer_name"] for b in active_batches_view))
    if not active_beers_with_batches:
        st.info("Add a batch above before recording a stocktake.")
    else:
        with st.form("stocktake_form"):
            c1, c2 = st.columns(2)
            st_beer = c1.selectbox("Beer", active_beers_with_batches)
            beer_batch_options = [b for b in active_batches_view if b["beer_name"] == st_beer]
            st_batch = c2.selectbox(
                "Batch",
                beer_batch_options,
                format_func=lambda b: f"{b['batch_number']} (brewed {b.get('brew_date','—')})"
            )
            st_date = st.date_input("Stocktake Date", value=datetime.today())

            st.markdown("**Counted Cans**")
            st_cans = st.number_input("Cans counted", value=0, min_value=0, step=1)

            st.markdown("**Counted Kegs (by size)**")
            keg_cols2 = st.columns(len(KEG_SIZES))
            st_kegs = {}
            for col, size in zip(keg_cols2, KEG_SIZES):
                st_kegs[size] = col.number_input(f"{size}L ", value=0, min_value=0, step=1, key=f"st_keg_{size}")

            st_notes = st.text_area("Notes (optional)", key="st_notes")

            if st.form_submit_button("💾 Save Stocktake", type="primary"):
                new_st = {
                    "stocktake_id": f"{st_batch['batch_id']}__{st_date.strftime('%Y-%m-%d')}",
                    "beer_name": st_beer,
                    "batch_id": st_batch["batch_id"],
                    "stocktake_date": st_date.strftime("%Y-%m-%d"),
                    "soh_cans": st_cans,
                    "notes": st_notes,
                }
                for size in KEG_SIZES:
                    new_st[_keg_field(size)] = st_kegs[size]
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
        st_df = pd.DataFrame(stocktakes)[["stocktake_date", "beer_name", "batch_id", "soh_cans"] +
                                         [_keg_field(s) for s in KEG_SIZES] + ["notes"]]
        st.dataframe(st_df.sort_values("stocktake_date", ascending=False), hide_index=True, use_container_width=True)


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
        fc1, fc2, fc3 = st.columns(3)
        forecast_window = fc1.number_input(
            "Months of sales history to use", value=3, min_value=1, max_value=12, step=1,
            key="fc_window",
            help="How many recent excise months to average for the forecast"
        )
        use_weighted = fc1.checkbox(
            "Weighted average (recent months count more)", value=True, key="fc_weighted",
            help="If checked, the most recent month gets the highest weight"
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
        soh_states = compute_soh_for_beer(beer_name, active_batches, stocktakes)
        if not soh_states:
            continue

        total_soh_litres = sum(s["soh_total_litres"] for s in soh_states)
        avg_monthly = weighted_avg_monthly_sales(beer_name, int(forecast_window), use_weighted)
        months_remaining = round(total_soh_litres / avg_monthly, 1) if avg_monthly > 0 else None
        ubd_months, ubd_date = months_to_earliest_use_by(beer_name, active_batches, today)

        stock_alert = months_remaining is not None and months_remaining <= stock_alert_months
        ubd_alert   = ubd_months is not None and ubd_months <= ubd_alert_months

        summary_rows.append({
            "Beer": beer_name,
            "SOH (L)": round(total_soh_litres, 1),
            "Avg Monthly Sales (L)": round(avg_monthly, 1),
            "Months Remaining (forecast)": months_remaining if months_remaining is not None else "—",
            "Earliest Use-By": ubd_date.strftime("%Y-%m-%d") if ubd_date else "—",
            "Months to Use-By": ubd_months if ubd_months is not None else "—",
            "⚠️ Stock Low": "🔴" if stock_alert else "",
            "⚠️ Use-By Soon": "🔴" if ubd_alert else "",
        })

        if stock_alert or ubd_alert:
            reasons = []
            if stock_alert:
                reasons.append(f"only ~{months_remaining} months of forecast stock left")
            if ubd_alert:
                reasons.append(f"earliest use-by is in ~{ubd_months} months ({ubd_date.strftime('%Y-%m-%d')})")
            alert_rows.append(f"**{beer_name}** — " + "; ".join(reasons))

    if alert_rows:
        st.warning("### 🚨 Alerts\n\n" + "\n\n".join(alert_rows))
    else:
        st.success("No stock or use-by alerts at current thresholds.")

    st.markdown("---")
    st.subheader("Summary — All Beers")
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
