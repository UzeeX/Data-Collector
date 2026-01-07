# app.py
import re
import time
from io import BytesIO
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
import streamlit as st

from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

# =============================================================================
# Constants / Regex
# =============================================================================

DEFAULT_HEADERS = {
    "User-Agent": "Inovestor-Directory-Extractor/0.4.2 (contact: ops@inovestor.com)",
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
email call connect discovery process additional specialist specialists
""".split())

PARTICLES = set([
    "de", "du", "des", "la", "le", "da", "di", "del", "della", "van", "von", "der", "den",
    "st", "ste", "saint", "sainte", "mc", "mac", "o'"
])

ROLE_WORDS = {
    "senior", "branch", "administrator", "admin", "assistant", "associate", "advisor", "adviser",
    "manager", "director", "president", "vp", "vice", "consultant", "specialist", "partner",
    "investment", "portfolio", "financial", "wealth", "planner", "planning",
    "conseiller", "conseillère", "placement", "gestionnaire", "directeur", "président", "adjointe", "adjoint",
    "client", "service", "representative", "représentant", "représentante"
}

JUNK_PHRASES = {
    "our branch team", "notre équipe de succursale", "our team", "notre équipe",
    "email us", "call us", "contact us", "let's connect", "lets connect",
    "additional td specialists", "a unique discovery process", "discovery process"
}

TD_STOP_MARKERS = {
    "Additional TD Specialists", "Spécialistes TD additionnels", "Additional TD specialists"
}
TD_SOCIAL_MARKERS = {"social links", "liens sociaux"}

# =============================================================================
# URL helpers (needed early for caching)
# =============================================================================

def norm_url(u: str) -> str:
    p = urlparse(u)
    return p._replace(fragment="", query="").geturl()

def same_domain(a: str, b: str) -> bool:
    return urlparse(a).netloc.lower() == urlparse(b).netloc.lower()

def extract_links(html: str, base_url: str):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        abs_url = norm_url(urljoin(base_url, href))
        text = a.get_text(" ", strip=True)
        out.append((text, abs_url))
    return out

def page_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(" ", strip=True)
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return ""

def find_best_link(links, base_url: str, pattern: re.Pattern):
    candidates = []
    for text, url in links:
        if not same_domain(url, base_url):
            continue
        if pattern.search(text or "") or pattern.search(url or ""):
            candidates.append((text, url))
    candidates.sort(key=lambda x: len(urlparse(x[1]).path))  # prefer shortest path
    return candidates[0][1] if candidates else ""

# =============================================================================
# Domain detectors
# =============================================================================

def is_td_url(u: str) -> bool:
    return "advisors.td.com" in (urlparse(u).netloc or "").lower()

def is_desjardins_url(u: str) -> bool:
    return "desjardins.com" in (urlparse(u).netloc or "").lower()

def is_cibc_wg_url(u: str) -> bool:
    return "woodgundyadvisors.cibc.com" in (urlparse(u).netloc or "").lower()

# =============================================================================
# Requests session + polite_get (with tiny cache)
# =============================================================================

SESSION = requests.Session()
SESSION.headers.update(DEFAULT_HEADERS)

_PAGE_CACHE = {}

def polite_get(url: str, sleep_s: float = 0.75, timeout: int = 25, retries: int = 3):
    """Polite GET with retry/backoff + safer decoding (accents) + small cache."""
    ukey = norm_url(url)
    if ukey in _PAGE_CACHE:
        return _PAGE_CACHE[ukey]

    time.sleep(max(0.0, sleep_s))
    last_err = None
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=timeout, allow_redirects=True)
            r.raise_for_status()

            enc = (r.encoding or "").lower()
            if not enc or enc == "iso-8859-1":
                r.encoding = r.apparent_encoding or "utf-8"

            html = r.text
            final_url = r.url

            _PAGE_CACHE[ukey] = (html, final_url)
            if len(_PAGE_CACHE) > 750:
                _PAGE_CACHE.pop(next(iter(_PAGE_CACHE)))

            return html, final_url
        except Exception as e:
            last_err = e
            time.sleep(1.25 * (attempt + 1))
    raise last_err

# =============================================================================
# Name cleaning / validation
# =============================================================================

def clean_person_name(raw: str) -> str:
    s = str(raw or "")
    s = s.replace("\u00A0", " ").replace("’", "'")
    s = re.sub(r"\([^)]*\)", "", s)
    s = s.strip()
    if "," in s:
        s = s.split(",", 1)[0].strip()
    s = re.sub(r"[^A-Za-zÀ-ÿ\-\s'\.]", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" -–—|")
    return s

def is_valid_person_name(raw: str) -> bool:
    s = clean_person_name(raw)
    if not s or re.search(r"\d", s):
        return False
    if s.lower().strip() in JUNK_PHRASES:
        return False

    tokens = s.split()
    if len(tokens) < 2 or len(tokens) > 6:
        return False

    low_tokens = [t.lower().strip(".") for t in tokens]
    if any(t in BANNED_WORDS for t in low_tokens):
        return False

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

def normalize_name(raw: str) -> str:
    s = re.sub(r"\s+", " ", (raw or "")).strip()
    if "," in s:
        s = s.split(",", 1)[0].strip()
    return s

# =============================================================================
# Role helpers
# =============================================================================

def _canon(s: str) -> str:
    return re.sub(r"[^a-z]+", "", (s or "").lower())

def is_likely_role(text: str, person_name: str = "") -> bool:
    if not text:
        return False
    t = re.sub(r"\s+", " ", text).strip(" -|•·")
    if len(t) < 2 or len(t) > 110:
        return False
    tl = t.lower()
    if tl in JUNK_PHRASES:
        return False
    if EMAIL_RE.search(t) or PHONE_RE.search(t):
        return False

    if person_name and _canon(t) == _canon(person_name):
        return False

    toks = re.findall(r"[A-Za-zÀ-ÿ']+", tl)
    return any(tok in ROLE_WORDS for tok in toks)

# =============================================================================
# TD helpers: directory → advisors + teams, then crawl advisor profiles + team meet pages
# =============================================================================

def td_root_from_any_td_url(u: str) -> str:
    p = urlparse(u)
    parts = [x for x in p.path.split("/") if x]
    if not parts:
        return f"{p.scheme}://{p.netloc}/"
    slug = parts[0]
    return f"{p.scheme}://{p.netloc}/{slug}/"

def _td_is_one_segment_root(u: str) -> bool:
    p = urlparse(u)
    parts = [x for x in p.path.strip("/").split("/") if x]
    return len(parts) == 1

def _norm_heading_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\u00A0", " ")).strip().lower()

def td_is_directory_page(soup: BeautifulSoup) -> bool:
    text = soup.get_text(" ", strip=True).lower()
    return ("advisors/teams" in text) or ("advisors & teams" in text) or ("advisors and teams" in text) or \
           ("conseillers/équipes" in text) or ("conseillers/equipes" in text)

def td_extract_links_under_heading(soup: BeautifulSoup, base_url: str, heading_set: set):
    """
    Find a heading whose text matches heading_set, collect links in sibling content until next heading.
    Works for most TD directory pages.
    """
    for h in soup.find_all(["h2", "h3", "h4"]):
        ht = _norm_heading_text(h.get_text(" ", strip=True))
        if ht not in heading_set:
            continue

        links = []
        for sib in h.find_next_siblings():
            if getattr(sib, "name", None) in ["h2", "h3", "h4"]:
                break
            for a in sib.find_all("a", href=True):
                t = a.get_text(" ", strip=True)
                u = norm_url(urljoin(base_url, a.get("href")))
                links.append((t, u))

        # fallback if the section is wrapped weirdly
        if not links:
            container = h.find_parent()
            if container:
                nxt = h.find_next("ul")
                if nxt:
                    for a in nxt.find_all("a", href=True):
                        t = a.get_text(" ", strip=True)
                        u = norm_url(urljoin(base_url, a.get("href")))
                        links.append((t, u))

        out = []
        seen = set()
        for t, u in links:
            if not is_td_url(u):
                continue
            root = td_root_from_any_td_url(u)
            if not _td_is_one_segment_root(root):
                continue
            k = root.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append((t, root))
        return out

    return []

def td_detect_single_root_kind(html: str) -> str:
    """If user pasted a single TD root, guess advisor vs team via 'Part of/Fait partie de' link presence."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        t = a.get_text(" ", strip=True)
        if re.match(r"^(Part of|Fait partie de)\b", t, re.I):
            return "advisor"
    return "team"

def td_extract_part_of_team(html: str, base_url: str):
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        t = a.get_text(" ", strip=True)
        if re.match(r"^(Part of|Fait partie de)\b", t, re.I):
            team_name = re.sub(r"^(Part of|Fait partie de)\s*", "", t, flags=re.I).strip()
            team_url = norm_url(urljoin(base_url, a.get("href")))
            if is_td_url(team_url):
                return team_name, td_root_from_any_td_url(team_url)
    return "", ""

def td_guess_meet_the_team_url(root_final: str, sleep_s: float):
    guesses = [
        "meet-the-team.htm", "meet-the-team.html", "meet-the-team",
        "meet-the-advisors.htm", "meet-the-advisors.html"
    ]
    base = root_final.rstrip("/") + "/"
    for g in guesses:
        u = urljoin(base, g)
        try:
            _, final_u = polite_get(u, sleep_s=sleep_s)
            return final_u
        except Exception:
            continue
    return ""

def _normalize_phone_list(phone_candidates):
    by_digits = {}
    for p in phone_candidates:
        s = re.sub(r"\s+", " ", (p or "")).strip()
        digs = re.sub(r"\D+", "", s)
        if len(digs) < 10:
            continue
        score = 0
        if "(" in s and ")" in s:
            score += 3
        if " " in s or "-" in s:
            score += 1
        if len(s) <= 18:
            score += 1
        if digs not in by_digits or score > by_digits[digs][0]:
            by_digits[digs] = (score, s)
    out = [v[1] for v in sorted(by_digits.values(), key=lambda x: -x[0])]
    return out[:3]

def td_extract_person_from_profile(html: str, base_url: str):
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    if not h1:
        return None

    name = clean_person_name(h1.get_text(" ", strip=True))
    if not is_valid_person_name(name):
        return None

    # Role (best-effort from text lines near top)
    lines = [x.strip() for x in soup.get_text("\n", strip=True).split("\n") if x.strip()]
    role_lines = []
    try:
        idx = next(i for i, x in enumerate(lines) if clean_person_name(x) == name)
    except StopIteration:
        idx = 0

    stop_words = {"phone", "toll free", "fax", "mobile", "office location", "contact", "courriel", "téléphone", "adresse"}
    for line in lines[idx + 1: idx + 22]:
        ll = line.lower()
        if ll in stop_words:
            break
        if is_likely_role(line, name):
            role_lines.append(line)

    role = " / ".join(dict.fromkeys(role_lines))[:120]

    # Email
    emails = []
    for a in soup.select('a[href^="mailto:"]'):
        href = a.get("href", "")
        e = href.split("mailto:", 1)[-1].split("?", 1)[0].strip()
        if e and e not in emails:
            emails.append(e)
    if not emails:
        for m in EMAIL_RE.findall(soup.get_text(" ", strip=True)):
            if m not in emails:
                emails.append(m)

    # Phones
    phones = []
    for a in soup.select('a[href^="tel:"]'):
        href = a.get("href", "")
        p = href.split("tel:", 1)[-1].strip()
        if p and p not in phones:
            phones.append(p)
    if not phones:
        for m in PHONE_RE.findall(soup.get_text(" ", strip=True)):
            phones.append(m)
    phones = _normalize_phone_list(phones)

    # Address
    address = ""
    text_lines = [x.strip() for x in soup.get_text("\n", strip=True).split("\n") if x.strip()]
    for i, line in enumerate(text_lines):
        if line.lower() in {"office location", "adresse du bureau"}:
            if i + 1 < len(text_lines):
                address = text_lines[i + 1]
            break
    if not address:
        for i, line in enumerate(text_lines):
            if POSTAL_CA_RE.search(line):
                start = max(0, i - 2)
                end = min(len(text_lines), i + 2)
                address = " | ".join(text_lines[start:end])
                break

    return {
        "advisor_name": name,
        "advisor_role": role,
        "advisor_email": "; ".join(emails[:3]),
        "advisor_phone": "; ".join(phones[:3]),
        "advisor_address": address,
        "advisor_profile_url": norm_url(base_url),
        "source": "td_profile",
    }

def td_extract_people_from_meet_page(html: str, base_url: str):
    """
    TD Meet-the-Team pages can contain many entries; some have no profile URL.
    Parse via stripped strings + boundary heuristics.
    """
    soup = BeautifulSoup(html, "html.parser")
    strings = [s.strip().replace("\u00A0", " ") for s in soup.stripped_strings if s and s.strip()]

    trimmed = []
    for s in strings:
        if s in TD_STOP_MARKERS:
            break
        trimmed.append(s)

    entries = []
    cur = []

    def has_email(buf):
        return any(EMAIL_RE.search(x) for x in buf)

    def looks_like_person_line(x: str) -> bool:
        nm = clean_person_name(x)
        return is_valid_person_name(nm) and (nm.lower() not in JUNK_PHRASES)

    for s in trimmed:
        sl = s.strip().lower()
        if sl in TD_SOCIAL_MARKERS:
            if cur:
                entries.append(cur)
            cur = []
            continue

        if s.lower() == "photo":
            continue

        if cur and has_email(cur) and looks_like_person_line(s):
            entries.append(cur)
            cur = [s]
            continue

        cur.append(s)

    if cur:
        entries.append(cur)

    people = []
    for buf in entries:
        name = ""
        for x in buf:
            nm = clean_person_name(x)
            if is_valid_person_name(nm):
                name = nm
                break
        if not name:
            continue

        emails = []
        for x in buf:
            for m in EMAIL_RE.findall(x):
                if m not in emails:
                    emails.append(m)

        phone_candidates = []
        for x in buf:
            for m in PHONE_RE.findall(x):
                phone_candidates.append(m)
        phones = _normalize_phone_list(phone_candidates)

        prof = ""
        buf_text = " ".join(buf)
        m = re.search(r"(?:https?:)?//advisors\.td\.com/([A-Za-z0-9.\-_/]+)", buf_text)
        if m:
            prof = "https://advisors.td.com/" + m.group(1).strip("/")
            prof = norm_url(prof)

        role_lines = []
        hit_name = False
        for x in buf:
            if clean_person_name(x) == name:
                hit_name = True
                continue
            if not hit_name:
                continue
            if EMAIL_RE.search(x) or PHONE_RE.search(x) or x.lower() in TD_SOCIAL_MARKERS:
                break
            if is_likely_role(x, name):
                role_lines.append(x)

        role = " / ".join(dict.fromkeys(role_lines))[:120]

        people.append({
            "advisor_name": name,
            "advisor_role": role,
            "advisor_email": "; ".join(emails[:3]),
            "advisor_phone": "; ".join(phones[:3]),
            "advisor_address": "",
            "advisor_profile_url": prof,
            "source": "td_meet_the_team",
        })

    # Dedupe within page
    seen = set()
    out = []
    for p in people:
        key = (p.get("advisor_profile_url") or "").lower()
        if not key:
            key = (p.get("advisor_email") or "").lower()
        if not key:
            key = canon_name(p.get("advisor_name") or "") + "|" + re.sub(r"\D+", "", (p.get("advisor_phone") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out

def td_fetch_people(url: str, sleep_s: float):
    html, final_url = polite_get(url, sleep_s=sleep_s)
    path = (urlparse(final_url).path or "").lower()

    if "meet-the-team" in path or "meet-the-advisors" in path:
        return td_extract_people_from_meet_page(html, final_url), final_url

    p = td_extract_person_from_profile(html, final_url)
    if p:
        return [p], final_url

    # if template changed but still a roster page
    if ("meet the team" in html.lower()) or ("rencontrez l’équipe" in html.lower()) or ("rencontrez l'equipe" in html.lower()):
        roster = td_extract_people_from_meet_page(html, final_url)
        if roster:
            return roster, final_url

    return [], final_url

def discover_td_targets(seed_url: str, sleep_s: float):
    """
    If seed is a TD directory page: return BOTH sections:
      - Advisors (each advisor profile)
      - Teams (each team root)
    Otherwise: return the single root as advisor/team guess.
    """
    html, final_url = polite_get(seed_url, sleep_s=sleep_s)
    soup = BeautifulSoup(html, "html.parser")

    if td_is_directory_page(soup):
        advisors = td_extract_links_under_heading(
            soup, final_url, heading_set={"advisors", "conseillers", "advisor", "conseiller"}
        )
        teams = td_extract_links_under_heading(
            soup, final_url, heading_set={"teams", "équipes", "equipes", "team", "équipe", "equipe"}
        )

        rows = []
        for t, u in advisors:
            rows.append({"branch_seed_url": seed_url, "team_root_url": u, "link_text": t, "td_kind": "advisor"})
        for t, u in teams:
            rows.append({"branch_seed_url": seed_url, "team_root_url": u, "link_text": t, "td_kind": "team"})

        df = pd.DataFrame(rows)
        if df.empty:
            return pd.DataFrame(columns=["branch_seed_url", "team_root_url", "link_text", "td_kind"])
        return df.drop_duplicates(subset=["team_root_url", "td_kind"]).reset_index(drop=True)

    root = td_root_from_any_td_url(final_url)
    kind = td_detect_single_root_kind(html)
    return pd.DataFrame([{"branch_seed_url": seed_url, "team_root_url": root, "link_text": "seed", "td_kind": kind}])

# =============================================================================
# Desjardins discovery
# =============================================================================

DESJARDINS_TEAM_LINK_RE = re.compile(r"/find-us/desjardins-securities-team/[^/?#]+\.html$", re.I)

def discover_desjardins_team_pages(seed_url: str, sleep_s: float):
    html, final_url = polite_get(seed_url, sleep_s=sleep_s)
    links = extract_links(html, final_url)

    candidates = []
    if DESJARDINS_TEAM_LINK_RE.search(urlparse(final_url).path):
        candidates.append({"branch_seed_url": seed_url, "team_root_url": norm_url(final_url), "link_text": "seed", "td_kind": ""})

    for text, u in links:
        if not is_desjardins_url(u):
            continue
        if not DESJARDINS_TEAM_LINK_RE.search(urlparse(u).path):
            continue
        t = (text or "").strip()
        if t.lower().startswith("view profile") or t.lower().startswith("voir le profil"):
            continue
        candidates.append({"branch_seed_url": seed_url, "team_root_url": norm_url(u), "link_text": t or u, "td_kind": ""})

    df = pd.DataFrame(candidates)
    if df.empty:
        return pd.DataFrame(columns=["branch_seed_url", "team_root_url", "link_text", "td_kind"])
    return df.drop_duplicates(subset=["team_root_url"]).reset_index(drop=True)

# =============================================================================
# CIBC Wood Gundy discovery support
# =============================================================================

def branch_slug_from_url(url: str) -> str:
    parts = urlparse(url).path.strip("/").split("/")
    if len(parts) >= 2 and parts[0].lower() == "web":
        return parts[1].lower()
    return ""

def is_true_team_root(url: str, branch_slug: str) -> bool:
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

# =============================================================================
# Discovery: Branch → targets
# =============================================================================

def discover_team_roots_from_branch(seed_url: str, sleep_s: float):
    if is_td_url(seed_url):
        return discover_td_targets(seed_url, sleep_s=sleep_s)

    if is_desjardins_url(seed_url):
        return discover_desjardins_team_pages(seed_url, sleep_s=sleep_s)

    # CIBC WG
    html, final_url = polite_get(seed_url, sleep_s=sleep_s)
    links = extract_links(html, final_url)

    if "our-investment-advisors-and-their-teams" not in final_url.lower():
        for _, u in links:
            if "our-investment-advisors-and-their-teams" in (u or "").lower():
                html, final_url = polite_get(u, sleep_s=sleep_s)
                links = extract_links(html, final_url)
                break

    branch_slug = branch_slug_from_url(final_url)
    candidates = []
    for text, u in links:
        if not same_domain(u, final_url):
            continue
        if is_true_team_root(u, branch_slug):
            candidates.append({"branch_seed_url": seed_url, "team_root_url": u, "link_text": text, "td_kind": ""})

    df = pd.DataFrame(candidates)
    if df.empty:
        return pd.DataFrame(columns=["branch_seed_url", "team_root_url", "link_text", "td_kind"])
    return df.drop_duplicates(subset=["team_root_url"]).reset_index(drop=True)

# =============================================================================
# Slug helper
# =============================================================================

def to_team_slug(team_root_url: str) -> str:
    p = urlparse(team_root_url)
    host = (p.netloc or "").lower()
    parts = [x for x in p.path.strip("/").split("/") if x]

    # TD
    if "advisors.td.com" in host and parts and parts[0]:
        return parts[0].lower()

    # Desjardins
    if "desjardins.com" in host and parts:
        last = parts[-1].lower().replace(".html", "")
        last = last.replace("_", "-")
        last = re.sub(r"[^a-z0-9\-]+", "-", last)
        last = re.sub(r"-{2,}", "-", last).strip("-")
        return last

    # CIBC
    seg = parts[0] if parts else ""
    seg = seg.replace("_", "-")
    seg = re.sub(r"[^A-Za-z0-9\-]+", "-", seg)
    seg = re.sub(r"-{2,}", "-", seg).strip("-")
    return seg.lower()

# =============================================================================
# Generic people extraction (non-TD)
# =============================================================================

def looks_like_name(name: str) -> bool:
    s = (name or "").strip()
    if not s:
        return False
    sl = s.lower()
    if sl in JUNK_PHRASES:
        return False
    if s.isupper() and len(s.split()) >= 2:
        return False
    parts = s.split()
    if len(parts) < 2 or len(parts) > 6:
        return False
    if any(p.lower() in ROLE_WORDS for p in parts):
        return False
    if EMAIL_RE.search(s) or PHONE_RE.search(s):
        return False
    if not re.match(r"^[A-Za-zÀ-ÿ]", s):
        return False
    return True

def extract_contact_from_block(block: BeautifulSoup):
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
    soup = BeautifulSoup(html, "html.parser")
    people = []

    for h in soup.find_all(["h2", "h3", "h4", "h5"]):
        raw = h.get_text(" ", strip=True)
        name = normalize_name(raw)
        if not looks_like_name(name):
            continue

        block = h
        for _ in range(4):
            if block.parent is None:
                break
            block = block.parent
            if block.select_one('a[href^="mailto:"]') or block.select_one('a[href^="tel:"]'):
                break

        role = ""
        for sib in h.find_next_siblings(limit=6):
            txt = sib.get_text(" ", strip=True)
            if is_likely_role(txt, name):
                role = txt
                break

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
    if is_td_url(url):
        return td_fetch_people(url, sleep_s=sleep_s)

    html, final_url = polite_get(url, sleep_s=sleep_s)
    people = extract_people_from_page(html, final_url)
    return people, final_url

# =============================================================================
# Post-processing / Global de-dupe (no same person on 2 lines)
# =============================================================================

BASE_OUT_COLS = [
    "branch_seed_url", "team_root_url", "team_slug", "team_name",
    "team_page_url", "contact_page_url",
    "advisor_name", "advisor_role", "advisor_email", "advisor_phone",
    "advisor_address", "advisor_profile_url",
    "source", "source_page_used"
]

def _ensure_cols(df: pd.DataFrame, cols, fill=""):
    for c in cols:
        if c not in df.columns:
            df[c] = fill
    return df

def _first_email(email_field: str) -> str:
    s = (email_field or "").strip()
    if not s:
        return ""
    return s.split(";")[0].strip().lower()

def _digits_phone(phone_field: str) -> str:
    s = (phone_field or "").strip()
    if not s:
        return ""
    return re.sub(r"\D+", "", s)[:15]

def post_process_directory(df_out: pd.DataFrame, drop_no_contact=True) -> pd.DataFrame:
    df = df_out.copy()
    df = _ensure_cols(df, BASE_OUT_COLS, fill="")

    df["advisor_name"] = df["advisor_name"].apply(clean_person_name)
    df = df[df["advisor_name"].apply(is_valid_person_name)].copy()
    if df.empty:
        return pd.DataFrame(columns=BASE_OUT_COLS)

    def score_row(r):
        score = 0
        for c in ["advisor_email", "advisor_phone", "advisor_address", "advisor_profile_url"]:
            v = str(r.get(c) or "").strip()
            if v:
                score += 1
        role = str(r.get("advisor_role") or "").strip()
        if is_likely_role(role, str(r.get("advisor_name") or "")):
            score += 1
        return score

    df["_score"] = df.apply(score_row, axis=1)
    df = df.sort_values("_score", ascending=False)

    # Global person key: profile URL > email > phone > canonical name
    def person_key(r):
        prof = (r.get("advisor_profile_url") or "").strip().lower()
        if prof:
            return "p:" + prof
        em = _first_email(r.get("advisor_email") or "")
        if em:
            return "e:" + em
        ph = _digits_phone(r.get("advisor_phone") or "")
        if ph:
            return "t:" + ph
        return "n:" + canon_name(r.get("advisor_name") or "")

    df["person_key"] = df.apply(person_key, axis=1)

    merged_rows = []
    for _, g in df.groupby(["person_key"], sort=False):
        base = g.iloc[0].to_dict()

        # Merge team(s) so person isn't duplicated across Advisor + Team crawls
        team_slugs = [str(x).strip() for x in g["team_slug"].tolist() if str(x).strip()]
        team_names = [str(x).strip() for x in g["team_name"].tolist() if str(x).strip()]
        base["team_slug"] = "; ".join(dict.fromkeys(team_slugs))
        base["team_name"] = "; ".join(dict.fromkeys(team_names))

        for col in ["advisor_role", "advisor_email", "advisor_phone", "advisor_address", "advisor_profile_url"]:
            vals = [v for v in g[col].tolist() if pd.notna(v) and str(v).strip() != ""]
            if col == "advisor_role":
                nm = base.get("advisor_name", "")
                vals = [v for v in vals if is_likely_role(str(v), nm)]
            base[col] = vals[0] if vals else (base.get(col) or "")

        merged_rows.append(base)

    out = pd.DataFrame(merged_rows)
    out = _ensure_cols(out, BASE_OUT_COLS, fill="")
    out = out.drop(columns=["_score", "person_key"], errors="ignore")

    if drop_no_contact:
        out = out[
            (out["advisor_email"].fillna("").str.strip() != "") |
            (out["advisor_phone"].fillna("").str.strip() != "")
        ].copy()

    return out.reset_index(drop=True)

# =============================================================================
# Excel export (auto-fit + team color banding + sheets per input link)
# =============================================================================

def _safe_sheet_name(name: str) -> str:
    s = str(name or "").strip()
    s = re.sub(r"[\[\]\:\*\?\/\\]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        s = "Sheet"
    return s[:31]

def _autofit_columns(ws, max_width: int = 52, min_width: int = 10):
    for col_idx in range(1, ws.max_column + 1):
        col_letter = get_column_letter(col_idx)
        max_len = 0
        for row_idx in range(1, ws.max_row + 1):
            v = ws.cell(row=row_idx, column=col_idx).value
            if v is None:
                continue
            v = str(v)
            if len(v) > max_len:
                max_len = len(v)
        ws.column_dimensions[col_letter].width = max(min_width, min(max_width, max_len + 2))

def _apply_team_banding(ws, team_col_idx: int, start_row: int, end_row: int):
    fills = [
        PatternFill("solid", fgColor="F5F7FF"),
        PatternFill("solid", fgColor="F7F7F7"),
        PatternFill("solid", fgColor="F4FFF7"),
        PatternFill("solid", fgColor="FFF7F4"),
    ]
    team_to_fill = {}
    fill_idx = 0

    for r in range(start_row, end_row + 1):
        team_val = ws.cell(row=r, column=team_col_idx).value
        team_raw = str(team_val or "").strip()
        # if merged team list: band by the first team in the list
        team_key = team_raw.split(";")[0].strip().lower() if team_raw else ""

        if team_key not in team_to_fill:
            team_to_fill[team_key] = fills[fill_idx % len(fills)]
            fill_idx += 1

        fill = team_to_fill[team_key]
        for c in range(1, ws.max_column + 1):
            ws.cell(row=r, column=c).fill = fill

def build_styled_xlsx(df_full: pd.DataFrame, keep_cols: list) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)

    df = df_full.copy()
    for c in keep_cols:
        if c not in df.columns:
            df[c] = ""
    df = df[keep_cols].copy()

    sheets = [("All", df)]

    # Separate sheets per input link (seed URL) when available
    if "branch_seed_url" in df_full.columns:
        for seed, g in df_full.groupby("branch_seed_url", dropna=False):
            seed_name = _safe_sheet_name(seed)
            existing = set([n for n, _ in sheets])
            base = seed_name
            k = 2
            while seed_name in existing:
                seed_name = _safe_sheet_name(f"{base} {k}")
                k += 1
            sg = g.copy()
            for c in keep_cols:
                if c not in sg.columns:
                    sg[c] = ""
            sg = sg[keep_cols].copy()
            sheets.append((seed_name, sg))

    header_fill = PatternFill("solid", fgColor="111827")
    header_font = Font(bold=True, color="FFFFFF")
    header_align = Alignment(horizontal="left", vertical="center")
    cell_align = Alignment(horizontal="left", vertical="top", wrap_text=True)

    used_table_names = set()

    for sheet_name, sdf in sheets:
        ws = wb.create_sheet(title=_safe_sheet_name(sheet_name))

        # Header
        for col_idx, col_name in enumerate(keep_cols, start=1):
            cell = ws.cell(row=1, column=col_idx, value=col_name)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = header_align

        # Rows
        for r_idx, row in enumerate(sdf.itertuples(index=False), start=2):
            for c_idx, value in enumerate(row, start=1):
                cell = ws.cell(row=r_idx, column=c_idx, value="" if value is None else str(value))
                cell.alignment = cell_align

        ws.freeze_panes = "A2"
        ws.row_dimensions[1].height = 22

        # Table (filters)
        if ws.max_row >= 2 and ws.max_column >= 1:
            ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"
            base_name = re.sub(r"[^A-Za-z0-9_]", "", f"Tbl_{ws.title}")[:22] or "Table"
            tname = base_name
            i = 2
            while tname in used_table_names:
                tname = f"{base_name}{i}"
                i += 1
            used_table_names.add(tname)

            tab = Table(displayName=tname, ref=ref)
            tab.tableStyleInfo = TableStyleInfo(
                name="TableStyleMedium9",
                showFirstColumn=False,
                showLastColumn=False,
                showRowStripes=False,
                showColumnStripes=False
            )
            ws.add_table(tab)

        # Auto-fit widths
        _autofit_columns(ws)

        # Team banding if team_slug exists
        if "team_slug" in keep_cols:
            team_col_idx = keep_cols.index("team_slug") + 1
            if ws.max_row >= 2:
                _apply_team_banding(ws, team_col_idx, start_row=2, end_row=ws.max_row)

    bio = BytesIO()
    wb.save(bio)
    return bio.getvalue()

# =============================================================================
# UI
# =============================================================================

st.set_page_config(page_title="AR Directory Extractor", layout="wide")

st.markdown("""
<style>
.block-container { max-width: 1100px; padding-top: 2.2rem; padding-bottom: 3rem; }
html, body, [class*="css"]  { font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
header {visibility: hidden;}
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
.card {
  border: 1px solid var(--card-border);
  border-radius: 16px;
  padding: 18px 18px;
  background: var(--card-bg);
  box-shadow: 0 6px 24px var(--shadow);
  backdrop-filter: blur(8px);
}
.h1 { font-size: 40px; font-weight: 700; letter-spacing: -0.02em; margin: 0; color: var(--txt); }
.sub { font-size: 15px; margin-top: 6px; color: var(--sub); }
.stTextArea textarea, .stTextInput input { border-radius: 12px !important; }
.stButton button, .stDownloadButton button {
  border-radius: 999px !important;
  padding: 0.55rem 1.0rem !important;
  font-weight: 600 !important;
}
</style>
""", unsafe_allow_html=True)

st.markdown('<p class="h1">AR Directory Extractor</p>', unsafe_allow_html=True)
st.markdown(
    '<p class="sub">Pulls publicly available advisor contact details (email, phone, address) from Wealth Management pages. '
    '<b>TD directories:</b> collects BOTH <b>Advisors</b> and <b>Teams</b>, then crawls advisor profiles + team meet pages, and globally de-dupes people.</p>',
    unsafe_allow_html=True
)
st.write("")

left, right = st.columns([2.15, 1], gap="large")

with right:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown("### Settings")
    sleep_s = st.slider(
        "Polite delay (seconds)",
        0.25, 2.0, 0.75, 0.25,
        help="Small pause between requests so the site is less likely to block you."
    )
    max_targets = st.number_input("Max targets per run", 1, 600, 150, 10)
    drop_no_contact = st.checkbox(
        "Drop rows with no email AND no phone",
        value=False
    )
    st.caption("Tip: If you see blocking/errors, increase delay to 1.0–1.5s.")
    st.markdown('</div>', unsafe_allow_html=True)

with left:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown("### 1) Paste branch URLs")
    seed_urls_text = st.text_area(
        "Branch seed URLs (one per line)",
        height=140,
        placeholder="Examples:\nhttps://advisors.td.com/montreal1/\nhttps://www.desjardins.com/.../find-us/.../desjardins-securities-team/....html\nhttps://woodgundyadvisors.cibc.com/our-investment-advisors-and-their-teams/"
    )

    cA, cB, cC = st.columns([1, 1, 1.2])
    with cA:
        discover_clicked = st.button("Discover targets", type="primary", use_container_width=True)
    with cB:
        clear_clicked = st.button("Clear results", use_container_width=True)
    with cC:
        st.caption("Keep runs smaller for faster tests.")

    st.markdown('</div>', unsafe_allow_html=True)

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
        with st.spinner("Discovering targets..."):
            for s in seeds:
                try:
                    dfs.append(discover_team_roots_from_branch(s, sleep_s=sleep_s))
                except Exception as e:
                    errors.append({"seed": s, "error": str(e)})

        df_candidates = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame(
            columns=["branch_seed_url", "team_root_url", "link_text", "td_kind"]
        )
        if "td_kind" not in df_candidates.columns:
            df_candidates["td_kind"] = ""

        df_candidates = df_candidates.drop_duplicates(subset=["team_root_url", "td_kind"]).reset_index(drop=True)
        df_candidates["include"] = True

        st.session_state["df_candidates"] = df_candidates
        st.session_state.pop("edited_candidates", None)
        st.session_state.pop("df_clean", None)
        st.session_state["errs_build"] = errors

# --- Candidates ---
if "df_candidates" in st.session_state and not st.session_state["df_candidates"].empty:
    st.write("")
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown("### 2) Select targets")
    st.caption("Uncheck anything you don’t want to crawl. Then click **Build directory**.")

    dfc = st.session_state["df_candidates"].copy()

    b1, b2, b3, _ = st.columns([1, 1, 1.2, 5])
    if b1.button("Select all"):
        dfc["include"] = True
    if b2.button("Select none"):
        dfc["include"] = False
    if b3.button("Keep first 120"):
        dfc["include"] = False
        dfc.loc[:119, "include"] = True

    edited = st.data_editor(
        dfc[["include", "branch_seed_url", "team_root_url", "td_kind", "link_text"]],
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
    st.info("Discover and select targets above, then you can build.")
else:
    edited = st.session_state["edited_candidates"].copy()
    chosen = edited[edited["include"] == True].head(int(max_targets)).copy()

    # For TD: process Advisors first, then Teams (as requested)
    if "td_kind" in chosen.columns:
        order_map = {"advisor": 0, "team": 1}
        chosen["_ord"] = chosen["td_kind"].astype(str).str.lower().map(order_map).fillna(2).astype(int)
        chosen = chosen.sort_values(["_ord", "branch_seed_url", "team_root_url"]).drop(columns=["_ord"], errors="ignore")

    m1, m2, m3 = st.columns(3)
    m1.metric("Selected targets", int(len(chosen)))
    m2.metric("Polite delay", f"{sleep_s:.2f}s")
    m3.metric("Max this run", int(max_targets))

    build_clicked = st.button("Build directory", type="primary")

    if build_clicked:
        rows = []
        errs = []

        prog = st.progress(0)
        total = len(chosen)

        with st.spinner("Building directory (this can take a few minutes)..."):
            for i, r in enumerate(chosen.itertuples(index=False), start=1):
                try:
                    td_kind = (getattr(r, "td_kind", "") or "").strip().lower()

                    # ---------------- TD ----------------
                    if is_td_url(r.team_root_url):
                        root_html, root_final = polite_get(r.team_root_url, sleep_s=sleep_s)
                        root_slug = to_team_slug(root_final)

                        # (A) Advisors section: crawl each advisor profile
                        if td_kind == "advisor":
                            people, src = td_fetch_people(root_final, sleep_s=sleep_s)

                            team_aff_name, team_aff_root = td_extract_part_of_team(root_html, root_final)
                            # If Part-of exists, attach that team context; else keep advisor root context
                            team_root_url = team_aff_root or root_final
                            team_slug = to_team_slug(team_root_url)
                            team_name = team_aff_name or page_title(root_html) or team_slug

                            for p in people:
                                rows.append({
                                    "branch_seed_url": r.branch_seed_url,
                                    "team_root_url": team_root_url,
                                    "team_slug": team_slug,
                                    "team_name": team_name,
                                    "team_page_url": team_root_url,
                                    "contact_page_url": "",
                                    "advisor_name": p.get("advisor_name", ""),
                                    "advisor_role": p.get("advisor_role", ""),
                                    "advisor_email": p.get("advisor_email", ""),
                                    "advisor_phone": p.get("advisor_phone", ""),
                                    "advisor_address": p.get("advisor_address", ""),
                                    "advisor_profile_url": p.get("advisor_profile_url", ""),
                                    "source": p.get("source", ""),
                                    "source_page_used": src
                                })

                        # (B) Teams section: crawl each team meet-the-team page
                        else:
                            team_name = page_title(root_html) or root_slug
                            meet_url = td_guess_meet_the_team_url(root_final, sleep_s=sleep_s) or urljoin(root_final.rstrip("/") + "/", "meet-the-team.htm")
                            people, src = td_fetch_people(meet_url, sleep_s=sleep_s)

                            if not people:
                                # last fallback
                                people, src = td_fetch_people(root_final, sleep_s=sleep_s)

                            for p in people:
                                rows.append({
                                    "branch_seed_url": r.branch_seed_url,
                                    "team_root_url": root_final,
                                    "team_slug": root_slug,
                                    "team_name": team_name,
                                    "team_page_url": meet_url,
                                    "contact_page_url": "",
                                    "advisor_name": p.get("advisor_name", ""),
                                    "advisor_role": p.get("advisor_role", ""),
                                    "advisor_email": p.get("advisor_email", ""),
                                    "advisor_phone": p.get("advisor_phone", ""),
                                    "advisor_address": p.get("advisor_address", ""),
                                    "advisor_profile_url": p.get("advisor_profile_url", ""),
                                    "source": p.get("source", ""),
                                    "source_page_used": src
                                })

                        prog.progress(min(1.0, i / max(1, total)))
                        continue

                    # ---------------- Non-TD ----------------
                    html_root, root_final = polite_get(r.team_root_url, sleep_s=sleep_s)
                    team_name = page_title(html_root)
                    slug = to_team_slug(root_final)

                    links = extract_links(html_root, root_final)
                    team_page = find_best_link(links, root_final, TEAM_PAGE_TEXT_PAT)
                    contact_page = find_best_link(links, root_final, CONTACT_PAGE_TEXT_PAT)

                    # Desjardins: team page is root
                    if is_desjardins_url(root_final):
                        team_page = root_final
                        contact_page = ""

                    # CIBC WG: try /web/<slug>/our-team and /web/<slug>/contact
                    if is_cibc_wg_url(root_final):
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

                    people = []
                    source_page_used = ""

                    if team_page:
                        people, source_page_used = fetch_people(team_page, sleep_s=sleep_s)

                    if contact_page:
                        contact_people, contact_src = fetch_people(contact_page, sleep_s=sleep_s)
                        by_name = {canon_name(p["advisor_name"]): p for p in people if p.get("advisor_name")}
                        for cp in contact_people:
                            k = canon_name(cp.get("advisor_name", ""))
                            if not k:
                                continue
                            if k in by_name:
                                for fld in ["advisor_email", "advisor_phone", "advisor_address", "advisor_role", "advisor_profile_url"]:
                                    if fld == "advisor_role":
                                        if (not by_name[k].get(fld)) and is_likely_role(cp.get(fld, ""), cp.get("advisor_name", "")):
                                            by_name[k][fld] = cp.get(fld, "")
                                    else:
                                        if not by_name[k].get(fld) and cp.get(fld):
                                            by_name[k][fld] = cp.get(fld)
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
                                "advisor_name": p.get("advisor_name", ""),
                                "advisor_role": p.get("advisor_role", ""),
                                "advisor_email": p.get("advisor_email", ""),
                                "advisor_phone": p.get("advisor_phone", ""),
                                "advisor_address": p.get("advisor_address", ""),
                                "advisor_profile_url": p.get("advisor_profile_url", ""),
                                "source": p.get("source", ""),
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

    # CSV columns (as you requested previously)
    KEEP_COLS = ["team_slug", "team_name", "advisor_name", "advisor_role", "advisor_email", "advisor_phone"]
    df_csv = df_clean.copy()
    for c in KEEP_COLS:
        if c not in df_csv.columns:
            df_csv[c] = ""
    df_csv = df_csv[KEEP_COLS].copy()

    a1, a2, a3 = st.columns(3)
    a1.metric("Rows exported", int(len(df_csv)))
    a2.metric("Teams (unique)", int(df_csv["team_slug"].nunique()) if len(df_csv) else 0)
    a3.metric("Errors", int(len(errs)))

    st.dataframe(df_csv, use_container_width=True, height=420)

    # ✅ CSV: accents work in Excel with UTF-8 BOM
    csv_bytes = df_csv.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "Download CSV (UTF-8)",
        data=csv_bytes,
        file_name="directory_output.csv",
        mime="text/csv"
    )

    # ✅ Excel: auto-fit + team color banding + one sheet per input link
    # Include branch_seed_url in workbook so we can split sheets by input link
    excel_cols = ["branch_seed_url"] + KEEP_COLS if "branch_seed_url" in df_clean.columns else KEEP_COLS
    df_excel = df_clean.copy()
    for c in excel_cols:
        if c not in df_excel.columns:
            df_excel[c] = ""
    df_excel = df_excel[excel_cols].copy()

    try:
        xlsx_bytes = build_styled_xlsx(df_excel, keep_cols=excel_cols)
        st.download_button(
            "Download Excel (Styled .xlsx)",
            data=xlsx_bytes,
            file_name="directory_output.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        st.caption("Excel includes auto-fit column widths, team banding (by team_slug), and one worksheet per input link (branch_seed_url) + an All sheet.")
    except Exception as e:
        st.warning(f"Excel export failed: {e}")

    if errs:
        with st.expander("Show errors"):
            st.dataframe(pd.DataFrame(errs), use_container_width=True)

    st.markdown('</div>', unsafe_allow_html=True)
```

