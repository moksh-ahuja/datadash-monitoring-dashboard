import streamlit as st
import pandas as pd
import mysql.connector
import concurrent.futures
from datetime import date, timedelta
import warnings
warnings.filterwarnings('ignore')

# ── PAGE CONFIG ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="DataDash Monitor", page_icon="📊", layout="wide")

st.markdown("""
<style>
  section.main > div { padding-top: 0.5rem; }
  .sec { font-size:.78rem; font-weight:700; color:#6e7681; text-transform:uppercase;
         letter-spacing:.07em; margin:1.5rem 0 .6rem; padding-bottom:5px;
         border-bottom:2px solid #21262d; }
  .role-hdr { font-size:.85rem; font-weight:600; color:#c9d1d9;
              margin:.8rem 0 .2rem; padding:6px 12px;
              background:#161b22; border-left:3px solid #58a6ff; border-radius:3px; }
  table.ovr { width:100%; border-collapse:collapse; font-size:.79rem; }
  table.ovr th { background:#161b22; color:#8b949e; padding:8px 11px;
                 border:1px solid #30363d; text-align:center; white-space:nowrap; }
  table.ovr th.lh { text-align:left; min-width:200px; background:#0d1117; }
  table.ovr th.abm { background:#1c2333; color:#79c0ff; }
  table.ovr td { padding:7px 11px; border:1px solid #21262d;
                 text-align:center; color:#c9d1d9; white-space:nowrap; }
  table.ovr td.lh { text-align:left; font-weight:500; color:#e6edf3;
                    background:#0d1117; padding-left:16px; }
  table.ovr td.grp { text-align:left; font-weight:700; color:#58a6ff;
                     background:#0d1117; font-size:.72rem; text-transform:uppercase;
                     letter-spacing:.04em; padding-left:8px; }
  table.ovr tr:hover td { background:#161b22 !important; }
  div[data-testid="metric-container"] {
    background:#0d1117; border:1px solid #21262d;
    border-radius:8px; padding:12px 16px; }
  [data-testid="stSidebar"] { background:#0d1117; border-right:1px solid #21262d; }
</style>
""", unsafe_allow_html=True)

# ── DB ────────────────────────────────────────────────────────────────────────
DB = dict(
    host='datadash-restore-1.c5k0ah8qcmmb.ap-south-1.rds.amazonaws.com',
    database='dash-development', user='moksh_ahuja',
    password='moksh_ahuja#wetg', port=3306, connection_timeout=30
)

# The 4 dashboards we care about (URL slug → display label)
DASH_MAP = {
    'demography':    'Demography',
    'geography':     'Geography',
    'past-elections':'Past Elections',
    'surveys':       'Surveys',
}
DASH_SLUGS  = list(DASH_MAP.keys())       # for SQL IN clause
DASH_LABELS = list(DASH_MAP.values())     # ordered display cols

@st.cache_data(ttl=300, show_spinner=False)
def Q(sql: str) -> pd.DataFrame:
    try:
        c = mysql.connector.connect(**DB)
        df = pd.read_sql(sql, c); c.close(); return df
    except Exception as e:
        st.error(f"DB: {e}"); return pd.DataFrame()

@st.cache_data(ttl=3600, show_spinner=False)
def get_abm_states() -> pd.DataFrame:
    return Q("""
        SELECT state, abbreviation
        FROM candidateProfiles_candidatesstateabbreviation
        WHERE is_abm = 1 AND is_active = 1
        ORDER BY state
    """)

