"""
Brewery Pricing & Forecast Tool
Replaces the Excel workbook with a fast, clean Python/Streamlit app.
"""

import math
import streamlit as st
import pandas as pd

# ─────────────────────────────────────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Brewery Pricing Tool",
    page_icon="🍺",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def abv_class(abv: float) -> str:
    if abv < 0.03:
        return "<3%"
    elif abv <= 0.035:
        return "3%<=ABV<=3.5%"
    else:
        return "ABV>3.5%"

def excise_rate(pkg: str, abv: float, rates: dict) -> float:
    """Return excise rate ($/L of pure alcohol) for package + ABV combo."""
    cls = abv_class(abv)
    return rates.get((pkg, cls), 0.0)

def ethanol_per_package(abv: float, pkg_size_l: float) -> float:
    """
    Australian excise rule:
    - subtract 1.15% from actual ABV
    - multiply by package size to get litres of pure alcohol
    """
    adjusted_abv = abv - 0.0115
    return adjusted_abv * pkg_size_l

def excise_per_package(pkg_type: str, abv: float, pkg_size_l: float, rates: dict) -> float:
    """Excise duty per package in $."""
    pkg_key = "Can" if pkg_type in ("Can", "4-Pack", "Case") else "Keg"
    rate = excise_rate(pkg_key, abv, rates)
    return rate * ethanol_per_package(abv, pkg_size_l)

def packaging_cost_per_can(pak_tech: bool, can_size_l: float, inputs: dict) -> float:
    """Total packaging cost per can."""
    p = inputs["packaging"]
    pak_tech_cost = p["pak_tech_per_can"] if pak_tech else 0.0
    return p["printed_can"] + p["can_lid"] + pak_tech_cost + p["carton_per_can"] + p["return_and_earn"]

def cost_per_litre(
    raw_mat_per_batch: float,
    batch_size_l: float,
    can_prop: float,
    keg_prop: float,
    can_size_l: float,
    keg_size_l: float,
    pak_tech: bool,
    abv: float,
    fixed_cost_per_l: float,
    excise_rates: dict,
    packaging_inputs: dict,
) -> dict:
    """
    Returns a dict of cost components per litre for cans and kegs.
    """
    raw_mat_per_l = raw_mat_per_batch / batch_size_l if batch_size_l > 0 else 0

    # Can volumes
    can_batch_l = batch_size_l * can_prop
    n_cans = can_batch_l / can_size_l if can_size_l > 0 else 0

    # Keg volumes
    keg_batch_l = batch_size_l * keg_prop
    n_kegs = keg_batch_l / keg_size_l if keg_size_l > 0 else 0

    # Packaging costs per can
    p = packaging_inputs
    pkg_can = p["printed_can"] + p["can_lid"] + (p["pak_tech_per_can"] if pak_tech else 0) + p["carton_per_can"] + p["return_and_earn"]
    pkg_can_per_l = pkg_can / can_size_l if can_size_l > 0 else 0

    pkg_keg_per_l = p["keg_cost"] / keg_size_l if keg_size_l > 0 else 0

    # Return & Earn is can-only, already included in pkg_can
    r_and_e_per_l_can = p["return_and_earn"] / can_size_l if can_size_l > 0 else 0
    r_and_e_per_l_keg = 0

    return {
        "raw_mat_per_l": raw_mat_per_l,
        "fixed_cost_per_l": fixed_cost_per_l,
        "pkg_can_per_l": pkg_can_per_l,
        "pkg_keg_per_l": pkg_keg_per_l,
        "r_and_e_per_l_can": r_and_e_per_l_can,
        "r_and_e_per_l_keg": r_and_e_per_l_keg,
        "pkg_can_unit": pkg_can,
        "pkg_keg_unit": p["keg_cost"],
    }


