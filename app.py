# -*- coding: utf-8 -*-
"""
Created on Thu Dec 18 13:59:31 2025

@author: AsifurRahman
"""

app_py = r'''
import time
import re
import json
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
import streamlit as st

DEFAULT_HEADERS = {
    "User-Agent": "Inovestor-Directory-Indexer/0.2.2 (contact: ops@inovestor.com)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

STOP_TEXT = {
    "home","accueil","privacy","confidentialité","legal","légal","terms","conditions",
    "accessibility","accessibilité","sitemap","plan du site","search","recherche",
    "market insights","insights","perspectives","blog","news","nouvelles",
    "careers","carrières","cookies","cookie","security","sécurité"
}

STOP_URL_FRAG = [
    "privacy","legal","terms","accessibility","sitemap","search",
    "market","insights","news","blog","careers","cookies","security",
    "perspectives","nouvelles","carriere","carrières"
]

TEAM_PAGE_TEXT_PAT = re.compile(r"\b(our team|notre équipe|team members|membres de l[' ]équipe)\b", re.I)
CONTACT_PAGE_TEXT_PAT = re.compile(r"\b(contact|contactez-nous|nous joindre|communiqu|communicat)\b", re.I)

EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"(\+?\d[\d\-\s().]{7,}\d)")

COMMON_CREDENTIALS = re.compile(
    r"\b(CFA|CFP|CIM|CIMA|MBA|CPA|CA|FCSI|BBA|BA|BSc|MSc|PhD|JD|LLB|LLM|RIA)\b",
    re.I
)

def polite_get(url: str, sleep_s: float = 0.75, timeout: int = 25):
    time.sleep(max(0.0, sleep_s))
    r = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r.text, r.url

def norm_url(u: str) -> str:
    p = urlparse(u)
    return p._replace(fragment="").geturl()

def same_domain(a: str, b: str) -> bool:
    return urlparse(a).netloc.lower() == urlparse(b).netloc.lower()

def looks_like_stop_link(text: str, url: str) -> bool:
    t = (text or "").strip().lower()
    if t in STOP_TEXT:
        return True
    u = (url or "").lower()
    return any(f in u for f in STOP_URL_FRAG)

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
    candidates = []
    for text, url in links:
        if not same_domain(url, base_url):
            continue
        if looks_like_stop_link(text, url):
            continue
        if pattern.search(text) or pattern.search(url):
            candidates.append((text, url))
    candidates.sort(key=lambda x: (len(urlparse(x[1]).path), -len(x[0] or "")))
    return candidates[0][1] if candidates else ""

def extract_people_jsonld(html: str):
    soup = BeautifulSoup(html, "lxml")
    people = []
    scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    for s in scripts:
        raw = s.string
        if not raw:
            continue
        raw = raw.strip()
        try:
            data = json.loads(raw)
        except Exception:
            continue

        def walk(obj):
            if isinstance(obj, dict):
                t = obj.get("@type")
                if isinstance(t, list):
                    is_person = any(str(x).lower() == "person" for x in t)
                else:
                    is_person = str(t).lower() == "person"
                if is_person and obj.get("name"):
                    people.append({
                        "person_name": str(obj.get("name")).strip(),
                        "person_role": str(obj.get("jobTitle") or obj.get("jobtitle") or "").strip(),
                        "person_profile_url": str(obj.get("url") or "").strip(),
                        "source": "jsonld"
                    })
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for it in obj:
                    walk(it)
        walk(data)

    seen = set()
    out = []
    for p in people:
        k = (p["person_name"].lower(), p["person_role"].lower(), p["person_profile_url"])
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out

def normalize_name(raw: str) -> str:
    s = re.sub(r"\s+", " ", (raw or "")).strip()
    if not s or len(s) > 120:
        return ""
    if "," in s:
        s = s.split(",", 1)[0].strip()
    s = COMMON_CREDENTIALS.sub("", s)
    s = re.sub(r"\s+", " ", s).strip(" -–—|")
    return s

def looks_like_name(s: str) -> bool:
    if not s:
        return False
    parts = s.split()
    if len(parts) < 2 or len(parts) > 5:
        return False
    if not re.match(r"^[A-Za-zÀ-ÿ]", s):
        return False
    if EMAIL_RE.search(s) or PHONE_RE.search(s):
        return False
    if s.lower() in ("notre équipe", "our team", "contact", "accueil", "home"):
        return False
    return True

def extract_role_near_heading(h) -> str:
    def clean(s):
        return re.sub(r"\s+", " ", (s or "")).strip()

    candidates = []
    for sib in [h.find_next_sibling(), h.find_next_sibling("p"), h.find_next_sibling("div")]:
        if not sib:
            continue
        txt = clean(sib.get_text("\n", strip=True))
        if txt:
            candidates.append(txt)

    if h.parent:
        txt = clean(h.parent.get_text("\n", strip=True))
        if txt:
            candidates.append(txt)

    for txt in candidates:
        for line in [clean(x) for x in re.split(r"[\n\r]+", txt) if clean(x)]:
            if EMAIL_RE.search(line) or PHONE_RE.search(line):
                continue
            if line.lower().startswith("opens in"):
                continue
            if 3 <= len(line) <= 80:
                return line
    return ""

def extract_people_heuristic(html: str):
    soup = BeautifulSoup(html, "lxml")
    results = []
    for h in soup.find_all(["h2","h3","h4"]):
        raw = h.get_text(" ", strip=True)
        name = normalize_name(raw)
        if not looks_like_name(name):
            continue
        role = extract_role_near_heading(h)
        results.append({
            "person_name": name,
            "person_role": role,
            "person_profile_url": "",
            "source": "heuristic"
        })

    seen = set()
    out = []
    for r in results:
        k = r["person_name"].lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out

def to_team_slug_from_root_url(team_root_url: str) -> str:
    path = urlparse(team_root_url).path.strip("/")
    if not path:
        return ""
    if path.lower().startswith("web/"):
        parts = path.split("/")
        return parts[1].lower() if len(parts) >= 2 else ""
    seg = path.split("/")[0]
    seg = seg.replace("_", "-")
    seg = re.sub(r"[^A-Za-z0-9\-]+", "-", seg)
    seg = re.sub(r"-{2,}", "-", seg).strip("-")
    return seg.lower()

def discover_team_roots_from_branch(seed_url: str, sleep_s: float):
    html, final_url = polite_get(seed_url, sleep_s=sleep_s)
    links = extract_links(html, final_url)

    if "our-investment-advisors-and-their-teams" not in final_url:
        team_list_url = ""
        for _, url in links:
            if "our-investment-advisors-and-their-teams" in (url or "").lower():
                team_list_url = url
                break
        if team_list_url and same_domain(team_list_url, final_url):
            html, final_url = polite_get(team_list_url, sleep_s=sleep_s)
            links = extract_links(html, final_url)

    candidates = []
    for text, url in links:
        if not same_domain(url, final_url):
            continue
        if looks_like_stop_link(text, url):
            continue
        path = urlparse(url).path.strip("/")
        if not path:
            continue
        if path.lower().startswith("web/") and "montreal-" in path.lower():
            continue
        if path.count("/") >= 2:
            continue
        candidates.append((text, url))

    seen = set()
    out = []
    for text, url in candidates:
        if url in seen:
            continue
        seen.add(url)
        out.append({"branch_seed_url": seed_url, "team_root_url": url, "link_text": text})
    return out

def resolve_team_pages(team_root_url: str, sleep_s: float):
    html_root, root_final = polite_get(team_root_url, sleep_s=sleep_s)
    links = extract_links(html_root, root_final)

    team_page = find_best_link(links, root_final, TEAM_PAGE_TEXT_PAT)
    contact_page = find_best_link(links, root_final, CONTACT_PAGE_TEXT_PAT)

    if not team_page:
        for slug in ["our-team","notre-equipe","notre-équipe","team","equipe","équipe"]:
            guess = urljoin(root_final.rstrip("/") + "/", slug)
            try:
                _, u = polite_get(guess, sleep_s=sleep_s)
                team_page = u
                break
            except Exception:
                pass

    if not contact_page:
        for slug in ["contact","contact-us","contactez-nous","nous-joindre"]:
            guess = urljoin(root_final.rstrip("/") + "/", slug)
            try:
                _, u = polite_get(guess, sleep_s=sleep_s)
                contact_page = u
                break
            except Exception:
                pass

    slug = to_team_slug_from_root_url(root_final)
    if slug:
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

def fetch_people_from_url(url: str, sleep_s: float):
    html, final_url = polite_get(url, sleep_s=sleep_s)
    people = extract_people_jsonld(html)
    if not people:
        people = extract_people_heuristic(html)
    return people, final_url

st.set_page_config(page_title="WG Montreal Directory (Useful v2.2)", layout="wide")
st.title("WG Montreal Directory Builder (Useful v2.2)")
st.caption("Fixes the **/Team-Name/** vs **/web/team-name/** mismatch. Extracts **names + roles** and includes team/contact URLs. Does not bulk-collect emails/phones.")

seed_urls_text = st.text_area("Branch seed URLs (one per line)", height=140)
sleep_s = st.slider("Polite delay between requests (seconds)", min_value=0.25, max_value=2.0, value=0.75, step=0.25)
max_team_sites = st.number_input("Max team sites to process per run", min_value=1, max_value=300, value=80, step=5)

if st.button("1) Discover team sites from branch seeds"):
    seeds = [s.strip() for s in seed_urls_text.splitlines() if s.strip()]
    if not seeds:
        st.warning("Please paste at least one BRANCH seed URL.")
        st.stop()

    all_candidates = []
    errs = []
    for s in seeds:
        try:
            all_candidates.extend(discover_team_roots_from_branch(s, sleep_s=sleep_s))
        except Exception as e:
            errs.append({"branch_seed_url": s, "error": str(e)})

    df_candidates = pd.DataFrame(all_candidates).drop_duplicates(subset=["team_root_url"])
    st.session_state["df_candidates"] = df_candidates
    st.subheader("Team site candidates")
    st.dataframe(df_candidates, use_container_width=True)

    if errs:
        st.subheader("Seed fetch errors")
        st.dataframe(pd.DataFrame(errs), use_container_width=True)

if "df_candidates" in st.session_state and not st.session_state["df_candidates"].empty:
    st.divider()
    st.subheader("2) Select team sites to process")
    dfc = st.session_state["df_candidates"].copy()
    dfc["include"] = True

    edited = st.data_editor(
        dfc[["include","branch_seed_url","team_root_url","link_text"]],
        use_container_width=True,
        num_rows="dynamic",
        key="team_selector"
    )

    if st.button("3) Build directory"):
        chosen = edited[edited["include"] == True].head(int(max_team_sites))
        rows = []
        errs = []
        progress = st.progress(0)
        total = len(chosen)

        for i, r in enumerate(chosen.itertuples(index=False), start=1):
            try:
                html_root, root_final, team_page, contact_page, slug = resolve_team_pages(r.team_root_url, sleep_s=sleep_s)
                team_name = page_title(html_root)

                people = []
                source_url_used = ""

                if team_page:
                    people, source_url_used = fetch_people_from_url(team_page, sleep_s=sleep_s)

                if not people and contact_page:
                    people, source_url_used = fetch_people_from_url(contact_page, sleep_s=sleep_s)

                if not people:
                    people = extract_people_jsonld(html_root) or extract_people_heuristic(html_root)
                    source_url_used = root_final

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
                        "advisor_profile_url": "",
                        "source": "no_people_found",
                        "source_page_used": source_url_used
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
                            "advisor_name": p.get("person_name",""),
                            "advisor_role": p.get("person_role",""),
                            "advisor_profile_url": p.get("person_profile_url",""),
                            "source": p.get("source",""),
                            "source_page_used": source_url_used
                        })

            except Exception as e:
                errs.append({"team_root_url": r.team_root_url, "error": str(e)})

            progress.progress(min(1.0, i / max(1, total)))

        df_out = pd.DataFrame(rows)
        st.session_state["df_out"] = df_out

        st.subheader("Directory output")
        st.dataframe(df_out, use_container_width=True)

        if errs:
            st.subheader("Errors")
            st.dataframe(pd.DataFrame(errs), use_container_width=True)

        st.subheader("Export")
        csv = df_out.to_csv(index=False).encode("utf-8")
        st.download_button("Download CSV", data=csv, file_name="wg_directory_output.csv", mime="text/csv")
'''