@st.cache_data(ttl=300, show_spinner=False)
def load_data(s: str, e: str, min_sec: int, min_active_days: int) -> dict:
    slugs_sql = "'" + "','".join(DASH_SLUGS) + "'"   # e.g. 'demography','geography',...

    qs = {
        # All non-dev users with their first group role
        'user_base': """
            SELECT au.id, au.username, up.state,
                   COALESCE(MIN(ag.name), 'Unknown') AS role
            FROM auth_user au
            JOIN authentication_userprofile up ON au.id = up.user_id
            LEFT JOIN auth_user_groups aug ON au.id = aug.user_id
            LEFT JOIN auth_group ag        ON aug.group_id = ag.id
            WHERE au.is_active = 1 AND up.is_dev_user = 0
            GROUP BY au.id, au.username, up.state
        """,

        # Per-user session aggregates for period
        'session_stats': f"""
            SELECT sm.user_id AS id,
                   ROUND(SUM(sm.duration) / 3600, 2) AS total_hrs,
                   COUNT(sm.id)                       AS total_sessions,
                   ROUND(AVG(sm.duration) / 60, 1)   AS avg_session_min
            FROM userAnalytics_sessionmanager sm
            JOIN authentication_userprofile up ON sm.user_id = up.user_id
            WHERE sm.created_date BETWEEN '{s}' AND '{e}'
              AND up.is_dev_user = 0
            GROUP BY sm.user_id
        """,

        # Active = ≥ min_sec on ≥ min_active_days WEEKDAYS (Mon–Fri, DAYOFWEEK 2–6)
        # DAYOFWEEK: 1=Sun, 2=Mon, …, 6=Fri, 7=Sat  → exclude 1 and 7
        'active_users': f"""
            SELECT DISTINCT user_id FROM (
                SELECT user_id, COUNT(*) AS qualifying_wdays
                FROM (
                    SELECT sm.user_id, sm.created_date,
                           SUM(sm.duration) AS day_total
                    FROM userAnalytics_sessionmanager sm
                    JOIN authentication_userprofile up ON sm.user_id = up.user_id
                    WHERE sm.created_date BETWEEN '{s}' AND '{e}'
                      AND DAYOFWEEK(sm.created_date) NOT IN (1, 7)
                      AND up.is_dev_user = 0
                    GROUP BY sm.user_id, sm.created_date
                    HAVING day_total >= {min_sec}
                ) weekday_sessions
                GROUP BY user_id
                HAVING qualifying_wdays >= {min_active_days}
            ) qualified
        """,

        # Dashboard time – extract slug at depth-4 OR depth-5 (handles both URL structures):
        #   /main/dashboards/demography                     → depth-4 = demography ✓
        #   /main/dashboards/Uttar Pradesh/past-elections   → depth-4 = "Uttar Pradesh",
        #                                                     depth-5 = "past-elections" ✓
        'dashboard_time': f"""
            SELECT sm.user_id AS id,
                   CASE
                     WHEN LOWER(SUBSTRING_INDEX(SUBSTRING_INDEX(pv.page,'/',4),'/',-1))
                          IN ({slugs_sql})
                       THEN LOWER(SUBSTRING_INDEX(SUBSTRING_INDEX(pv.page,'/',4),'/',-1))
                     WHEN LOWER(SUBSTRING_INDEX(SUBSTRING_INDEX(pv.page,'/',5),'/',-1))
                          IN ({slugs_sql})
                       THEN LOWER(SUBSTRING_INDEX(SUBSTRING_INDEX(pv.page,'/',5),'/',-1))
                     ELSE NULL
                   END AS slug,
                   ROUND(SUM(pv.duration) / 3600, 2) AS hours
            FROM userAnalytics_pageview pv
            JOIN userAnalytics_sessionmanager sm ON pv.session_id = sm.id
            JOIN authentication_userprofile up  ON sm.user_id = up.user_id
            WHERE pv.page LIKE '/main/dashboards/%'
              AND pv.created_date BETWEEN '{s}' AND '{e}'
              AND up.is_dev_user = 0
            GROUP BY sm.user_id, slug
            HAVING slug IS NOT NULL
        """,

        # Events created per user
        'events_per_user': f"""
            SELECT created_by_id AS id, COUNT(*) AS events_created
            FROM stateDashboard_eventdiary
            WHERE DATE(date_of_creation) BETWEEN '{s}' AND '{e}'
              AND is_active = 1
            GROUP BY created_by_id
        """,

        # Candidates created per user
        'candidates_per_user': f"""
            SELECT created_by_id AS id, COUNT(*) AS candidates_created
            FROM acDashboard_candidatemaster
            WHERE DATE(date_of_creation) BETWEEN '{s}' AND '{e}'
              AND is_active = 1
            GROUP BY created_by_id
        """,

        # Peak DAU day (excluding dev users via join)
        'peak_dau': f"""
            SELECT sm.created_date AS dt, COUNT(DISTINCT sm.user_id) AS dau
            FROM userAnalytics_sessionmanager sm
            JOIN authentication_userprofile up ON sm.user_id = up.user_id
            WHERE sm.created_date BETWEEN '{s}' AND '{e}'
              AND up.is_dev_user = 0
            GROUP BY sm.created_date
            ORDER BY dau DESC LIMIT 1
        """,

        # Longest single session
        'max_session': f"""
            SELECT ROUND(MAX(sm.duration) / 60, 0) AS max_min
            FROM userAnalytics_sessionmanager sm
            JOIN authentication_userprofile up ON sm.user_id = up.user_id
            WHERE sm.created_date BETWEEN '{s}' AND '{e}'
              AND up.is_dev_user = 0
        """,
    }

    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        fut = {ex.submit(Q, sql): k for k, sql in qs.items()}
        for f in concurrent.futures.as_completed(fut):
            out[fut[f]] = f.result()
    return out