def compute_pricing(beer: dict, gi: dict) -> list[dict]:
    """
    Compute sell price, cost, and margin for all channels & package types.
    Returns a list of row dicts.
    """
    abv = beer["abv"]
    batch_size = beer["batch_size_l"]
    can_size = beer["can_size_l"]
    keg_size = beer["keg_size_l"]
    can_prop = beer["proportion_cans"]
    keg_prop = beer["proportion_kegs"]
    raw_mat = beer["raw_materials"]
    target_margin = beer["base_margin"]
    royalty_pct = beer["royalty_pct"]
    pak_tech = beer["pak_tech"]
    cans_per_case = beer["cans_per_case"]

    excise_rates = gi["excise_rates"]
    fixed_cost_per_l = gi["fixed_cost_per_l"]
    p = gi["packaging"]
    channel_discounts = gi["channel_discounts"]

    raw_mat_per_l = raw_mat / batch_size if batch_size > 0 else 0

    # Packaging per can — R&E is tracked SEPARATELY, not included here
    pkg_can = (
        p["printed_can"] + p["can_lid"]
        + (p["pak_tech_per_can"] if pak_tech else 0)
        + p["carton_per_can"]
    )
    pkg_keg = p["keg_cost"]  # full keg fitting cost per keg

    rows = []

    def make_row(channel, pkg_type, pkg_size_l, pkg_cost_total,
                 r_and_e_per_pkg, excise_per_pkg, discount):
        cost_per_pkg = (
            excise_per_pkg
            + fixed_cost_per_l * pkg_size_l
            + r_and_e_per_pkg
            + pkg_cost_total
            + raw_mat_per_l * pkg_size_l
        )
        # Step 1: sell price to achieve target margin (before royalty)
        sell_ex_royalty = cost_per_pkg / (1 - target_margin) if (1 - target_margin) > 0 else 0
        # Step 2: gross up for royalty — royalty is a % of the FINAL sell price
        sell_inc_royalty = sell_ex_royalty / (1 - royalty_pct) if royalty_pct > 0 else sell_ex_royalty
        royalty_per_pkg = sell_inc_royalty - sell_ex_royalty
        # Step 3: apply channel/pack discount — always multiplicative (1 - discount)
        # Tap Room discounts are negative (e.g. -2.25), so price multiplies UP
        sell_price = sell_inc_royalty * (1 - discount)
        sell_price_rounded = round(sell_price)
        margin_dollar = sell_price_rounded - cost_per_pkg
        margin_pct = margin_dollar / sell_price_rounded if sell_price_rounded > 0 else 0
        return {
            "Beer": beer["name"],
            "Channel": channel,
            "Package": pkg_type,
            "ABV": abv,
            "Package Size (L)": pkg_size_l,
            "Excise ($)": round(excise_per_pkg, 4),
            "Fixed Cost ($)": round(fixed_cost_per_l * pkg_size_l, 4),
            "R&E ($)": round(r_and_e_per_pkg, 4),
            "Packaging ($)": round(pkg_cost_total, 4),
            "Raw Materials ($)": round(raw_mat_per_l * pkg_size_l, 4),
            "Cost ($)": round(cost_per_pkg, 4),
            "Sell Price ($)": sell_price_rounded,
            "Margin ($)": round(margin_dollar, 4),
            "Margin %": round(margin_pct * 100, 2),
            "Royalty ($)": round(royalty_per_pkg, 4),
        }

    # ── Package definitions ───────────────────────────────────────────────
    # Can (single) — R&E passed separately
    can_exc = excise_per_package("Can", abv, can_size, excise_rates)
    row_can = make_row("Retail + Online", "Can", can_size, pkg_can, p["return_and_earn"], can_exc,
                       channel_discounts.get(("Retail + Online", "Can"), 0))
    rows.append(row_can)

    # 4-Pack
    pack4_exc = can_exc * 4
    row_4pack = make_row("Retail + Online", "4-Pack", can_size * 4, pkg_can * 4, p["return_and_earn"] * 4, pack4_exc,
                         channel_discounts.get(("Retail + Online", "4-Pack"), 0))
    rows.append(row_4pack)

    # Case
    case_exc = can_exc * cans_per_case
    row_case = make_row("Retail + Online", "Case", can_size * cans_per_case,
                        pkg_can * cans_per_case, p["return_and_earn"] * cans_per_case, case_exc,
                        channel_discounts.get(("Retail + Online", "Case"), 0))
    rows.append(row_case)

    # Retail Keg — no R&E for kegs
    keg_exc = excise_per_package("Keg", abv, keg_size, excise_rates)
    row_keg_retail = make_row("Retail + Online", "Keg", keg_size, pkg_keg, 0, keg_exc,
                              channel_discounts.get(("Retail + Online", "Keg"), 0))
    rows.append(row_keg_retail)

    # Wholesale Case
    row_ws_case = make_row("Wholesale", "Case", can_size * cans_per_case,
                           pkg_can * cans_per_case, p["return_and_earn"] * cans_per_case, case_exc,
                           channel_discounts.get(("Wholesale", "Case"), 0))
    rows.append(row_ws_case)

    # Wholesale Keg
    row_ws_keg = make_row("Wholesale", "Keg", keg_size, pkg_keg, 0, keg_exc,
                          channel_discounts.get(("Wholesale", "Keg"), 0))
    rows.append(row_ws_keg)

    # Tap Room: all serves come from a keg, so keg packaging cost is allocated per litre of serve.
    # keg_pkg_per_l = keg_cost / keg_size  (e.g. $24.52 / 50L = $0.4904/L)
    keg_pkg_per_l = pkg_keg / keg_size if keg_size > 0 else 0
    for tap_pkg, tap_size_l in [("Middy", 0.285), ("Schooner", 0.425), ("Pint", 0.568), ("Jug", 1.14)]:
        tap_exc = excise_per_package("Keg", abv, tap_size_l, excise_rates)
        tap_pkg_cost = keg_pkg_per_l * tap_size_l   # keg cost pro-rated to serve size
        disc = channel_discounts.get(("Tap Room", tap_pkg), 0)
        row_tap = make_row("Tap Room", tap_pkg, tap_size_l, tap_pkg_cost, 0, tap_exc, disc)
        rows.append(row_tap)

    return rows


