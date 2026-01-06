import re
import time
from io import BytesIO
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
import streamlit as st

# ---------------- Constants / Regex ----------------

DEFAULT_HEADERS = {
    "User-Agent": "Inovestor-WG-Directory/0.2.6",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

TEAM_PAGE_TEXT_PAT = re.compile(r"\b(our team|notre équipe|team members|membres de l[' ]équipe)\b", re.I)
CONTACT_PAGE_TEXT_PAT = re.compile(r"\b(contact|contactez-nous|nous joindre|communiqu|communicat)\b", re.I)

EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"(\+?\d[\d\-\s().]{7,}\d)")
POSTAL_CA_RE = re.compile(r"\b[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z][ -]?\d[ABCEGHJ-NPRSTV-Z]\d\b", re.I)

BANNED_WORDS = set("""
contact communiquer communique contactez nous joindre
approach commitment services service produits product planning planification patrimoine
privabanque bio biographie team accueil home
wealth investment community partners partner
successoraux fiduciaires fondée founded savoir plus visitez visit
""".split())

PARTICLES = set(["de","du","des","la","le","da","di","del","della","van","von","der","den","st","ste","saint","sainte","mc","mac","o'"])

# Expanded role vocab helps avoid "role = surname" mistakes and improves precision
ROLE_WORDS = {
    "senior","branch","administrator","admin","assistant","associate","advisor","adviser",
    "manager","director","president","vp","vice","consultant","specialist","partner",
    "investment","portfolio","financial","wealth","planner","planning",
    "conseiller","conseillère","placement","gestionnaire","directeur","président","adjointe","adjoint"
}

JUNK_PHRASES = {
    "our branch team","notre équipe de succursale","our team","notre équipe"
}

# ---------------- Requests session ----------------

SESSION = requests.Session()
SESSION.headers.update(DEFAULT_HEADERS)

def polite_get(url: str, sleep_s: float = 0.75, timeout: int = 25, retries: int = 3):
    """Polite GET with retry/backoff (helps stability on some sites)."""
    time.sleep(max(0.0, sleep_s))
    last_err = None
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=timeout, allow_redirects=True)
            r.raise_for_status()
            return r.text, r.url
        except Exception as e:
            last_err = e
            time.sleep(1.25 * (attempt + 1))
    raise last_err

# ---------------- Name cleaning / validation ----------------

def clean_person_name(raw: str) -> str:
    s = str(raw or "")
    s = s.replace("\u00A0", " ").replace("’", "'")      # normalize apostrophe + nbsp
    s = re.sub(r"\([^)]*\)", "", s)                    # remove nicknames in ( )
    s = s.strip()
    if "," in s:
        s = s.split(",", 1)[0].strip()                 # drop credentials after comma
    s = re.sub(r"[^A-Za-zÀ-ÿ\-\s'\.]", "", s)          # remove weird symbols
    s = re.sub(r"\s{2,}", " ", s).strip(" -–—|")
    return s

def is_valid_person_name(raw: str) -> bool:
    s = clean_person_name(raw)
    if not s or re.search(r"\d", s):
        return False

    tokens = s.split()
    if len(tokens) < 2 or len(tokens) > 4:
        return False

    low_tokens = [t.lower().strip(".") for t in tokens]
    if any(t in BANNED_WORDS for t in low_tokens):
        return False

    # require at least 2 Capitalized tokens (allow particles like "de", "van")
    caps = 0
    for t in tokens:
        tl = t.lower().strip(".")
        if tl in PARTICLES:
            continue
        if re.match(r"^[A-ZÀ-Ý]", t):
            caps += 1
        else:
            return False
    return caps >= 2

def canon_name(raw: str) -> str:
    return re.sub(r"[^a-z]+", "", clean_person_name(raw).lower())

# ---------------- Role extraction fixes ----------------

def _canon(s: str) -> str:
    return re.sub(r"[^a-z]+", "", (s or "").lower())