# ── SESSION STATE ─────────────────────────────────────────────────────────────
if 'sd' not in st.session_state:
    st.session_state.sd = date.today() - timedelta(days=6)
if 'ed' not in st.session_state:
    st.session_state.ed = date.today()


# ═══════════════════════════════════════════════════════
#  SIDEBAR
# ═══════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("## 🎛️ Controls")
    st.divider()

    # ── Date range ────────────────────────────────────
    st.markdown("**📅 Date Range**")
    b1, b2, b3 = st.columns(3)
    if b1.button("7d",  use_container_width=True):
        st.session_state.sd = date.today() - timedelta(6)
        st.session_state.ed = date.today(); st.rerun()
    if b2.button("15d", use_container_width=True):
        st.session_state.sd = date.today() - timedelta(14)
        st.session_state.ed = date.today(); st.rerun()
    if b3.button("30d", use_container_width=True):
        st.session_state.sd = date.today() - timedelta(29)
        st.session_state.ed = date.today(); st.rerun()

    sd_val = st.date_input("From", value=st.session_state.sd,
                            key='sd_inp', label_visibility='collapsed')
    ed_val = st.date_input("To",   value=st.session_state.ed,
                            key='ed_inp', label_visibility='collapsed')

    # Sync manual edits
    if sd_val != st.session_state.sd or ed_val != st.session_state.ed:
        st.session_state.sd = sd_val
        st.session_state.ed = ed_val
        st.rerun()

    START = st.session_state.sd
    END   = st.session_state.ed

    if START > END:
        st.error("'From' must be ≤ 'To'"); st.stop()

    # Total weekdays (Mon–Fri) in selected period
    total_wdays = sum(
        1 for i in range((END - START).days + 1)
        if (START + timedelta(days=i)).weekday() < 5
    )
    st.caption(f"{START}  →  {END} · **{total_wdays} weekdays**")

    st.divider()

    # ── Active User Definition ────────────────────────
    st.markdown("**👤 Active User Definition**")
    st.caption("Saturdays & Sundays are always excluded.")

    min_min = st.slider(
        "Min minutes per active day",
        min_value=5, max_value=120, value=15, step=5,
        help="A day counts as 'active' only if the user's total time that day ≥ this threshold."
    )
    min_days = st.slider(
        f"Min active weekdays (of {total_wdays})",
        min_value=1, max_value=max(1, total_wdays),
        value=min(3, max(1, total_wdays)),
        help="How many qualifying weekdays a user must have to be counted as Active."
    )
    min_sec = min_min * 60

    st.info(
        f"**Active** = ≥ {min_min} min on ≥ {min_days} of {total_wdays} weekday(s)"
    )

    st.divider()

    if st.button("🔄 Refresh Data", use_container_width=True):
        st.cache_data.clear(); st.rerun()

    with st.spinner("…"):
        ping = Q("SELECT 1 AS ok")
    st.success("🟢 DB Connected") if not ping.empty else st.error("🔴 DB Offline")


# ── LOAD DATA ─────────────────────────────────────────────────────────────────
with st.spinner("Fetching data…"):
    abm_df = get_abm_states()
    data   = load_data(
        START.strftime('%Y-%m-%d'),
        END.strftime('%Y-%m-%d'),
        min_sec, min_days
    )

abm_states = abm_df['state'].tolist() if not abm_df.empty else []

# ── BUILD MERGED DATAFRAME ────────────────────────────────────────────────────
users  = data.get('user_base',          pd.DataFrame())
ss     = data.get('session_stats',      pd.DataFrame())
active = data.get('active_users',       pd.DataFrame())
dash_t = data.get('dashboard_time',     pd.DataFrame())
ev_pu  = data.get('events_per_user',    pd.DataFrame())
ca_pu  = data.get('candidates_per_user',pd.DataFrame())

if users.empty:
    st.error("No user data returned. Check DB connection."); st.stop()

active_set = set(active['user_id'].tolist()) if not active.empty else set()