# ─────────────────────────────────────────────────────────────────────────────
# SESSION STATE DEFAULTS
# ─────────────────────────────────────────────────────────────────────────────
def default_general_inputs():
    return {
        "excise_rates": {
            # Period 1 (current)
            ("Can",  "<3%"):            52.87,
            ("Can",  "3%<=ABV<=3.5%"):  61.57,
            ("Can",  "ABV>3.5%"):       61.57,
            ("Keg",  "<3%"):            10.57,
            ("Keg",  "3%<=ABV<=3.5%"):  33.11,
            ("Keg",  "ABV>3.5%"):       43.39,
        },
        "excise_rates_2": {
            # Period 2 (next)
            ("Can",  "<3%"):            53.72,
            ("Can",  "3%<=ABV<=3.5%"):  62.56,
            ("Can",  "ABV>3.5%"):       62.56,
            ("Keg",  "<3%"):            10.57,
            ("Keg",  "3%<=ABV<=3.5%"):  33.11,
            ("Keg",  "ABV>3.5%"):       43.39,
        },
        "excise_period": 1,  # which period to use
        "fixed_costs": {
            "rent": 170_000,
            "brewers": 162_060,
            "power": 30_000,
            "hire_et_al": 50_000,
            "rm": 50_000,
        },
        "annual_production_l": 168_417,
        "keg_size_l": 50,
        "packaging": {
            "printed_can":    0.44,
            "can_lid":        0.065,
            "pak_tech_per_can": 0.0695,   # when pak-tech used, per can
            "carton_per_can": 0.06,
            "return_and_earn": 0.15,
            "keg_cost":       24.52,
        },
        "channel_discounts": {
            # Positive = % discount from full retail price  |  Tap Room = $ value adjustments
            ("Retail + Online", "Can"):    0.00,
            ("Retail + Online", "4-Pack"): 0.10,
            ("Retail + Online", "Case"):   0.20,
            ("Retail + Online", "Keg"):    0.00,
            ("Wholesale", "Case"):         0.25,
            ("Wholesale", "Keg"):          0.35,
            ("Tap Room", "Middy"):        -2.25,   # negative = customer price ABOVE cost-based
            ("Tap Room", "Schooner"):     -1.80,
            ("Tap Room", "Pint"):         -1.70,
            ("Tap Room", "Jug"):          -1.60,
        },
    }

def default_beers():
    return [
        {"name": "Rainbow Cherry",       "abv": 0.060, "batch_size_l": 2300, "can_size_l": 0.440, "proportion_cans": 0.65, "proportion_kegs": 0.35, "cans_per_case": 16, "keg_size_l": 50, "pak_tech": False, "raw_materials": 3820,   "base_margin": 0.37, "royalty_pct": 0.10, "active": True},
        {"name": "Rainbow Sherbet",      "abv": 0.060, "batch_size_l": 2300, "can_size_l": 0.375, "proportion_cans": 0.50, "proportion_kegs": 0.50, "cans_per_case": 24, "keg_size_l": 50, "pak_tech": True,  "raw_materials": 2757,   "base_margin": 0.37, "royalty_pct": 0.10, "active": True},
        {"name": "Queensie Lager",       "abv": 0.050, "batch_size_l": 2500, "can_size_l": 0.375, "proportion_cans": 0.50, "proportion_kegs": 0.50, "cans_per_case": 16, "keg_size_l": 50, "pak_tech": True,  "raw_materials": 1752.51,"base_margin": 0.37, "royalty_pct": 0.00, "active": True},
        {"name": "Surf Mist Hazy",       "abv": 0.041, "batch_size_l": 2200, "can_size_l": 0.375, "proportion_cans": 0.50, "proportion_kegs": 0.50, "cans_per_case": 16, "keg_size_l": 50, "pak_tech": True,  "raw_materials": 2115,   "base_margin": 0.37, "royalty_pct": 0.00, "active": True},
        {"name": "Hop Symphony Ale",     "abv": 0.054, "batch_size_l": 2200, "can_size_l": 0.375, "proportion_cans": 0.50, "proportion_kegs": 0.50, "cans_per_case": 16, "keg_size_l": 50, "pak_tech": True,  "raw_materials": 1667.15,"base_margin": 0.37, "royalty_pct": 0.00, "active": True},
        {"name": "Monsoon IPA",          "abv": 0.064, "batch_size_l": 2000, "can_size_l": 0.375, "proportion_cans": 0.50, "proportion_kegs": 0.50, "cans_per_case": 16, "keg_size_l": 50, "pak_tech": True,  "raw_materials": 2069.87,"base_margin": 0.37, "royalty_pct": 0.00, "active": True},
        {"name": "Mountains Cold IPA",   "abv": 0.063, "batch_size_l": 2200, "can_size_l": 0.375, "proportion_cans": 0.50, "proportion_kegs": 0.50, "cans_per_case": 16, "keg_size_l": 50, "pak_tech": True,  "raw_materials": 2575.58,"base_margin": 0.37, "royalty_pct": 0.00, "active": True},
        {"name": "Budgy Pale Ale",       "abv": 0.050, "batch_size_l": 2400, "can_size_l": 0.375, "proportion_cans": 0.50, "proportion_kegs": 0.50, "cans_per_case": 16, "keg_size_l": 50, "pak_tech": False, "raw_materials": 1470.08,"base_margin": 0.37, "royalty_pct": 0.00, "active": True},
        {"name": "Coastal Mid Strength", "abv": 0.033, "batch_size_l": 2600, "can_size_l": 0.375, "proportion_cans": 0.50, "proportion_kegs": 0.50, "cans_per_case": 16, "keg_size_l": 50, "pak_tech": True,  "raw_materials": 1083.44,"base_margin": 0.37, "royalty_pct": 0.00, "active": True},
        {"name": "Baroness Red IPA",     "abv": 0.065, "batch_size_l": 2000, "can_size_l": 0.375, "proportion_cans": 0.50, "proportion_kegs": 0.50, "cans_per_case": 16, "keg_size_l": 50, "pak_tech": True,  "raw_materials": 1851.07,"base_margin": 0.37, "royalty_pct": 0.00, "active": True},
        {"name": "Gweilo Raspberry Waffle","abv": 0.060,"batch_size_l": 2250,"can_size_l": 0.440,"proportion_cans": 0.50,"proportion_kegs": 0.50,"cans_per_case": 16,"keg_size_l": 50,"pak_tech": False,"raw_materials": 5134.37,"base_margin": 0.37,"royalty_pct": 0.10,"active": True},
        {"name": "Pier 39 WC IPA",       "abv": 0.067, "batch_size_l": 2200, "can_size_l": 0.375, "proportion_cans": 0.50, "proportion_kegs": 0.50, "cans_per_case": 16, "keg_size_l": 50, "pak_tech": True,  "raw_materials": 2157.36,"base_margin": 0.37, "royalty_pct": 0.00, "active": True},
        {"name": "New Beer 1",           "abv": 0.050, "batch_size_l": 2000, "can_size_l": 0.375, "proportion_cans": 0.50, "proportion_kegs": 0.50, "cans_per_case": 16, "keg_size_l": 50, "pak_tech": True,  "raw_materials": 1500,   "base_margin": 0.37, "royalty_pct": 0.00, "active": False},
    ]

