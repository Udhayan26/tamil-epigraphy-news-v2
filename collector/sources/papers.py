"""Research papers and journal articles.

Two free sources, no API key needed:

* OpenAlex (api.openalex.org) - an open index of ~250 million scholarly works
  (journals, conference papers, books, theses) with titles, abstracts, authors,
  journal names, DOIs and open-access PDF links.
* OAI-PMH - the standard harvesting interface built into most open-access
  journal systems (OJS). Used for Tamil-language journals that OpenAlex does not
  cover well, e.g. International Research Journal of Tamil.

Every record is kept only if the keyword groups match its title/abstract and it
has a Tamil connection (same rule as for news).
"""
from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta

from ..config import Config
from ..db import DB, now
from ..match import Matcher, is_tamil
from .. import http

log = logging.getLogger(__name__)

OPENALEX = "https://api.openalex.org/works"
PER_PAGE = 100
MAX_PAGES_PER_QUERY = 15          # 1,500 results per query is plenty for specific queries
OAI_NS = {"oai": "http://www.openarchives.org/OAI/2.0/",
          "dc": "http://purl.org/dc/elements/1.1/",
          "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/"}


# ----------------------------------------------------------------------------- helpers
def _abstract(inv: dict | None) -> str:
    """OpenAlex stores abstracts as {word: [positions]}; rebuild the text."""
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def save_paper(db: DB, matcher: Matcher, rec: dict) -> bool:
    """Store a paper if it matches. rec: id,title,abstract,authors,year,date,venue,doi,url,oa_url,source"""
    title, abstract = rec.get("title") or "", rec.get("abstract") or ""
    res = matcher.accept(title, abstract)
    if not res:
        return False
    db.conn.execute(
        "INSERT OR IGNORE INTO papers(id,title,authors,year,pub_date,venue,doi,url,oa_url,abstract,"
        "groups,matched_terms,places,language,source,found_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rec["id"], title.strip(), rec.get("authors", ""), rec.get("year"), rec.get("date", ""),
         rec.get("venue", ""), rec.get("doi", ""), rec.get("url", ""), rec.get("oa_url", ""),
         abstract[:600], json.dumps(res.groups, ensure_ascii=False),
         json.dumps(res.terms, ensure_ascii=False), json.dumps(res.places, ensure_ascii=False),
         "Tamil" if is_tamil(title + " " + abstract[:300]) else "English/Other",
         rec.get("source", ""), now()))
    return True


# ----------------------------------------------------------------------------- OpenAlex
def _openalex_rec(w: dict) -> dict:
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    oa = w.get("open_access") or {}
    doi = (w.get("doi") or "").replace("https://doi.org/", "")
    return {
        "id": w.get("id", ""),
        "title": w.get("title") or w.get("display_name") or "",
        "abstract": _abstract(w.get("abstract_inverted_index")),
        "authors": "; ".join((a.get("author") or {}).get("display_name", "")
                             for a in (w.get("authorships") or [])[:8]),
        "year": w.get("publication_year"),
        "date": w.get("publication_date") or "",
        "venue": src.get("display_name") or w.get("type") or "",
        "doi": doi,
        "url": loc.get("landing_page_url") or (f"https://doi.org/{doi}" if doi else w.get("id", "")),
        "oa_url": oa.get("oa_url") or "",
        "source": "OpenAlex",
    }


def openalex_page(query: str, cursor: str, filters: str) -> tuple[list[dict], str | None]:
    params = {"search": query, "per-page": str(PER_PAGE), "cursor": cursor, "filter": filters,
              "select": "id,doi,title,display_name,publication_year,publication_date,authorships,"
                        "primary_location,open_access,abstract_inverted_index,type"}
    r = http.get(OPENALEX, params=params, timeout=40, retries=2, backoff=3)
    if r is None or r.status_code != 200:
        raise RuntimeError(f"OpenAlex HTTP {getattr(r, 'status_code', None)}")
    data = r.json()
    return [_openalex_rec(w) for w in data.get("results", [])], (data.get("meta") or {}).get("next_cursor")


def _queries(cfg: Config) -> list[str]:
    return list(cfg.paper_queries)


def openalex_step(db: DB, cfg: Config, matcher: Matcher) -> bool:
    """History: one page of one query. False when all queries are done."""
    qs = _queries(cfg)
    st = db.get_state("papers_openalex", {"q": 0, "cursor": "*", "page": 0})
    if st["q"] >= len(qs):
        return False
    try:
        recs, nxt = openalex_page(qs[st["q"]], st["cursor"], f"from_publication_date:{cfg.papers_from_year}-01-01")
        kept = sum(save_paper(db, matcher, r) for r in recs)
        db.commit()
        log.info("openalex %r page %d: %d results, %d kept", qs[st["q"]], st["page"] + 1, len(recs), kept)
        st["page"] += 1
        if not nxt or not recs or st["page"] >= MAX_PAGES_PER_QUERY:
            st = {"q": st["q"] + 1, "cursor": "*", "page": 0}
        else:
            st["cursor"] = nxt
    except Exception as e:
        st["errors"] = st.get("errors", 0) + 1
        log.warning("openalex %r: %s", qs[st["q"]], e)
        if st["errors"] >= 3:
            st = {"q": st["q"] + 1, "cursor": "*", "page": 0}
        time.sleep(3)
    db.set_state("papers_openalex", st)
    return True