def is_likely_role(text: str, person_name: str = "") -> bool:
    if not text:
        return False
    t = re.sub(r"\s+", " ", text).strip(" -|•·")
    if len(t) < 3 or len(t) > 90:
        return False
    if t.lower() in JUNK_PHRASES:
        return False
    if EMAIL_RE.search(t) or PHONE_RE.search(t):
        return False

    # reject if it's basically the name or pieces of it (prevents "role = last name")
    if person_name and _canon(t) == _canon(person_name):
        return False
    if person_name:
        name_tokens = set(re.findall(r"[A-Za-zÀ-ÿ']+", person_name.lower()))
        role_tokens = set(re.findall(r"[A-Za-zÀ-ÿ']+", t.lower()))
        if role_tokens and role_tokens.issubset(name_tokens):
            return False

    toks = re.findall(r"[A-Za-zÀ-ÿ']+", t.lower())
    return any(tok in ROLE_WORDS for tok in toks)

def normalize_name(raw: str) -> str:
    s = re.sub(r"\s+", " ", (raw or "")).strip()
    if "," in s:
        s = s.split(",", 1)[0].strip()
    return s

def extract_role_near_heading(h):
    """
    Fixes the bug where the role becomes part of the name (often surname),
    due to headings being split across multiple lines in the DOM.
    """
    person = normalize_name(h.get_text(" ", strip=True))
    name_canon = _canon(person)

    # 1) Prefer short role-ish sibling elements right after the heading
    for sib in h.find_next_siblings(limit=6):
        txt = sib.get_text(" ", strip=True)
        if is_likely_role(txt, person):
            return txt

    # 2) Fallback: search within parent text, allowing the name to be split across lines
    parent = h.parent
    if not parent:
        return ""

    lines = [x.strip() for x in parent.get_text("\n", strip=True).split("\n") if x.strip()]

    idx = -1
    for i in range(len(lines)):
        for j in range(i, min(len(lines), i + 4)):  # join up to 4 lines to match split names
            window = " ".join(lines[i:j+1])
            if _canon(window) == name_canon:
                idx = j
                break
        if idx != -1:
            break

    for line in lines[idx+1: idx+8]:
        if is_likely_role(line, person):
            return line

    return ""

# ---------------- URL helpers ----------------

def norm_url(u: str) -> str:
    p = urlparse(u)
    return p._replace(fragment="", query="").geturl()