# ─────────────────────────────────────────────────────────────────────────────
# PERSISTENCE — save/load settings to JSON files alongside the app
# ─────────────────────────────────────────────────────────────────────────────
import json, os

SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "brewery_settings.json")
BEERS_FILE    = os.path.join(os.path.dirname(__file__), "brewery_beers.json")

def _gi_to_json(gi: dict) -> dict:
    """Convert gi dict (with tuple keys) to JSON-safe format."""
    out = {}
    out["excise_rates"]   = {"|".join(k): v for k, v in gi["excise_rates"].items()}
    out["excise_rates_2"] = {"|".join(k): v for k, v in gi.get("excise_rates_2", {}).items()}
    out["excise_period"]  = gi.get("excise_period", 1)
    out["fixed_costs"]    = gi["fixed_costs"]
    out["annual_production_l"] = gi["annual_production_l"]
    out["packaging"]      = gi["packaging"]
    out["channel_discounts"] = {"|".join(k): v for k, v in gi["channel_discounts"].items()}
    return out

def _gi_from_json(d: dict) -> dict:
    """Restore gi dict with tuple keys from JSON."""
    gi = {}
    gi["excise_rates"]   = {tuple(k.split("|")): v for k, v in d["excise_rates"].items()}
    gi["excise_rates_2"] = {tuple(k.split("|")): v for k, v in d.get("excise_rates_2", {}).items()}
    gi["excise_period"]  = d.get("excise_period", 1)
    gi["fixed_costs"]    = d["fixed_costs"]
    gi["annual_production_l"] = d["annual_production_l"]
    gi["packaging"]      = d["packaging"]
    gi["channel_discounts"] = {tuple(k.split("|")): v for k, v in d["channel_discounts"].items()}
    return gi

def save_settings(gi: dict, beers: list):
    """Write current inputs to disk."""
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(_gi_to_json(gi), f, indent=2)
        with open(BEERS_FILE, "w") as f:
            json.dump(beers, f, indent=2)
        return True
    except Exception as e:
        return str(e)

def load_settings():
    """Load inputs from disk; fall back to defaults if files not found."""
    gi, beers = None, None
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                gi = _gi_from_json(json.load(f))
        except Exception:
            gi = None
    if os.path.exists(BEERS_FILE):
        try:
            with open(BEERS_FILE) as f:
                beers = json.load(f)
        except Exception:
            beers = None
    return gi or default_general_inputs(), beers or default_beers()

if "beers" not in st.session_state or "gi" not in st.session_state:
    gi, beers = load_settings()
    st.session_state.gi    = gi
    st.session_state.beers = beers


# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR – NAVIGATION
# ─────────────────────────────────────────────────────────────────────────────
st.sidebar.title("🍺 Brewery Pricing Tool")
page = st.sidebar.radio(
    "Navigate",
    ["📊 Price Lookup", "📋 Price Lists", "⚙️ General Inputs", "🍺 Beer Inputs"],
    label_visibility="collapsed",
)

# ─────────────────────────────────────────────────────────────────────────────
# COMPUTE ALL PRICING
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data
def compute_all(beers_tuple, gi_frozen):
    import json
    gi = json.loads(gi_frozen)
    # Reconstruct tuple keys for excise rates
    gi["excise_rates"] = {tuple(k.split("|")): v for k, v in gi["excise_rates"].items()}
    gi["channel_discounts"] = {tuple(k.split("|")): v for k, v in gi["channel_discounts"].items()}
    beers = [dict(b) for b in beers_tuple]
    all_rows = []
    for beer in beers:
        if not beer.get("active", True):
            continue
        if beer["batch_size_l"] <= 0 or beer["abv"] <= 0:
            continue
        try:
            rows = compute_pricing(beer, gi)
            all_rows.extend(rows)
        except Exception:
            pass
    return pd.DataFrame(all_rows)

def gi_to_frozen(gi: dict) -> str:
    import json
    gi_copy = {}
    gi_copy["excise_rates"] = {"|".join(k): v for k, v in gi["excise_rates"].items()}
    gi_copy["channel_discounts"] = {"|".join(k): v for k, v in gi["channel_discounts"].items()}
    gi_copy["fixed_costs"] = gi["fixed_costs"]
    gi_copy["annual_production_l"] = gi["annual_production_l"]
    gi_copy["packaging"] = gi["packaging"]
    gi_copy["fixed_cost_per_l"] = gi["fixed_cost_per_l"]
    return json.dumps(gi_copy, sort_keys=True)

