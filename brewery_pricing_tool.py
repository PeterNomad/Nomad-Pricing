"""
Brewery Pricing & Forecast Tool
"""

import math, json, os
from datetime import datetime
import streamlit as st
import pandas as pd

st.set_page_config(
    page_title="Brewery Pricing Tool",
    page_icon="🍺",
    layout="wide",
    initial_sidebar_state="expanded",
)

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
    try:
        with open(SETTINGS_FILE, "w") as f: json.dump(_gi_to_json(gi), f, indent=2)
        with open(BEERS_FILE,    "w") as f: json.dump(beers, f, indent=2)
        return True
    except Exception as e:
        return str(e)

def load_settings():
    gi, beers = None, None
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f: gi = _gi_from_json(json.load(f))
        except Exception: gi = None
    if os.path.exists(BEERS_FILE):
        try:
            with open(BEERS_FILE) as f: beers = json.load(f)
        except Exception: beers = None
    return gi or default_general_inputs(), beers or default_beers()

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f: return json.load(f)
        except Exception: pass
    return []

def save_history(history):
    try:
        with open(HISTORY_FILE, "w") as f: json.dump(history, f, indent=2)
    except Exception: pass

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
], label_visibility="collapsed")

# Sidebar: show active excise period
st.sidebar.markdown("---")
st.sidebar.caption(f"Active excise period: **{st.session_state.gi.get('active_excise_period','')}**")


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
        "can_size_l": wi_can_size, "proportion_cans": wi_can_prop,
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
        st.warning(f"⚠️ You are about to delete snapshot **{snap['label']}**. Tick to confirm:")
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