def openalex_recent(db: DB, cfg: Config, matcher: Matcher, days: int = 10,
                    budget_min: float = 5) -> int:
    """Papers added to OpenAlex in the last few days."""
    stop_at = time.time() + budget_min * 60
    since = (date.today() - timedelta(days=days)).isoformat()
    kept = 0
    for q in _queries(cfg):
        if time.time() > stop_at:
            break
        try:
            recs, _ = openalex_page(q, "*", f"from_created_date:{since}")
            kept += sum(save_paper(db, matcher, r) for r in recs)
        except Exception as e:
            log.warning("openalex recent %r: %s", q, e)
    db.commit()
    return kept


# ----------------------------------------------------------------------------- OAI-PMH journals
def parse_oai(xml_text: str, journal: str) -> tuple[list[dict], str | None]:
    root = ET.fromstring(xml_text)
    recs = []
    for rec in root.iter(f"{{{OAI_NS['oai']}}}record"):
        header = rec.find("oai:header", OAI_NS)
        if header is None or header.get("status") == "deleted":
            continue
        ident = header.findtext("oai:identifier", default="", namespaces=OAI_NS)
        md = rec.find("oai:metadata/oai_dc:dc", OAI_NS)
        if md is None:
            continue

        def all_(tag):
            return [_clean(e.text) for e in md.findall(f"dc:{tag}", OAI_NS) if e.text and e.text.strip()]

        titles, descs = all_("title"), all_("description")
        links = [i for i in all_("identifier") if i.startswith("http")]
        dates = all_("date")
        d = dates[0][:10] if dates else ""
        doi = next((i.split("doi.org/")[-1] for i in links if "doi.org/" in i), "")
        recs.append({
            "id": ident, "title": " / ".join(titles[:2]), "abstract": " ".join(descs)[:3000],
            "authors": "; ".join(all_("creator")[:8]), "year": int(d[:4]) if d[:4].isdigit() else None,
            "date": d, "venue": journal, "doi": doi,
            "url": next((i for i in links if "doi.org" not in i), links[0] if links else ""),
            "oa_url": "", "source": f"Journal: {journal}",
        })
    token_el = root.find(".//oai:resumptionToken", OAI_NS)
    token = token_el.text.strip() if token_el is not None and token_el.text else None
    return recs, token


def oai_step(db: DB, cfg: Config, matcher: Matcher) -> bool:
    """Harvest one page from the next journal. False when every journal is up to date."""
    for j in cfg.journals:
        key = f"oai:{j['name']}"
        st = db.get_state(key, {"token": None, "from": None, "done_today": None})
        today = date.today().isoformat()
        if st.get("done_today") == today:
            continue
        params = ({"verb": "ListRecords", "resumptionToken": st["token"]} if st.get("token") else
                  {"verb": "ListRecords", "metadataPrefix": "oai_dc", **({"from": st["from"]} if st.get("from") else {})})
        try:
            r = http.get(j["oai"], params=params, timeout=60, retries=2, backoff=3)
            if r is None or r.status_code != 200:
                raise RuntimeError(f"HTTP {getattr(r, 'status_code', None)}")
            if "noRecordsMatch" in r.text:
                recs, token = [], None
            else:
                recs, token = parse_oai(r.text, j["name"])
            kept = sum(save_paper(db, matcher, rec) for rec in recs)
            db.commit()
            log.info("journal %s: %d records, %d kept%s", j["name"], len(recs), kept, " (more)" if token else "")
            st["token"] = token
            st.pop("errors", None)
            if not token:
                st.update({"from": today, "done_today": today})
        except Exception as e:
            st["errors"] = st.get("errors", 0) + 1
            log.warning("journal %s: %s", j["name"], e)
            if st["errors"] >= 3:
                st.update({"token": None, "done_today": today, "errors": 0})
        db.set_state(key, st)
        return True
    return False


def progress(db: DB, cfg: Config) -> str:
    n = db.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    st = db.get_state("papers_openalex", {"q": 0})
    qs = _queries(cfg)
    oa = "done" if st["q"] >= len(qs) else f"query {st['q'] + 1}/{len(qs)}"
    return f"research papers kept: {n} (OpenAlex history {oa}; journals: {len(cfg.journals)})"