# Calculate fixed cost per litre
fc = st.session_state.gi["fixed_costs"]
total_fixed = sum(fc.values())
st.session_state.gi["fixed_cost_per_l"] = total_fixed / st.session_state.gi["annual_production_l"] if st.session_state.gi["annual_production_l"] > 0 else 0

# Select excise period
period = st.session_state.gi.get("excise_period", 1)
if period == 2 and "excise_rates_2" in st.session_state.gi:
    st.session_state.gi["excise_rates"] = dict(st.session_state.gi["excise_rates_2"])

df_all = compute_all(
    tuple(tuple(sorted(b.items())) for b in st.session_state.beers),
    gi_to_frozen(st.session_state.gi)
)


# ─────────────────────────────────────────────────────────────────────────────
# PAGE: PRICE LOOKUP
# ─────────────────────────────────────────────────────────────────────────────
if page == "📊 Price Lookup":
    st.title("📊 Price Lookup")
    st.caption("Select a beer, channel, and package to see full pricing detail.")

    col1, col2, col3 = st.columns(3)
    beer_names = sorted(df_all["Beer"].unique()) if not df_all.empty else []
    channels = ["Retail + Online", "Wholesale", "Tap Room"]
    packages_by_channel = {
        "Retail + Online": ["Can", "4-Pack", "Case", "Keg"],
        "Wholesale": ["Case", "Keg"],
        "Tap Room": ["Middy", "Schooner", "Pint", "Jug"],
    }

    with col1:
        sel_beer = st.selectbox("Beer Name", beer_names if beer_names else ["No beers loaded"])
    with col2:
        sel_channel = st.selectbox("Sales Channel", channels)
    with col3:
        sel_pkg = st.selectbox("Package / Serve", packages_by_channel[sel_channel])

    if not df_all.empty and sel_beer in beer_names:
        row = df_all[
            (df_all["Beer"] == sel_beer)
            & (df_all["Channel"] == sel_channel)
            & (df_all["Package"] == sel_pkg)
        ]
        if not row.empty:
            r = row.iloc[0]
            st.markdown("---")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Sell Price", f"${r['Sell Price ($)']:.2f}")
            c2.metric("Cost", f"${r['Cost ($)']:.2f}")
            c3.metric("Margin $", f"${r['Margin ($)']:.2f}")
            c4.metric("Margin %", f"{r['Margin %']:.1f}%")

            st.markdown("##### Cost Breakdown")
            breakdown = pd.DataFrame({
                "Component": ["Excise", "Fixed Costs", "Return & Earn", "Packaging", "Raw Materials", "Total Cost"],
                "Amount ($)": [
                    r["Excise ($)"], r["Fixed Cost ($)"], r["R&E ($)"], r["Packaging ($)"],
                    r["Raw Materials ($)"], r["Cost ($)"]
                ],
                "% of Sell Price": [
                    r["Excise ($)"] / r["Sell Price ($)"] * 100 if r["Sell Price ($)"] else 0,
                    r["Fixed Cost ($)"] / r["Sell Price ($)"] * 100 if r["Sell Price ($)"] else 0,
                    r["R&E ($)"] / r["Sell Price ($)"] * 100 if r["Sell Price ($)"] else 0,
                    r["Packaging ($)"] / r["Sell Price ($)"] * 100 if r["Sell Price ($)"] else 0,
                    r["Raw Materials ($)"] / r["Sell Price ($)"] * 100 if r["Sell Price ($)"] else 0,
                    r["Cost ($)"] / r["Sell Price ($)"] * 100 if r["Sell Price ($)"] else 0,
                ],
            })
            breakdown["Amount ($)"] = breakdown["Amount ($)"].apply(lambda x: f"${x:.4f}")
            breakdown["% of Sell Price"] = breakdown["% of Sell Price"].apply(lambda x: f"{x:.1f}%")
            st.dataframe(breakdown, hide_index=True, use_container_width=True)

            if r["Royalty ($)"] > 0:
                st.info(f"Royalty: ${r['Royalty ($)']:.4f} per package (included in sell price calculation)")

            st.markdown(f"**ABV:** {r['ABV']*100:.1f}%  |  **Package size:** {r['Package Size (L)']:.3f} L  |  **Excise category:** {abv_class(r['ABV'])}")
        else:
            st.warning("No pricing found for this combination.")


