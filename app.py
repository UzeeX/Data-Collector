# app.py
import re
import time
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
import streamlit as st

# ---------------- Constants / Regex ----------------

DEFAULT_HEADERS = {
    "User-Agent": "Inovestor-Directory-Extractor/0.3.1 (contact: ops@inovestor.com)",
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

PARTICLES = set([
    "de", "du", "des", "la", "le", "da", "di", "del", "della", "van", "von", "der", "den",
    "st", "ste", "saint", "sainte", "mc", "mac", "o'"
])

ROLE_WORDS = {
    "senior", "branch", "administrator", "admin", "assistant", "associate", "advisor", "adviser",
    "manager", "director", "president", "vp", "vice", "consultant", "specialist", "partner",
    "investment", "portfolio", "financial", "wealth", "planner", "planning",
    "conseiller", "conseillère", "placement", "gestionnaire", "directeur", "président",
    "adjointe", "adjoint"
}

JUNK_PHRASES = {
    "our branch team", "notre équipe de succursale", "our team", "notre équipe"
}

# ---------------- Requests session ----------------

SESSION = requests.Session()
SESSION.headers.update(DEFAULT_HEADERS)


def polite_get(url: str, sleep_s: float = 0.75, timeout: int = 25, retries: int = 3):
    """Polite GET with retry/backoff."""
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

    tokens = s.split()
    if len(tokens) < 2 or len(tokens) > 4:
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


# ---------------- Role extraction helpers ----------------

def _canon(s: str) -> str:
    return re.sub(r"[^a-z]+", "", (s or "").lower())


def is_likely_role(text: str, person_name: str = "") -> bool:
    if not text:
        return False
    t = re.sub(r"\s+", " ", text).strip(" -|•·")
    if len(t) < 3 or len(t) > 120:
        return False
    if t.lower() in JUNK_PHRASES:
        return False
    if EMAIL_RE.search(t) or PHONE_RE.search(t):
        return False

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
    person = normalize_name(h.get_text(" ", strip=True))
    name_canon = _canon(person)

    for sib in h.find_next_siblings(limit=6):
        txt = sib.get_text(" ", strip=True)
        if is_likely_role(txt, person):
            return txt

    parent = h.parent
    if not parent:
        return ""

    lines = [x.strip() for x in parent.get_text("\n", strip=True).split("\n") if x.strip()]

    idx = -1
    for i in range(len(lines)):
        for j in range(i, min(len(lines), i + 4)):
            window = " ".join(lines[i:j + 1])
            if _canon(window) == name_canon:
                idx = j
                break
        if idx != -1:
            break

    for line in lines[idx + 1: idx + 10]:
        if is_likely_role(line, person):
            return line

    return ""


# ---------------- URL helpers ----------------

def norm_url(u: str) -> str:
    p = urlparse(u)
    return p._replace(fragment="", query="").geturl()


def page_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(" ", strip=True)
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return ""


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


def same_domain(a: str, b: str) -> bool:
    return urlparse(a).netloc.lower() == urlparse(b).netloc.lower()


def find_best_link(links, base_url: str, pattern: re.Pattern):
    candidates = []
    for text, url in links:
        if not same_domain(url, base_url):
            continue
        if pattern.search(text or "") or pattern.search(url or ""):
            candidates.append((text, url))
    candidates.sort(key=lambda x: len(urlparse(x[1]).path))  # prefer shortest path
    return candidates[0][1] if candidates else ""


# ---------------- Domain detectors ----------------

def is_td_url(u: str) -> bool:
    return "advisors.td.com" in (urlparse(u).netloc or "").lower()


def is_desjardins_url(u: str) -> bool:
    return "desjardins.com" in (urlparse(u).netloc or "").lower()


def is_cibc_wg_url(u: str) -> bool:
    return "woodgundyadvisors.cibc.com" in (urlparse(u).netloc or "").lower()


# ---------------- TD discovery / resolve ----------------

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


def discover_td_team_pages(seed_url: str, sleep_s: float):
    """
    TD: If seed is a BRANCH HUB (like /montreal1/), discover teams listed under "Teams/Équipes".
    If seed is NOT a hub (or no teams found), treat it as a single site root.
    """
    html, final_url = polite_get(seed_url, sleep_s=sleep_s)
    soup = BeautifulSoup(html, "html.parser")

    candidates = []

    # Heuristic: branch hubs usually contain this label
    is_branch_hub = bool(soup.find(string=re.compile(r"Advisors/Teams|Conseillers/Équipes", re.I)))

    def is_teams_heading(tag):
        if tag.name not in ["h2", "h3", "h4"]:
            return False
        t = tag.get_text(" ", strip=True).lower()
        return t in {"teams", "équipes", "equipes"}

    if is_branch_hub:
        teams_hdr = soup.find(is_teams_heading)
        if teams_hdr:
            for el in teams_hdr.find_all_next(["a", "h2", "h3", "h4"], limit=450):
                if el.name in ["h2", "h3", "h4"] and el is not teams_hdr:
                    break
                if el.name == "a" and el.get("href"):
                    abs_u = norm_url(urljoin(final_url, el.get("href")))
                    if not is_td_url(abs_u):
                        continue
                    root_u = td_root_from_any_td_url(abs_u)
                    if not _td_is_one_segment_root(root_u):
                        continue
                    candidates.append({
                        "branch_seed_url": seed_url,
                        "team_root_url": root_u,
                        "link_text": el.get_text(" ", strip=True) or root_u
                    })

        if candidates:
            df = pd.DataFrame(candidates)
            return df.drop_duplicates(subset=["team_root_url"]).reset_index(drop=True)

    # Not a hub (or no teams found) → allow seed-only if it is a one-segment root
    if is_td_url(final_url) and _td_is_one_segment_root(final_url):
        return pd.DataFrame([{
            "branch_seed_url": seed_url,
            "team_root_url": td_root_from_any_td_url(final_url),
            "link_text": "seed"
        }])

    return pd.DataFrame(columns=["branch_seed_url", "team_root_url", "link_text"])


# ---------------- Desjardins discovery / resolve ----------------

DESJARDINS_TEAM_LINK_RE = re.compile(r"/find-us/desjardins-securities-team/[^/?#]+\.html$", re.I)


def discover_desjardins_team_pages(seed_url: str, sleep_s: float):
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
        if not DESJARDINS_TEAM_LINK_RE.search(urlparse(u).path):
            continue

        t = (text or "").strip()
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
    return df.drop_duplicates(subset=["team_root_url"]).reset_index(drop=True)


# ---------------- CIBC Wood Gundy discovery support ----------------

def branch_slug_from_url(url: str) -> str:
    parts = urlparse(url).path.strip("/").split("/")
    if len(parts) >= 2 and parts[0].lower() == "web":
        return parts[1].lower()
    return ""


def is_true_team_root(url: str, branch_slug: str) -> bool:
    """
    Accept only URLs like:
      https://woodgundyadvisors.cibc.com/Team-Name/
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


# ---------------- Discovery (Branch → Team roots) ----------------

def discover_team_roots_from_branch(seed_url: str, sleep_s: float):
    # ✅ TD flow
    if is_td_url(seed_url):
        return discover_td_team_pages(seed_url, sleep_s=sleep_s)

    # ✅ Desjardins flow
    if is_desjardins_url(seed_url):
        return discover_desjardins_team_pages(seed_url, sleep_s=sleep_s)

    # ✅ CIBC Wood Gundy flow (original)
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
            candidates.append({"branch_seed_url": seed_url, "team_root_url": u, "link_text": text})

    df = pd.DataFrame(candidates)
    if df.empty:
        return pd.DataFrame(columns=["branch_seed_url", "team_root_url", "link_text"])
    return df.drop_duplicates(subset=["team_root_url"]).reset_index(drop=True)


# ---------------- Slug + resolve pages ----------------

def to_team_slug(team_root_url: str) -> str:
    p = urlparse(team_root_url)
    host = (p.netloc or "").lower()
    parts = p.path.strip("/").split("/")

    # ✅ TD
    if "advisors.td.com" in host and parts and parts[0]:
        return parts[0].lower()

    # ✅ Desjardins: filename without .html
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

    # ✅ TD: roster is usually /meet-the-team.htm
    if is_td_url(root_final):
        slug = to_team_slug(root_final)

        team_guesses = ["meet-the-team.htm", "meet-the-team.html", "meet-the-team", "our-team.htm", "our-team.html"]
        contact_guesses = ["contact-us.htm", "contact-us.html", "contact.htm", "contact", "contact-us"]

        team_page = ""
        contact_page = ""

        for g in team_guesses:
            guess = urljoin(root_final.rstrip("/") + "/", g)
            try:
                _, u = polite_get(guess, sleep_s=sleep_s)
                team_page = u
                break
            except Exception:
                continue

        for g in contact_guesses:
            guess = urljoin(root_final.rstrip("/") + "/", g)
            try:
                _, u = polite_get(guess, sleep_s=sleep_s)
                contact_page = u
                break
            except Exception:
                continue

        if not team_page:
            team_page = root_final

        return html_root, root_final, team_page, contact_page, slug

    # ✅ Desjardins
    if is_desjardins_url(root_final):
        slug = to_team_slug(root_final)
        return html_root, root_final, root_final, "", slug

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
    if len(parts) < 2 or len(parts) > 5:
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


def extract_people_from_td_page(html: str, base_url: str):
    """
    TD-specific: cards often don't use name headings the same way.
    We anchor extraction around tel/mailto links and walk up to a reasonable "person container".
    Also captures headings like "Name, Branch Manager".
    """
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main") or soup

    people = []
    seen = set()

    def add_person(name, role="", email="", phone="", address="", profile=""):
        name = normalize_name(name)
        if not is_valid_person_name(name):
            return
        k = canon_name(name)
        if k in seen:
            return
        seen.add(k)
        people.append({
            "advisor_name": name,
            "advisor_role": (role or "").strip(),
            "advisor_email": (email or "").strip(),
            "advisor_phone": (phone or "").strip(),
            "advisor_address": (address or "").strip(),
            "advisor_profile_url": (profile or "").strip(),
            "source": "td_card_parse"
        })

    def find_person_container(anchor_tag):
        # Walk up a few levels; pick the first ancestor that has a name-like heading/text
        cur = anchor_tag
        for _ in range(10):
            if not cur or not getattr(cur, "parent", None):
                break
            cur = cur.parent
            txt = cur.get_text(" ", strip=True)
            if not txt:
                continue

            # Find a plausible name inside this container
            for cand in cur.find_all(["h2", "h3", "h4", "h5", "strong", "b", "p", "span"], limit=25):
                t = cand.get_text(" ", strip=True)
                if t and looks_like_name(t) and is_valid_person_name(t):
                    return cur
        return anchor_tag.parent if anchor_tag else None

    # 1) Card-driven extraction from tel/mailto anchors
    anchors = main.select('a[href^="mailto:"], a[href^="tel:"]')
    used_blocks = set()

    for a in anchors:
        block = find_person_container(a)
        if not block:
            continue
        bid = id(block)
        if bid in used_blocks:
            continue
        used_blocks.add(bid)

        # Name
        name = ""
        for tag in block.find_all(["h2", "h3", "h4", "h5", "strong", "b"], limit=20):
            t = tag.get_text(" ", strip=True)
            if t and looks_like_name(t) and is_valid_person_name(t):
                name = t
                break
        if not name:
            # Try first line fallback
            first_line = (block.get_text("\n", strip=True).split("\n") or [""])[0].strip()
            if looks_like_name(first_line) and is_valid_person_name(first_line):
                name = first_line

        if not name:
            continue

        # Email/phone
        email = ""
        phone = ""
        for ma in block.select('a[href^="mailto:"]'):
            href = ma.get("href", "")
            em = href.split("mailto:", 1)[-1].split("?", 1)[0].strip()
            if em:
                email = em
                break

        for ta in block.select('a[href^="tel:"]'):
            href = ta.get("href", "")
            ph = href.split("tel:", 1)[-1].strip()
            if ph:
                phone = ph
                break

        if not phone:
            m = PHONE_RE.search(block.get_text(" ", strip=True))
            if m:
                phone = m.group(1).strip()

        # Role
        role = ""
        lines = [x.strip() for x in block.get_text("\n", strip=True).split("\n") if x.strip()]
        for ln in lines:
            if _canon(ln) == _canon(name):
                continue
            if is_likely_role(ln, name):
                role = ln
                break

        # Address
        address = ""
        for i, ln in enumerate(lines):
            if POSTAL_CA_RE.search(ln):
                address = " | ".join(lines[max(0, i - 2):min(len(lines), i + 2)])
                break

        add_person(name, role=role, email=email, phone=phone, address=address)

    # 2) Management team headings like "Name, Branch Manager"
    for h in main.find_all(["h2", "h3", "h4", "h5"]):
        t = h.get_text(" ", strip=True)
        m = re.match(r"^(.+?),\s*(.+)$", t)
        if not m:
            continue
        nm, rl = m.group(1).strip(), m.group(2).strip()
        if looks_like_name(nm) and is_valid_person_name(nm) and is_likely_role(rl, nm):
            # Try to find contact in nearby container
            block = h
            for _ in range(5):
                if not getattr(block, "parent", None):
                    break
                block = block.parent
                if block.select_one('a[href^="mailto:"]') or block.select_one('a[href^="tel:"]'):
                    break
            contact = extract_contact_from_block(block)
            add_person(
                nm,
                role=rl,
                email=contact.get("advisor_email", ""),
                phone=contact.get("advisor_phone", ""),
                address=contact.get("advisor_address", "")
            )

    return people


def extract_people_from_page(html: str, base_url: str):
    soup = BeautifulSoup(html, "html.parser")

    # ✅ TD: use TD-specific extractor first (fixes missing "Meet the Advisors" blocks)
    if is_td_url(base_url):
        td_people = extract_people_from_td_page(html, base_url)
        if td_people:
            return td_people

    # Generic heuristic (works well for CIBC/Desjardins + some TD sections)
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


# ---------------- Post-processing ----------------

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


def post_process_directory(df_out: pd.DataFrame, drop_no_contact=True) -> pd.DataFrame:
    df = df_out.copy()
    df = _ensure_cols(df, BASE_OUT_COLS, fill="")

    df["advisor_name"] = df["advisor_name"].apply(clean_person_name)
    df = df[df["advisor_name"].apply(is_valid_person_name)].copy()

    if df.empty:
        return pd.DataFrame(columns=BASE_OUT_COLS)

    df["name_key"] = df["advisor_name"].apply(canon_name)

    def score_row(r):
        score = 0
        for c in ["advisor_email", "advisor_phone", "advisor_address"]:
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
    for (_, nk), g in df.groupby(["team_root_url", "name_key"], sort=False):
        base = g.iloc[0].to_dict()

        for col in ["advisor_role", "advisor_email", "advisor_phone", "advisor_address", "advisor_profile_url"]:
            vals = [v for v in g[col].tolist() if pd.notna(v) and str(v).strip() != ""]
            if col == "advisor_role":
                nm = base.get("advisor_name", "")
                vals = [v for v in vals if is_likely_role(str(v), nm)]
            base[col] = vals[0] if vals else ""
        merged_rows.append(base)

    out = pd.DataFrame(merged_rows)
    out = _ensure_cols(out, BASE_OUT_COLS, fill="")
    out = out.drop(columns=["name_key", "_score"], errors="ignore")

    if drop_no_contact:
        out = out[
            (out["advisor_email"].fillna("").str.strip() != "") |
            (out["advisor_phone"].fillna("").str.strip() != "")
        ].copy()

    return out.reset_index(drop=True)


# ---------------- UI ----------------

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
    '<p class="sub">Pulls publicly available advisor contact details (email, phone, address) from Wealth Management team pages '
    '(supports TD Advisors, Desjardins, and CIBC Wood Gundy discovery flows).</p>',
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
        placeholder="Examples:\nhttps://advisors.td.com/montreal1/\nhttps://www.desjardins.com/.../find-us/.../desjardins-securities-team/....html\nhttps://woodgundyadvisors.cibc.com/our-investment-advisors-and-their-teams/"
    )

    cA, cB, cC = st.columns([1, 1, 1.2])
    with cA:
        discover_clicked = st.button("Discover team sites", type="primary", use_container_width=True)
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
        with st.spinner("Discovering team sites..."):
            for s in seeds:
                try:
                    dfs.append(discover_team_roots_from_branch(s, sleep_s=sleep_s))
                except Exception as e:
                    errors.append({"seed": s, "error": str(e)})

        df_candidates = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame(
            columns=["branch_seed_url", "team_root_url", "link_text"]
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
        dfc[["include", "branch_seed_url", "team_root_url", "link_text"]],
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
                    html_root, root_final, team_page, contact_page, slug = resolve_team_pages(
                        r.team_root_url, sleep_s=sleep_s
                    )
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
                                for fld in ["advisor_email", "advisor_phone", "advisor_address", "advisor_role",
                                            "advisor_profile_url"]:
                                    if fld == "advisor_role":
                                        if (not by_name[k].get(fld)) and is_likely_role(
                                                cp.get(fld, ""), cp.get("advisor_name", "")
                                        ):
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

    KEEP_COLS = ["team_slug", "team_name", "advisor_name", "advisor_role", "advisor_email", "advisor_phone"]
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
        file_name="directory_output.csv",
        mime="text/csv"
    )

    if errs:
        with st.expander("Show errors"):
            st.dataframe(pd.DataFrame(errs), use_container_width=True)

    st.markdown('</div>', unsafe_allow_html=True)
