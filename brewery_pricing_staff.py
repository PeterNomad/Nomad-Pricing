"""
Brewery Pricing Tool — Staff (Read-Only)
Price lookup and price lists only. No editing capability.
Reads settings from the same JSON files as the main app.
"""

import math, json, os
import streamlit as st
import pandas as pd

st.set_page_config(
    page_title="Nomad Brewing — Price Lookup",
    page_icon="🍺",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────────────────────
# PASSWORD GATE
# Set APP_PASSWORD in Streamlit Cloud secrets (Manage app → Settings → Secrets)
# ─────────────────────────────────────────────────────────────────────────────
_CORRECT_PASSWORD = st.secrets.get("APP_PASSWORD", "nomad2024") if hasattr(st, "secrets") else "nomad2024"

if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

if not st.session_state["authenticated"]:
    st.markdown("## 🍺 Nomad Brewing — Price Lookup")
    st.markdown("Please enter the password to access pricing.")
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
# CORE CALCULATION HELPERS (copied from main app)
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

    excise_rates      = gi["excise_rates"]
    fixed_cost_per_l  = gi["fixed_cost_per_l"]
    p                 = gi["packaging"]
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
        sell_final = sell_inc * (1 - discount)
        sell_rnd   = round(sell_final)
        margin_dollar = sell_rnd - cost
        margin_pct    = margin_dollar / sell_rnd if sell_rnd > 0 else 0
        return {
            "Beer": beer["name"], "Channel": channel, "Package": pkg_type,
            "ABV": abv, "Package Size (L)": pkg_size_l,
            "Cost ($)":       round(cost, 4),
            "Sell Price ($)": sell_rnd,
            "Margin ($)":     round(margin_dollar, 4),
            "Margin %":       round(margin_pct * 100, 2),
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
# LOAD SETTINGS — reads the same files saved by the main app
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "brewery_settings.json")
BEERS_FILE    = os.path.join(BASE_DIR, "brewery_beers.json")

def _cd_from_json(d):
    return {tuple(k.split("|")): v for k, v in d.items()}

def _ep_from_json(d):
    return {ds: {tuple(k.split("|")): v for k, v in rates.items()} for ds, rates in d.items()}

def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            d = json.load(f)
        gi = {
            "excise_periods":       _ep_from_json(d.get("excise_periods", {})),
            "active_excise_period": d.get("active_excise_period", ""),
            "excise_rates":        {tuple(k.split("|")): v for k, v in d["excise_rates"].items()},
            "fixed_costs":          d["fixed_costs"],
            "annual_production_l":  d["annual_production_l"],
            "packaging":            d["packaging"],
            "channel_discounts":    _cd_from_json(d["channel_discounts"]),
        }
    except Exception:
        st.error("Could not load settings file. Please ask your administrator to check the app setup.")
        st.stop()
    try:
        with open(BEERS_FILE) as f:
            beers = json.load(f)
    except Exception:
        st.error("Could not load beers file. Please ask your administrator to check the app setup.")
        st.stop()
    return gi, beers

gi, beers = load_settings()

# Calculate fixed cost per litre
fc = gi["fixed_costs"]
gi["fixed_cost_per_l"] = sum(fc.values()) / gi["annual_production_l"] if gi["annual_production_l"] > 0 else 0

# Sync active excise rates
aep     = gi.get("active_excise_period", "")
periods = gi.get("excise_periods", {})
if aep in periods:
    gi["excise_rates"] = periods[aep]


# ─────────────────────────────────────────────────────────────────────────────
# COMPUTE ALL PRICING
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
        "excise_rates":       {"|".join(k): v for k, v in gi["excise_rates"].items()},
        "channel_discounts":  {"|".join(k): v for k, v in gi["channel_discounts"].items()},
        "fixed_costs":         gi["fixed_costs"],
        "annual_production_l": gi["annual_production_l"],
        "packaging":           gi["packaging"],
        "fixed_cost_per_l":    gi["fixed_cost_per_l"],
    }, sort_keys=True)

df_all = compute_all(
    tuple(tuple(sorted(b.items())) for b in beers),
    gi_to_frozen(gi)
)


# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────────────────────
st.sidebar.title("🍺 Price Lookup")
page = st.sidebar.radio("Navigate", [
    "📊 Price Lookup", "📋 Price Lists",
], label_visibility="collapsed")