# ─────────────────────────────────────────────────────────────────────────────
# PAGE: PRICE LISTS
# ─────────────────────────────────────────────────────────────────────────────
elif page == "📋 Price Lists":
    st.title("📋 Price Lists")
    tab1, tab2, tab3 = st.tabs(["🛒 Retail + Online", "🚚 Wholesale", "🍺 Tap Room"])

    def price_table(df, channel, pkgs):
        sub = df[df["Channel"] == channel][["Beer", "ABV", "Package", "Cost ($)", "Margin ($)", "Margin %", "Sell Price ($)"]]
        sub = sub[sub["Package"].isin(pkgs)]
        pivot_price = sub.pivot_table(index=["Beer", "ABV"], columns="Package", values="Sell Price ($)", aggfunc="first")
        pivot_margin = sub.pivot_table(index=["Beer", "ABV"], columns="Package", values="Margin %", aggfunc="first")
        pivot_cost = sub.pivot_table(index=["Beer", "ABV"], columns="Package", values="Cost ($)", aggfunc="first")
        pivot_price = pivot_price.reindex(columns=[p for p in pkgs if p in pivot_price.columns])
        pivot_margin = pivot_margin.reindex(columns=[p for p in pkgs if p in pivot_margin.columns])
        return pivot_price, pivot_margin, pivot_cost

    with tab1:
        if not df_all.empty:
            p_price, p_margin, p_cost = price_table(df_all, "Retail + Online", ["Can", "4-Pack", "Case", "Keg"])
            st.subheader("Sell Prices ($)")
            st.dataframe(p_price.style.format("${:.2f}"), use_container_width=True)
            st.subheader("Margin %")
            st.dataframe(p_margin.style.format("{:.1f}%"), use_container_width=True)
            csv = df_all[df_all["Channel"] == "Retail + Online"].to_csv(index=False)
            st.download_button("⬇️ Download Retail CSV", csv, "retail_prices.csv", "text/csv")

    with tab2:
        if not df_all.empty:
            p_price, p_margin, p_cost = price_table(df_all, "Wholesale", ["Case", "Keg"])
            st.subheader("Sell Prices ($)")
            st.dataframe(p_price.style.format("${:.2f}"), use_container_width=True)
            st.subheader("Margin %")
            st.dataframe(p_margin.style.format("{:.1f}%"), use_container_width=True)
            csv = df_all[df_all["Channel"] == "Wholesale"].to_csv(index=False)
            st.download_button("⬇️ Download Wholesale CSV", csv, "wholesale_prices.csv", "text/csv")

    with tab3:
        if not df_all.empty:
            p_price, p_margin, p_cost = price_table(df_all, "Tap Room", ["Middy", "Schooner", "Pint", "Jug"])
            st.subheader("Sell Prices ($)")
            st.dataframe(p_price.style.format("${:.2f}"), use_container_width=True)
            st.subheader("Margin %")
            st.dataframe(p_margin.style.format("{:.1f}%"), use_container_width=True)
            csv = df_all[df_all["Channel"] == "Tap Room"].to_csv(index=False)
            st.download_button("⬇️ Download Tap Room CSV", csv, "taproom_prices.csv", "text/csv")

    st.markdown("---")
    st.subheader("Full Pricing Export")
    if not df_all.empty:
        full_csv = df_all.to_csv(index=False)
        st.download_button("⬇️ Download All Pricing (CSV)", full_csv, "all_pricing.csv", "text/csv")