# Session stats already uses 'id' as column name (aliased in SQL)
merged = users.merge(ss, on='id', how='left')
merged['is_active_p']     = merged['id'].isin(active_set)
merged['total_hrs']       = merged['total_hrs'].fillna(0.0)
merged['total_sessions']  = merged['total_sessions'].fillna(0).astype(int)
merged['avg_session_min'] = merged['avg_session_min'].fillna(0.0)
merged['state']           = merged['state'].fillna('').str.strip()

# Dashboard pivot → columns are display labels (Demography, Geography, …)
AVAIL_DASH = []   # will hold whichever DASH_LABELS are present in data
if not dash_t.empty:
    dash_t = dash_t[dash_t['slug'].notna()].copy()
    dash_t['label'] = dash_t['slug'].map(DASH_MAP)
    dash_t = dash_t[dash_t['label'].notna()]

    dpivot = dash_t.pivot_table(
        index='id', columns='label',
        values='hours', aggfunc='sum', fill_value=0
    ).reset_index()

    # Keep consistent order (only those present in data)
    AVAIL_DASH = [lbl for lbl in DASH_LABELS if lbl in dpivot.columns]
    merged = merged.merge(dpivot[['id'] + AVAIL_DASH], on='id', how='left')
    for c in AVAIL_DASH:
        merged[c] = merged[c].fillna(0.0)

# Map events/candidates via dict (avoids merge column conflicts)
def map_col(src_df: pd.DataFrame, col: str) -> pd.Series:
    if not src_df.empty and col in src_df.columns:
        m = src_df.set_index('id')[col].to_dict()
        return merged['id'].map(m).fillna(0).astype(int)
    return pd.Series(0, index=merged.index)

merged['events_created']    = map_col(ev_pu,  'events_created')
merged['candidates_created']= map_col(ca_pu,  'candidates_created')


# ── KPI HELPER ────────────────────────────────────────────────────────────────
def kpis(df: pd.DataFrame) -> dict:
    if df.empty:
        return dict(total=0, active=0, total_hrs=0,
                    avg_hrs=0, avg_sess=0, avg_dur=0,
                    events=0, candidates=0)
    w = df[df['total_hrs'] > 0]
    return dict(
        total      = len(df),
        active     = int(df['is_active_p'].sum()),
        total_hrs  = round(df['total_hrs'].sum(), 1),
        avg_hrs    = round(w['total_hrs'].mean(), 1)        if len(w) else 0,
        avg_sess   = int(round(w['total_sessions'].mean())) if len(w) else 0,
        avg_dur    = round(w['avg_session_min'].mean(), 1)  if len(w) else 0,
        events     = int(df['events_created'].sum()),
        candidates = int(df['candidates_created'].sum()),
    )

ZERO_DASH = ('avg_hrs', 'avg_sess', 'avg_dur')   # show '—' when 0

def cell(v, key: str) -> str:
    if v == 0 and key in ZERO_DASH: return '—'
    return str(v)


# ═══════════════════════════════════════════════════════
#  HEADER + TOP METRICS
# ═══════════════════════════════════════════════════════
st.title("📊 DataDash Product Monitor")
st.caption(
    f"**{START}** → **{END}**  ·  "
    f"Active = ≥ {min_min} min on ≥ {min_days} weekday(s)  ·  Sat/Sun excluded"
)

all_k    = kpis(merged)
peak_dau = data.get('peak_dau', pd.DataFrame())
max_sess = data.get('max_session', pd.DataFrame())

peak_str = (f"{int(peak_dau['dau'].iloc[0])} · {peak_dau['dt'].iloc[0]}"
            if not peak_dau.empty else "—")
max_str  = (f"{int(max_sess['max_min'].iloc[0])} min"
            if not max_sess.empty and not pd.isna(max_sess['max_min'].iloc[0]) else "—")

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Total Users",         f"{all_k['total']:,}")
m2.metric("Active Users",        f"{all_k['active']:,}")
m3.metric("Total Hours",         f"{all_k['total_hrs']:,}")
m4.metric("Peak DAU",            peak_str)
m5.metric("Max Session",         max_str)


# ═══════════════════════════════════════════════════════
#  SECTION 1: OVERALL USER SUMMARY
# ═══════════════════════════════════════════════════════
st.markdown('<div class="sec">1. Overall User Summary · ABM States</div>',
            unsafe_allow_html=True)

abm_merged  = merged[merged['state'].isin(abm_states)]
overall_map = {'Total': kpis(merged), 'ABM States': kpis(abm_merged)}
for sn in abm_states:
    overall_map[sn] = kpis(merged[merged['state'] == sn])

