"""Load keyword and source configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("COLLECTOR_CONFIG_DIR", ROOT / "config"))
DATA_DIR = Path(os.environ.get("COLLECTOR_DATA_DIR", ROOT / "data"))


@dataclass
class Group:
    key: str
    label_ta: str
    label_en: str
    terms: list[str]


@dataclass
class Config:
    groups: list[Group]
    require_context: bool
    context_terms: list[str]
    search_queries_en: list[str]
    search_queries_ta: list[str]
    url_slug_terms: list[str]
    wayback: dict
    date_archives: list[dict]
    tld_country: dict
    places: list[dict]
    domain_info: dict = field(default_factory=dict)


def _load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config() -> Config:
    kw = _load_yaml("keywords.yaml")
    src = _load_yaml("sources.yaml")

    groups = []
    for key, g in (kw.get("groups") or {}).items():
        terms = [t for t in (g.get("ta") or []) + (g.get("en") or []) if t]
        groups.append(Group(key, g.get("label_ta", key), g.get("label_en", key), terms))

    ctx = kw.get("context") or {}
    sq = kw.get("search_queries") or {}

    # URL slug terms: English list + percent-encoded Tamil group terms
    slug_terms = list(kw.get("url_slug_terms") or [])
    for g in (kw.get("groups") or {}).values():
        for t in g.get("ta") or []:
            # Tamil slugs are usually the words joined by '-'
            slug_terms.append(quote(t.replace(" ", "-")))

    wayback = src.get("wayback") or {}
    domain_info = {d["domain"]: d for d in wayback.get("domains", [])}
    return Config(
        groups=groups,
        require_context=bool(kw.get("require_context", True)),
        context_terms=[t for t in (ctx.get("ta") or []) + (ctx.get("en") or []) if t],
        search_queries_en=sq.get("en") or [],
        search_queries_ta=sq.get("ta") or [],
        url_slug_terms=slug_terms,
        wayback=wayback,
        date_archives=src.get("date_archives") or [],
        tld_country=src.get("tld_country") or {},
        places=src.get("places") or [],
        domain_info=domain_info,
    )