# ─────────────────────────────────────────────────────────────────────────────
# PAGE: GENERAL INPUTS
# ─────────────────────────────────────────────────────────────────────────────
elif page == "⚙️ General Inputs":
    st.title("⚙️ General Inputs")
    st.info("These inputs apply to all beers. Changes take effect immediately.")

    gi = st.session_state.gi

    # ── Excise Rates ─────────────────────────────────────────────────────────
    st.subheader("Australian Excise Rates ($/L of pure alcohol)")
    st.caption("Updated every 6 months by the ATO. Source: [ATO Excise Duty Rates](https://www.ato.gov.au/businesses-and-organisations/income-deductions-and-concessions/excise-duty/excise-duty-rates)")

    excise_period = st.radio("Excise Period", [1, 2], horizontal=True,
                             index=gi.get("excise_period", 1) - 1,
                             format_func=lambda x: f"Period {x}")
    gi["excise_period"] = excise_period

    rates_key = "excise_rates" if excise_period == 1 else "excise_rates_2"
    rates = gi.get(rates_key, gi["excise_rates"])

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Cans**")
        rates[("Can", "<3%")] = st.number_input("Can < 3% ABV ($/L)", value=float(rates.get(("Can","<3%"), 52.87)), step=0.01, key=f"can_lt3_{excise_period}")
        rates[("Can", "3%<=ABV<=3.5%")] = st.number_input("Can 3–3.5% ABV ($/L)", value=float(rates.get(("Can","3%<=ABV<=3.5%"), 61.57)), step=0.01, key=f"can_mid_{excise_period}")
        rates[("Can", "ABV>3.5%")] = st.number_input("Can > 3.5% ABV ($/L)", value=float(rates.get(("Can","ABV>3.5%"), 61.57)), step=0.01, key=f"can_gt35_{excise_period}")
    with col2:
        st.markdown("**Kegs**")
        rates[("Keg", "<3%")] = st.number_input("Keg < 3% ABV ($/L)", value=float(rates.get(("Keg","<3%"), 10.57)), step=0.01, key=f"keg_lt3_{excise_period}")
        rates[("Keg", "3%<=ABV<=3.5%")] = st.number_input("Keg 3–3.5% ABV ($/L)", value=float(rates.get(("Keg","3%<=ABV<=3.5%"), 33.11)), step=0.01, key=f"keg_mid_{excise_period}")
        rates[("Keg", "ABV>3.5%")] = st.number_input("Keg > 3.5% ABV ($/L)", value=float(rates.get(("Keg","ABV>3.5%"), 43.39)), step=0.01, key=f"keg_gt35_{excise_period}")
    gi[rates_key] = rates
    if excise_period == 1:
        gi["excise_rates"] = rates
    else:
        gi["excise_rates_2"] = rates

    st.markdown("---")

    # ── Fixed Costs ──────────────────────────────────────────────────────────
    st.subheader("Fixed Costs (Annual, $)")
    fc = gi["fixed_costs"]
    c1, c2, c3 = st.columns(3)
    with c1:
        fc["rent"] = st.number_input("Rent", value=int(fc["rent"]), step=1000)
        fc["brewers"] = st.number_input("Brewers (Wages)", value=int(fc["brewers"]), step=1000)
    with c2:
        fc["power"] = st.number_input("Power", value=int(fc["power"]), step=500)
        fc["hire_et_al"] = st.number_input("Hire et al", value=int(fc["hire_et_al"]), step=500)
    with c3:
        fc["rm"] = st.number_input("Repairs & Maintenance", value=int(fc["rm"]), step=500)
    total_fc = sum(fc.values())
    annual_prod = st.number_input("Annual Production (L)", value=int(gi["annual_production_l"]), step=1000)
    gi["annual_production_l"] = annual_prod
    fcp_l = total_fc / annual_prod if annual_prod > 0 else 0
    gi["fixed_cost_per_l"] = fcp_l
    st.metric("Total Fixed Costs", f"${total_fc:,.0f}")
    st.metric("Fixed Cost per Litre", f"${fcp_l:.4f}")

    st.markdown("---")

    # ── Packaging Costs ──────────────────────────────────────────────────────
    st.subheader("Packaging Costs ($ per unit)")
    p = gi["packaging"]
    c1, c2, c3 = st.columns(3)
    with c1:
        p["printed_can"] = st.number_input("Printed Can", value=float(p["printed_can"]), step=0.001, format="%.4f")
        p["can_lid"] = st.number_input("Can Lid", value=float(p["can_lid"]), step=0.001, format="%.4f")
    with c2:
        p["pak_tech_per_can"] = st.number_input("Pak-Tech (per can when used)", value=float(p["pak_tech_per_can"]), step=0.001, format="%.4f")
        p["carton_per_can"] = st.number_input("Carton (per can)", value=float(p["carton_per_can"]), step=0.001, format="%.4f")
    with c3:
        p["return_and_earn"] = st.number_input("Return & Earn (per can)", value=float(p["return_and_earn"]), step=0.01, format="%.3f")
        p["keg_cost"] = st.number_input("Keg cost ($)", value=float(p["keg_cost"]), step=0.01, format="%.2f")
    total_can_pkg = p["printed_can"] + p["can_lid"] + p["carton_per_can"]
    st.info(f"Packaging per can (excl. Pak-Tech & R&E): ${total_can_pkg:.4f}  |  With Pak-Tech: ${total_can_pkg + p['pak_tech_per_can']:.4f}  |  Note: R&E is tracked as a separate cost line")

    st.markdown("---")

    # ── Channel Discounts ────────────────────────────────────────────────────
    st.subheader("Channel / Pack Size Adjustments")
    st.caption("Retail/Wholesale: % discount from full price. Tap Room: $ adjustment (negative = customer pays more than cost-based price).")
    cd = gi["channel_discounts"]
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**Retail + Online**")
        cd[("Retail + Online", "Can")]    = st.number_input("Can (%)", value=float(cd.get(("Retail + Online","Can"), 0)), step=0.01, format="%.2f", key="ro_can")
        cd[("Retail + Online", "4-Pack")] = st.number_input("4-Pack (%)", value=float(cd.get(("Retail + Online","4-Pack"), 0.10)), step=0.01, format="%.2f", key="ro_4p")
        cd[("Retail + Online", "Case")]   = st.number_input("Case (%)", value=float(cd.get(("Retail + Online","Case"), 0.20)), step=0.01, format="%.2f", key="ro_case")
        cd[("Retail + Online", "Keg")]    = st.number_input("Keg (%)", value=float(cd.get(("Retail + Online","Keg"), 0)), step=0.01, format="%.2f", key="ro_keg")
    with c2:
        st.markdown("**Wholesale**")
        cd[("Wholesale", "Case")] = st.number_input("Case (%)", value=float(cd.get(("Wholesale","Case"), 0.25)), step=0.01, format="%.2f", key="ws_case")
        cd[("Wholesale", "Keg")]  = st.number_input("Keg (%)", value=float(cd.get(("Wholesale","Keg"), 0.35)), step=0.01, format="%.2f", key="ws_keg")
    with c3:
        st.markdown("**Tap Room ($)**")
        cd[("Tap Room", "Middy")]    = st.number_input("Middy ($)", value=float(cd.get(("Tap Room","Middy"), -2.25)), step=0.05, format="%.2f", key="tr_middy")
        cd[("Tap Room", "Schooner")] = st.number_input("Schooner ($)", value=float(cd.get(("Tap Room","Schooner"), -1.80)), step=0.05, format="%.2f", key="tr_sch")
        cd[("Tap Room", "Pint")]     = st.number_input("Pint ($)", value=float(cd.get(("Tap Room","Pint"), -1.70)), step=0.05, format="%.2f", key="tr_pint")
        cd[("Tap Room", "Jug")]      = st.number_input("Jug ($)", value=float(cd.get(("Tap Room","Jug"), -1.60)), step=0.05, format="%.2f", key="tr_jug")

    st.session_state.gi = gi
    compute_all.clear()

    st.markdown("---")
    st.subheader("💾 Save Settings")
    st.caption("Changes to inputs above are applied immediately for this session. Click Save to persist them across refreshes.")
    col_save, col_reset, _ = st.columns([1, 1, 4])
    with col_save:
        if st.button("💾 Save All Settings", type="primary", use_container_width=True):
            result = save_settings(st.session_state.gi, st.session_state.beers)
            if result is True:
                st.success("✅ Settings saved — will persist after refresh.")
            else:
                st.error(f"Save failed: {result}")
    with col_reset:
        if st.button("↩️ Reset to Defaults", use_container_width=True):
            st.session_state.gi    = default_general_inputs()
            st.session_state.beers = default_beers()
            compute_all.clear()
            st.success("Reset to factory defaults.")
            st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# PAGE: BEER INPUTS