def page_title(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(" ", strip=True)
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return ""

def extract_links(html: str, base_url: str):
    soup = BeautifulSoup(html, "lxml")
    out = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href:
            continue
        abs_url = norm_url(urljoin(base_url, href))
        text = a.get_text(" ", strip=True)
        out.append((text, abs_url))
    return out

def find_best_link(links, base_url: str, pattern: re.Pattern):
    # prefer shortest matching path
    candidates = []
    for text, url in links:
        if urlparse(url).netloc.lower() != urlparse(base_url).netloc.lower():
            continue
        if pattern.search(text or "") or pattern.search(url or ""):
            candidates.append((text, url))
    candidates.sort(key=lambda x: len(urlparse(x[1]).path))
    return candidates[0][1] if candidates else ""

# ---------------- CIBC Wood Gundy team-root detection ----------------

def branch_slug_from_url(url: str) -> str:
    parts = urlparse(url).path.strip("/").split("/")
    if len(parts) >= 2 and parts[0].lower() == "web":
        return parts[1].lower()
    return ""

def is_true_team_root(url: str, branch_slug: str) -> bool:
    """
    Accept only URLs like:
      https://woodgundyadvisors.cibc.com/Danisi-Financial-Group/
    i.e. exactly ONE path segment, not the branch slug.
    """
    path = urlparse(url).path.strip("/")
    if not path:
        return False
    parts = path.split("/")
    if len(parts) != 1:
        return False
    seg = parts[0].lower()
    if branch_slug and seg == branch_slug:
        return False
    if seg in {"web", "home", "contact", "services", "produits", "products"}:
        return False
    return True

# ---------------- Desjardins discovery support ----------------

DESJARDINS_TEAM_LINK_RE = re.compile(r"/find-us/desjardins-securities-team/[^/?#]+\.html$", re.I)

def is_desjardins_url(u: str) -> bool:
    return "desjardins.com" in (urlparse(u).netloc or "").lower()

def discover_desjardins_team_pages(seed_url: str, sleep_s: float):
    """
    From a Desjardins branch page, collect links to team pages like:
    /find-us/desjardins-securities-team/<team>.html
    """
    html, final_url = polite_get(seed_url, sleep_s=sleep_s)
    links = extract_links(html, final_url)

    candidates = []

    # If user pasted a team page already, include it directly
    if DESJARDINS_TEAM_LINK_RE.search(urlparse(final_url).path):
        candidates.append({
            "branch_seed_url": seed_url,
            "team_root_url": norm_url(final_url),
            "link_text": "seed"
        })

    for text, u in links:
        if not is_desjardins_url(u):
            continue

        path = urlparse(u).path
        if not DESJARDINS_TEAM_LINK_RE.search(path):
            continue

        t = (text or "").strip()

        # avoid "view profile" links if present
        if t.lower().startswith("view profile") or t.lower().startswith("voir le profil"):
            continue

        candidates.append({
            "branch_seed_url": seed_url,
            "team_root_url": norm_url(u),
            "link_text": t or u
        })

    df = pd.DataFrame(candidates)
    if df.empty:
        return pd.DataFrame(columns=["branch_seed_url", "team_root_url", "link_text"])
    return df.drop_duplicates(subset=["team_root_url"])

# ---------------- Discovery (Branch → Team roots) ----------------

def discover_team_roots_from_branch(seed_url: str, sleep_s: float):
    # ✅ Desjardins flow
    if is_desjardins_url(seed_url):
        return discover_desjardins_team_pages(seed_url, sleep_s=sleep_s)

    # ✅ CIBC Wood Gundy flow (original)
    html, final_url = polite_get(seed_url, sleep_s=sleep_s)
    links = extract_links(html, final_url)

    # If user pasted a branch page (not team list), try to find the team list page
    if "our-investment-advisors-and-their-teams" not in final_url.lower():
        for _, u in links:
            if "our-investment-advisors-and-their-teams" in (u or "").lower():
                html, final_url = polite_get(u, sleep_s=sleep_s)
                links = extract_links(html, final_url)
                break

    branch_slug = branch_slug_from_url(final_url)

    candidates = []
    for text, u in links:
        if urlparse(u).netloc.lower() != urlparse(final_url).netloc.lower():
            continue
        if is_true_team_root(u, branch_slug):
            candidates.append({"branch_seed_url": seed_url, "team_root_url": u, "link_text": text})

    df = pd.DataFrame(candidates).drop_duplicates(subset=["team_root_url"])
    return df

# ---------------- Slug + resolve pages ----------------

def to_team_slug(team_root_url: str) -> str:
    p = urlparse(team_root_url)
    host = (p.netloc or "").lower()
    parts = p.path.strip("/").split("/")

    # ✅ Desjardins: use filename without .html
    if "desjardins.com" in host and parts:
        last = parts[-1].lower().replace(".html", "")
        last = last.replace("_", "-")
        last = re.sub(r"[^a-z0-9\-]+", "-", last)
        last = re.sub(r"-{2,}", "-", last).strip("-")
        return last

    # ✅ CIBC: first segment
    seg = parts[0] if parts else ""
    seg = seg.replace("_", "-")
    seg = re.sub(r"[^A-Za-z0-9\-]+", "-", seg)
    seg = re.sub(r"-{2,}", "-", seg).strip("-")
    return seg.lower()

def resolve_team_pages(team_root_url: str, sleep_s: float):
    html_root, root_final = polite_get(team_root_url, sleep_s=sleep_s)

    # ✅ Desjardins: the team page itself contains the roster/contact blocks
    if is_desjardins_url(root_final):
        slug = to_team_slug(root_final)
        team_page = root_final
        contact_page = ""
        return html_root, root_final, team_page, contact_page, slug

    # ✅ CIBC: original logic
    links = extract_links(html_root, root_final)

    team_page = find_best_link(links, root_final, TEAM_PAGE_TEXT_PAT)
    contact_page = find_best_link(links, root_final, CONTACT_PAGE_TEXT_PAT)

    slug = to_team_slug(root_final)
    web_team_guess = f"https://woodgundyadvisors.cibc.com/web/{slug}/our-team"
    web_contact_guess = f"https://woodgundyadvisors.cibc.com/web/{slug}/contact"

    if not team_page:
        try:
            _, u = polite_get(web_team_guess, sleep_s=sleep_s)
            team_page = u
        except Exception:
            pass

    if not contact_page:
        try:
            _, u = polite_get(web_contact_guess, sleep_s=sleep_s)
            contact_page = u
        except Exception:
            pass

    return html_root, root_final, team_page, contact_page, slug

# ---------------- People extraction ----------------

def looks_like_name(name: str) -> bool:
    s = (name or "").strip()
    if not s:
        return False
    if s.lower() in JUNK_PHRASES:
        return False
    if s.isupper() and len(s.split()) >= 2:
        return False
    parts = s.split()
    if len(parts) < 2 or len(parts) > 4:
        return False
    if any(p.lower() in ROLE_WORDS for p in parts):
        return False
    if EMAIL_RE.search(s) or PHONE_RE.search(s):
        return False
    if not re.match(r"^[A-Za-zÀ-ÿ]", s):
        return False
    return True

def extract_contact_from_block(block: BeautifulSoup):
    # emails
    emails = []
    for a in block.select('a[href^="mailto:"]'):
        href = a.get("href", "")
        e = href.split("mailto:", 1)[-1].split("?", 1)[0].strip()
        if e and e not in emails:
            emails.append(e)

    if not emails:
        for m in EMAIL_RE.findall(block.get_text(" ", strip=True)):
            if m not in emails:
                emails.append(m)

    # phones
    phones = []
    for a in block.select('a[href^="tel:"]'):
        href = a.get("href", "")
        p = href.split("tel:", 1)[-1].strip()
        if p and p not in phones:
            phones.append(p)

    if not phones:
        txt = block.get_text(" ", strip=True)
        for m in PHONE_RE.findall(txt):
            m = re.sub(r"\s+", " ", m).strip()
            if m and m not in phones:
                phones.append(m)

    # address (best-effort)
    address = ""
    txt_lines = [x.strip() for x in block.get_text("\n", strip=True).split("\n") if x.strip()]
    for i, line in enumerate(txt_lines):
        if POSTAL_CA_RE.search(line):
            start = max(0, i - 2)
            end = min(len(txt_lines), i + 2)
            address = " | ".join(txt_lines[start:end])
            break

    return {
        "advisor_email": "; ".join(emails[:3]),
        "advisor_phone": "; ".join(phones[:3]),
        "advisor_address": address
    }

def extract_people_from_page(html: str, base_url: str):
    soup = BeautifulSoup(html, "lxml")
    people = []

    # ✅ Added h5 to improve capture on some sites/cards
    for h in soup.find_all(["h2", "h3", "h4", "h5"]):
        raw = h.get_text(" ", strip=True)
        name = normalize_name(raw)
        if not looks_like_name(name):
            continue

        # choose a "block" that likely contains contacts
        block = h
        for _ in range(3):
            if block.parent is None:
                break
            block = block.parent
            if block.select_one('a[href^="mailto:"]') or block.select_one('a[href^="tel:"]'):
                break

        role = extract_role_near_heading(h)
        contact = extract_contact_from_block(block)

        profile_url = ""
        a = h.find("a", href=True)
        if a:
            profile_url = norm_url(urljoin(base_url, a.get("href")))

        people.append({
            "advisor_name": name,
            "advisor_role": role,
            "advisor_profile_url": profile_url,
            **contact,
            "source": "heuristic_block"
        })

    # dedupe inside page by name+email+phone
    seen = set()
    out = []
    for p in people:
        k = (p["advisor_name"].lower(), (p["advisor_email"] or "").lower(), p["advisor_phone"] or "")
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out

def fetch_people(url: str, sleep_s: float):
    html, final_url = polite_get(url, sleep_s=sleep_s)
    people = extract_people_from_page(html, final_url)
    return people, final_url

# ---------------- Post-processing (KeyError fix + merge) ----------------

BASE_OUT_COLS = [
    "branch_seed_url","team_root_url","team_slug","team_name",
    "team_page_url","contact_page_url",
    "advisor_name","advisor_role","advisor_email","advisor_phone",
    "advisor_address","advisor_profile_url",
    "source","source_page_used"
]

def _ensure_cols(df: pd.DataFrame, cols, fill=""):
    for c in cols:
        if c not in df.columns:
            df[c] = fill
    return df

def post_process_directory(df_out: pd.DataFrame, drop_no_contact=True) -> pd.DataFrame:
    df = df_out.copy()
    df = _ensure_cols(df, BASE_OUT_COLS, fill="")

    # Clean + validate
    df["advisor_name"] = df["advisor_name"].apply(clean_person_name)
    df = df[df["advisor_name"].apply(is_valid_person_name)].copy()

    # ✅ If nothing left, return an empty DF WITH expected columns (prevents KeyError)
    if df.empty:
        return pd.DataFrame(columns=BASE_OUT_COLS)

    df["name_key"] = df["advisor_name"].apply(canon_name)

    def score_row(r):
        score = 0
        for c in ["advisor_email","advisor_phone","advisor_address"]:
            v = str(r.get(c) or "").strip()
            if v:
                score += 1

        role = str(r.get("advisor_role") or "").strip()
        if is_likely_role(role, str(r.get("advisor_name") or "")):
            score += 1
        return score

    df["_score"] = df.apply(score_row, axis=1)
    df = df.sort_values("_score", ascending=False)

    merged_rows = []
    for (team, nk), g in df.groupby(["team_root_url", "name_key"], sort=False):
        base = g.iloc[0].to_dict()

        for col in ["advisor_role","advisor_email","advisor_phone","advisor_address","advisor_profile_url"]:
            vals = [v for v in g[col].tolist() if pd.notna(v) and str(v).strip() != ""]

            if col == "advisor_role":
                nm = base.get("advisor_name", "")
                vals = [v for v in vals if is_likely_role(str(v), nm)]

            base[col] = vals[0] if vals else ""

        merged_rows.append(base)

    out = pd.DataFrame(merged_rows)
    out = _ensure_cols(out, BASE_OUT_COLS, fill="")
    out = out.drop(columns=["name_key","_score"], errors="ignore")

    if drop_no_contact:
        out = out[
            (out["advisor_email"].fillna("").str.strip() != "") |
            (out["advisor_phone"].fillna("").str.strip() != "")
        ].copy()

    return out.reset_index(drop=True)

# ---------------- Export helpers ----------------

KEEP_COLS = ["team_slug","team_name","advisor_name","advisor_role","advisor_email","advisor_phone"]


# ---------------- UI ----------------

st.set_page_config(page_title="AR Directory Extractor", layout="wide")

# --- Minimal, Apple-like CSS (subtle, clean, lots of whitespace) ---
st.markdown("""
<style>
/* Page width + typography */
.block-container { max-width: 1100px; padding-top: 2.2rem; padding-bottom: 3rem; }
html, body, [class*="css"]  { font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }

/* Hide Streamlit chrome */
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
header {visibility: hidden;}

/* Theme-aware variables (light + dark) */
:root{
  --txt: rgba(0,0,0,0.92);
  --sub: rgba(0,0,0,0.62);
  --card-bg: rgba(255,255,255,0.72);
  --card-border: rgba(0,0,0,0.10);
  --shadow: rgba(0,0,0,0.08);
}

@media (prefers-color-scheme: dark){
  :root{
    --txt: rgba(255,255,255,0.92);
    --sub: rgba(255,255,255,0.68);
    --card-bg: rgba(25,25,25,0.55);
    --card-border: rgba(255,255,255,0.12);
    --shadow: rgba(0,0,0,0.35);
  }
}

/* Card style */
.card {
  border: 1px solid var(--card-border);
  border-radius: 16px;
  padding: 18px 18px;
  background: var(--card-bg);
  box-shadow: 0 6px 24px var(--shadow);
  backdrop-filter: blur(8px);
}

/* Section titles */
.h1 { font-size: 40px; font-weight: 700; letter-spacing: -0.02em; margin: 0; color: var(--txt); }
.sub { font-size: 15px; margin-top: 6px; color: var(--sub); }

/* Modern widget feel */
.stTextArea textarea, .stTextInput input { border-radius: 12px !important; }
.stButton button, .stDownloadButton button {
  border-radius: 999px !important;
  padding: 0.55rem 1.0rem !important;
  font-weight: 600 !important;
}
</style>
""", unsafe_allow_html=True)

# --- Header ---
st.markdown('<p class="h1">AR Directory Extractor</p>', unsafe_allow_html=True)
st.markdown('<p class="sub">Pulls publicly available advisor contact details (email, phone, address) from Wealth Management team pages — in one streamlined page: paste a link → discover teams → select advisors → build your directory → export.</p>', unsafe_allow_html=True)
st.write("")

# --- Layout: left = main workflow, right = settings ---
left, right = st.columns([2.15, 1], gap="large")

with right:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown("### Settings")
    sleep_s = st.slider(
        "Polite delay (seconds)",
        0.25, 2.0, 0.75, 0.25,
        help="A small pause between page requests so the site is less likely to block you."
    )
    max_team_sites = st.number_input("Max team sites per run", 1, 300, 80, 5)
    drop_no_contact = st.checkbox("Drop rows with no email AND no phone", value=True)
    st.caption("Tip: If you see blocking/errors, increase delay to 1.0–1.5s.")
    st.markdown('</div>', unsafe_allow_html=True)

with left:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown("### 1) Paste branch URLs")
    seed_urls_text = st.text_area(
        "Branch seed URLs (one per line)",
        height=140,
        placeholder="Paste branch URLs here (one per line)..."
    )

    cA, cB, cC = st.columns([1, 1, 1.2])
    with cA:
        discover_clicked = st.button("Discover team sites", type="primary", use_container_width=True)
    with cB:
        clear_clicked = st.button("Clear results", use_container_width=True)
    with cC:
        st.caption("Keep runs smaller for faster tests.")

    st.markdown('</div>', unsafe_allow_html=True)

# Clear button
if clear_clicked:
    for k in ["df_candidates", "edited_candidates", "df_clean", "errs_build"]:
        st.session_state.pop(k, None)
    st.rerun()

# --- Discover ---
if discover_clicked:
    seeds = [s.strip() for s in seed_urls_text.splitlines() if s.strip()]
    if not seeds:
        st.warning("Paste at least one branch/team-list URL.")
    else:
        dfs = []
        errors = []
        with st.spinner("Discovering team sites..."):
            for s in seeds:
                try:
                    dfs.append(discover_team_roots_from_branch(s, sleep_s=sleep_s))
                except Exception as e:
                    errors.append({"seed": s, "error": str(e)})

        df_candidates = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame(
            columns=["branch_seed_url","team_root_url","link_text"]
        )
        df_candidates = df_candidates.drop_duplicates(subset=["team_root_url"]).reset_index(drop=True)
        df_candidates["include"] = True

        st.session_state["df_candidates"] = df_candidates
        st.session_state.pop("edited_candidates", None)
        st.session_state.pop("df_clean", None)
        st.session_state["errs_build"] = errors

# --- Show candidates + selection ---
if "df_candidates" in st.session_state and not st.session_state["df_candidates"].empty:
    st.write("")
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown("### 2) Select team sites")
    st.caption("Uncheck teams you don’t want to crawl. Then click **Build directory**.")

    dfc = st.session_state["df_candidates"].copy()

    b1, b2, b3, _ = st.columns([1, 1, 1.2, 5])
    if b1.button("Select all"):
        dfc["include"] = True
    if b2.button("Select none"):
        dfc["include"] = False
    if b3.button("Keep first 50"):
        dfc["include"] = False
        dfc.loc[:49, "include"] = True

    edited = st.data_editor(
        dfc[["include","branch_seed_url","team_root_url","link_text"]],
        use_container_width=True,
        num_rows="dynamic"
    )
    st.session_state["edited_candidates"] = edited
    st.markdown('</div>', unsafe_allow_html=True)

# --- Build directory ---
build_ready = "edited_candidates" in st.session_state and not st.session_state["edited_candidates"].empty

st.write("")
st.markdown('<div class="card">', unsafe_allow_html=True)
st.markdown("### 3) Build directory")

if not build_ready:
    st.info("Discover and select team sites above, then you can build.")
else:
    edited = st.session_state["edited_candidates"]
    chosen = edited[edited["include"] == True].head(int(max_team_sites))

    m1, m2, m3 = st.columns(3)
    m1.metric("Selected teams", int(len(chosen)))
    m2.metric("Polite delay", f"{sleep_s:.2f}s")
    m3.metric("Max this run", int(max_team_sites))

    build_clicked = st.button("Build directory", type="primary")

    if build_clicked:
        rows = []
        errs = []

        prog = st.progress(0)
        total = len(chosen)

        with st.spinner("Building directory (this can take a few minutes)..."):
            for i, r in enumerate(chosen.itertuples(index=False), start=1):
                try:
                    html_root, root_final, team_page, contact_page, slug = resolve_team_pages(r.team_root_url, sleep_s=sleep_s)
                    team_name = page_title(html_root)

                    people = []
                    source_page_used = ""

                    if team_page:
                        people, source_page_used = fetch_people(team_page, sleep_s=sleep_s)

                    if contact_page:
                        contact_people, contact_src = fetch_people(contact_page, sleep_s=sleep_s)
                        by_name = {canon_name(p["advisor_name"]): p for p in people}
                        for cp in contact_people:
                            k = canon_name(cp["advisor_name"])
                            if k in by_name:
                                for fld in ["advisor_email","advisor_phone","advisor_address","advisor_role","advisor_profile_url"]:
                                    if fld == "advisor_role":
                                        if (not by_name[k].get(fld)) and is_likely_role(cp.get(fld, ""), cp.get("advisor_name", "")):
                                            by_name[k][fld] = cp[fld]
                                    else:
                                        if not by_name[k].get(fld) and cp.get(fld):
                                            by_name[k][fld] = cp[fld]
                            else:
                                people.append(cp)

                    if not people:
                        people = extract_people_from_page(html_root, root_final)
                        source_page_used = root_final

                    if not people:
                        rows.append({
                            "branch_seed_url": r.branch_seed_url,
                            "team_root_url": root_final,
                            "team_slug": slug,
                            "team_name": team_name,
                            "team_page_url": team_page,
                            "contact_page_url": contact_page,
                            "advisor_name": "",
                            "advisor_role": "",
                            "advisor_email": "",
                            "advisor_phone": "",
                            "advisor_address": "",
                            "advisor_profile_url": "",
                            "source": "no_people_found",
                            "source_page_used": source_page_used
                        })
                    else:
                        for p in people:
                            rows.append({
                                "branch_seed_url": r.branch_seed_url,
                                "team_root_url": root_final,
                                "team_slug": slug,
                                "team_name": team_name,
                                "team_page_url": team_page,
                                "contact_page_url": contact_page,
                                "advisor_name": p.get("advisor_name",""),
                                "advisor_role": p.get("advisor_role",""),
                                "advisor_email": p.get("advisor_email",""),
                                "advisor_phone": p.get("advisor_phone",""),
                                "advisor_address": p.get("advisor_address",""),
                                "advisor_profile_url": p.get("advisor_profile_url",""),
                                "source": p.get("source",""),
                                "source_page_used": source_page_used or team_page or root_final
                            })
                except Exception as e:
                    errs.append({"team_root_url": getattr(r, "team_root_url", ""), "error": str(e)})

                prog.progress(min(1.0, i / max(1, total)))

        df_out = pd.DataFrame(rows)
        df_clean = post_process_directory(df_out, drop_no_contact=drop_no_contact)

        st.session_state["df_clean"] = df_clean
        st.session_state["errs_build"] = errs

        st.success("Done. Scroll down to export.")

st.markdown('</div>', unsafe_allow_html=True)

# --- Results / Export ---
if "df_clean" in st.session_state:
    st.write("")
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown("### Results & Export")

    df_clean = st.session_state["df_clean"]
    errs = st.session_state.get("errs_build", [])

    KEEP_COLS = ["team_slug","team_name","advisor_name","advisor_role","advisor_email","advisor_phone"]
    df_export = df_clean.copy()
    for c in KEEP_COLS:
        if c not in df_export.columns:
            df_export[c] = ""
    df_export = df_export[KEEP_COLS]

    a1, a2, a3 = st.columns(3)
    a1.metric("Rows exported", int(len(df_export)))
    a2.metric("Teams (unique)", int(df_export["team_slug"].nunique()) if len(df_export) else 0)
    a3.metric("Errors", int(len(errs)))

    st.dataframe(df_export, use_container_width=True, height=420)

    csv = df_export.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download CSV",
        data=csv,
        file_name="wg_directory_output.csv",
        mime="text/csv"
    )

    if errs:
        with st.expander("Show errors"):
            st.dataframe(pd.DataFrame(errs), use_container_width=True)

    st.markdown('</div>', unsafe_allow_html=True)