st.sidebar.markdown("---")
st.sidebar.caption(f"Excise period: **{aep}**")
st.sidebar.caption("*Read-only — contact your manager to update pricing.*")


# ─────────────────────────────────────────────────────────────────────────────
# PAGE: PRICE LOOKUP
# ─────────────────────────────────────────────────────────────────────────────
if page == "📊 Price Lookup":
    st.title("📊 Price Lookup")
    st.caption("Select a beer, channel, and package to see the sell price.")

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
        row = df_all[
            (df_all["Beer"]==sel_beer) &
            (df_all["Channel"]==sel_channel) &
            (df_all["Package"]==sel_pkg)
        ]
        if not row.empty:
            r = row.iloc[0]
            st.markdown("---")
            c1, c2, _ = st.columns([1, 1, 2])
            c1.metric("Sell Price", f"${r['Sell Price ($)']:.2f}")
            c2.metric("Margin %",   f"{r['Margin %']:.1f}%")
            st.markdown(
                f"**ABV:** {r['ABV']*100:.1f}%  |  "
                f"**Package size:** {r['Package Size (L)']:.3f} L  |  "
                f"**Excise period:** {aep}"
            )
        else:
            st.warning("No pricing found for this combination.")


# ─────────────────────────────────────────────────────────────────────────────
# PAGE: PRICE LISTS
# ─────────────────────────────────────────────────────────────────────────────
elif page == "📋 Price Lists":
    st.title("📋 Price Lists")
    tab1, tab2, tab3 = st.tabs(["🛒 Retail + Online", "🚚 Wholesale", "🍺 Tap Room"])

    def price_table(df, channel, pkgs):
        sub = df[df["Channel"]==channel][["Beer","ABV","Package","Sell Price ($)","Margin %"]]
        sub = sub[sub["Package"].isin(pkgs)]
        pp  = sub.pivot_table(index=["Beer","ABV"], columns="Package", values="Sell Price ($)", aggfunc="first")
        pm  = sub.pivot_table(index=["Beer","ABV"], columns="Package", values="Margin %",       aggfunc="first")
        return (pp.reindex(columns=[p for p in pkgs if p in pp.columns]),
                pm.reindex(columns=[p for p in pkgs if p in pm.columns]))

    with tab1:
        if not df_all.empty:
            pp, pm = price_table(df_all, "Retail + Online", ["Can","4-Pack","Case","Keg"])
            st.subheader("Sell Prices ($)")
            st.dataframe(pp.style.format("${:.2f}"), use_container_width=True)
            st.subheader("Margin %")
            st.dataframe(pm.style.format("{:.1f}%"), use_container_width=True)
            st.download_button(
                "⬇️ Download CSV",
                df_all[df_all["Channel"]=="Retail + Online"][["Beer","Package","Sell Price ($)","Margin %"]].to_csv(index=False),
                "retail_prices.csv", "text/csv"
            )

    with tab2:
        if not df_all.empty:
            pp, pm = price_table(df_all, "Wholesale", ["Case","Keg"])
            st.subheader("Sell Prices ($)")
            st.dataframe(pp.style.format("${:.2f}"), use_container_width=True)
            st.subheader("Margin %")
            st.dataframe(pm.style.format("{:.1f}%"), use_container_width=True)
            st.download_button(
                "⬇️ Download CSV",
                df_all[df_all["Channel"]=="Wholesale"][["Beer","Package","Sell Price ($)","Margin %"]].to_csv(index=False),
                "wholesale_prices.csv", "text/csv"
            )

    with tab3:
        if not df_all.empty:
            pp, pm = price_table(df_all, "Tap Room", ["Middy","Schooner","Pint","Jug"])
            st.subheader("Sell Prices ($)")
            st.dataframe(pp.style.format("${:.2f}"), use_container_width=True)
            st.subheader("Margin %")
            st.dataframe(pm.style.format("{:.1f}%"), use_container_width=True)
            st.download_button(
                "⬇️ Download CSV",
                df_all[df_all["Channel"]=="Tap Room"][["Beer","Package","Sell Price ($)","Margin %"]].to_csv(index=False),
                "taproom_prices.csv", "text/csv"
            )