# ─────────────────────────────────────────────────────────────────────────────
elif page == "🍺 Beer Inputs":
    st.title("🍺 Beer Inputs")
    st.caption("Add, edit, or deactivate beers. All prices recalculate instantly.")

    beers = st.session_state.beers

    # ── Add new beer ─────────────────────────────────────────────────────────
    with st.expander("➕ Add a New Beer"):
        with st.form("add_beer"):
            c1, c2, c3 = st.columns(3)
            new_name = c1.text_input("Beer Name", "New Beer")
            new_abv  = c2.number_input("ABV", value=0.050, min_value=0.0, max_value=0.20, step=0.001, format="%.3f")
            new_batch = c3.number_input("Batch Size (L)", value=2000, step=100)
            c1, c2, c3 = st.columns(3)
            new_can_size = c1.number_input("Can Size (L)", value=0.375, step=0.005, format="%.3f")
            new_can_prop = c2.number_input("Proportion Cans", value=0.50, min_value=0.0, max_value=1.0, step=0.05)
            new_keg_prop = c3.number_input("Proportion Kegs", value=0.50, min_value=0.0, max_value=1.0, step=0.05)
            c1, c2, c3 = st.columns(3)
            new_cpc = c1.number_input("Cans per Case", value=16, step=1)
            new_raw = c2.number_input("Raw Materials ($/batch)", value=1500.0, step=50.0)
            new_margin = c3.number_input("Target Margin %", value=37.0, step=1.0) / 100
            c1, c2, c3 = st.columns(3)
            new_royalty = c1.number_input("Royalty %", value=0.0, step=1.0) / 100
            new_pak_tech = c2.checkbox("Uses Pak-Tech")
            submitted = st.form_submit_button("Add Beer")
            if submitted:
                beers.append({
                    "name": new_name, "abv": new_abv, "batch_size_l": new_batch,
                    "can_size_l": new_can_size, "proportion_cans": new_can_prop,
                    "proportion_kegs": new_keg_prop, "cans_per_case": new_cpc,
                    "keg_size_l": 50, "pak_tech": new_pak_tech, "raw_materials": new_raw,
                    "base_margin": new_margin, "royalty_pct": new_royalty, "active": True
                })
                st.success(f"Added {new_name}!")
                compute_all.clear()
                save_settings(st.session_state.gi, beers)
                st.rerun()

    st.markdown("---")

    # ── Edit existing beers ──────────────────────────────────────────────────
    for i, beer in enumerate(beers):
        label = f"{'✅' if beer['active'] else '⬜'} {beer['name']}  (ABV {beer['abv']*100:.1f}%)"
        with st.expander(label):
            with st.form(f"beer_{i}"):
                c1, c2, c3 = st.columns(3)
                beer["name"]        = c1.text_input("Beer Name", value=beer["name"], key=f"n_{i}")
                beer["abv"]         = c2.number_input("ABV", value=beer["abv"], min_value=0.0, max_value=0.20, step=0.001, format="%.3f", key=f"abv_{i}")
                beer["batch_size_l"]= c3.number_input("Batch Size (L)", value=int(beer["batch_size_l"]), step=100, key=f"bs_{i}")
                c1, c2, c3 = st.columns(3)
                beer["can_size_l"]     = c1.number_input("Can Size (L)", value=beer["can_size_l"], step=0.005, format="%.3f", key=f"cs_{i}")
                beer["proportion_cans"]= c2.number_input("Proportion Cans", value=beer["proportion_cans"], min_value=0.0, max_value=1.0, step=0.05, key=f"pc_{i}")
                beer["proportion_kegs"]= c3.number_input("Proportion Kegs", value=beer["proportion_kegs"], min_value=0.0, max_value=1.0, step=0.05, key=f"pk_{i}")
                c1, c2, c3 = st.columns(3)
                beer["cans_per_case"]  = c1.number_input("Cans per Case", value=int(beer["cans_per_case"]), step=1, key=f"cpc_{i}")
                beer["raw_materials"]  = c2.number_input("Raw Materials ($/batch)", value=float(beer["raw_materials"]), step=50.0, key=f"rm_{i}")
                beer["base_margin"]    = c3.number_input("Target Margin %", value=beer["base_margin"]*100, step=1.0, key=f"bm_{i}") / 100
                c1, c2, c3 = st.columns(3)
                beer["royalty_pct"]= c1.number_input("Royalty %", value=beer["royalty_pct"]*100, step=1.0, key=f"rp_{i}") / 100
                beer["pak_tech"]   = c2.checkbox("Uses Pak-Tech", value=beer["pak_tech"], key=f"pt_{i}")
                beer["active"]     = c3.checkbox("Active (include in pricing)", value=beer["active"], key=f"act_{i}")

                c1, c2 = st.columns([1, 5])
                save = c1.form_submit_button("💾 Save")
                if save:
                    beers[i] = beer
                    compute_all.clear()
                    save_settings(st.session_state.gi, beers)
                    st.success("Saved!")
                    st.rerun()

            if st.button(f"🗑️ Delete {beer['name']}", key=f"del_{i}"):
                beers.pop(i)
                compute_all.clear()
                save_settings(st.session_state.gi, beers)
                st.rerun()

    st.session_state.beers = beers
