"""Registry of libraries the user can choose from.

`status` is truthful: only connectors that exist are "available". Sources whose
access terms are unconfirmed are "unconfirmed" and shown as not available yet.
"""

from typing import Literal, TypedDict


class Library(TypedDict):
    id: str
    name: str
    tier: Literal["open", "membership"]
    status: Literal["available", "planned", "unconfirmed"]
    languages: list[str]
    # How a user can add access: api_key, proxy_url, upload. Empty for open sources.
    access_modes: list[str]


LIBRARIES: list[Library] = [
    {"id": "openalex", "name": "OpenAlex", "tier": "open", "status": "available",
     "languages": ["en", "ar", "fr", "zh", "de", "es"], "access_modes": []},
    {"id": "crossref", "name": "Crossref", "tier": "open", "status": "available",
     "languages": ["en", "ar", "fr", "zh", "de", "es"], "access_modes": []},
    {"id": "arxiv", "name": "arXiv", "tier": "open", "status": "available",
     "languages": ["en"], "access_modes": []},
    {"id": "europepmc", "name": "Europe PMC / PubMed", "tier": "open", "status": "available",
     "languages": ["en"], "access_modes": []},
    {"id": "semanticscholar", "name": "Semantic Scholar", "tier": "open", "status": "planned",
     "languages": ["en"], "access_modes": []},
    {"id": "core", "name": "CORE", "tier": "open", "status": "planned",
     "languages": ["en", "fr", "de", "es"], "access_modes": []},
    {"id": "unpaywall", "name": "Unpaywall (legal open copies)", "tier": "open",
     "status": "planned", "languages": [], "access_modes": []},
    {"id": "ieee", "name": "IEEE Xplore", "tier": "membership", "status": "planned",
     "languages": ["en"], "access_modes": ["api_key", "proxy_url", "upload"]},
    {"id": "elsevier", "name": "Elsevier / ScienceDirect", "tier": "membership",
     "status": "planned", "languages": ["en"], "access_modes": ["api_key", "proxy_url", "upload"]},
    {"id": "springer", "name": "Springer Nature", "tier": "membership", "status": "planned",
     "languages": ["en", "de"], "access_modes": ["api_key", "proxy_url", "upload"]},
    {"id": "wiley", "name": "Wiley", "tier": "membership", "status": "planned",
     "languages": ["en"], "access_modes": ["api_key", "proxy_url", "upload"]},
    {"id": "cnki", "name": "CNKI", "tier": "membership", "status": "unconfirmed",
     "languages": ["zh"], "access_modes": ["proxy_url", "upload"]},
    {"id": "wanfang", "name": "Wanfang", "tier": "membership", "status": "unconfirmed",
     "languages": ["zh"], "access_modes": ["proxy_url", "upload"]},
    {"id": "almanhal", "name": "Al Manhal", "tier": "membership", "status": "unconfirmed",
     "languages": ["ar"], "access_modes": ["proxy_url", "upload"]},
    {"id": "dialnet", "name": "Dialnet", "tier": "open", "status": "unconfirmed",
     "languages": ["es"], "access_modes": []},
]