ROWS = [
    ('User Activity',               None,         True),
    ('Total Users',                 'total',      False),
    ('Total Active Users',          'active',     False),
    ('User Engagement',             None,         True),
    ('Avg Time / User (hrs)',       'avg_hrs',    False),
    ('Avg Sessions / User',         'avg_sess',   False),
    ('Avg Session Duration (min)',  'avg_dur',    False),
    ('Content Creation',            None,         True),
    ('Events Created',              'events',     False),
    ('Candidates Created',          'candidates', False),
]

cols_order = list(overall_map.keys())
html = '<table class="ovr"><thead><tr><th class="lh">KPIs</th>'
for c in cols_order:
    html += f'<th {"class=abm" if c == "ABM States" else ""}>{c}</th>'
html += '</tr></thead><tbody>'

for label, key, is_hdr in ROWS:
    if is_hdr:
        html += f'<tr><td class="grp" colspan="{len(cols_order)+1}">{label}</td></tr>'
    else:
        html += f'<tr><td class="lh">{label}</td>'
        for c in cols_order:
            html += f'<td>{cell(overall_map[c].get(key, "—"), key)}</td>'
        html += '</tr>'

html += '</tbody></table>'
st.markdown(html, unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════
#  SECTION 2: STATE-WISE USER SUMMARY
# ═══════════════════════════════════════════════════════
st.markdown('<div class="sec">2. State-wise User Summary</div>', unsafe_allow_html=True)

if not abm_states:
    st.warning("No ABM states in DB."); st.stop()

sel_state = st.selectbox("State", abm_states, key='state_sel')

# Only inverv.com users for the client-facing state view
state_users = merged[
    (merged['state'] == sel_state) &
    (merged['username'].str.lower().str.endswith('inverv.com', na=False))
].copy()

n_total  = len(state_users)
n_active = int(state_users['is_active_p'].sum()) if not state_users.empty else 0
st.caption(
    f"**{sel_state}**  ·  {n_total} inverv.com users  ·  {n_active} active this period"
)

if state_users.empty:
    st.info(f"No inverv.com users found for {sel_state}.")
    st.stop()

# ── Column spec for per-role tables ──────────────────────
BASE = ['username', 'is_active_p', 'total_hrs', 'total_sessions', 'avg_session_min']
RMAP = {
    'username':        'Username',
    'is_active_p':     'Active',
    'total_hrs':       'Hours',
    'total_sessions':  'Sessions',
    'avg_session_min': 'Avg Session (min)',
}
for lbl in AVAIL_DASH:
    BASE.append(lbl)
    RMAP[lbl] = f"📊 {lbl}"
BASE += ['events_created', 'candidates_created']
RMAP.update({
    'events_created':    'Events Created',
    'candidates_created':'Candidates Created',
})

def fmt_f(v):
    try:
        fv = float(v)
        return f"{fv:.1f}" if fv > 0 else '—'
    except:
        return '—'

# ── One sub-table per role group ─────────────────────────
roles_present = sorted(state_users['role'].dropna().unique())

for role in roles_present:
    rdf = state_users[state_users['role'] == role].copy()
    nr  = len(rdf)
    na  = int(rdf['is_active_p'].sum())

    st.markdown(
        f'<div class="role-hdr">'
        f'{role} &nbsp;·&nbsp; {nr} users &nbsp;·&nbsp; {na} active'
        f'</div>',
        unsafe_allow_html=True
    )

    avail = [c for c in BASE if c in rdf.columns]
    disp  = rdf[avail].rename(columns=RMAP).copy()

    disp['Active']            = disp['Active'].map({True: '✅', False: '—'})
    disp['Hours']             = disp['Hours'].apply(fmt_f)
    disp['Avg Session (min)'] = disp['Avg Session (min)'].apply(fmt_f)
    for lbl in AVAIL_DASH:
        col_name = f"📊 {lbl}"
        if col_name in disp.columns:
            disp[col_name] = disp[col_name].apply(fmt_f)

    disp = disp.sort_values('Username')

    col_cfg = {
        'Username': st.column_config.TextColumn(width='medium'),
        'Active':   st.column_config.TextColumn(width='small'),
        'Sessions': st.column_config.NumberColumn(format="%d"),
        'Events Created':     st.column_config.NumberColumn(format="%d"),
        'Candidates Created': st.column_config.NumberColumn(format="%d"),
    }
    st.dataframe(disp, use_container_width=True, hide_index=True, column_config=col_cfg)
