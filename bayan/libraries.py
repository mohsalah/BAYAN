"""Registry of libraries the user can choose from.

`status` is truthful: only connectors that exist are "available". Sources whose
access terms are unconfirmed are "unconfirmed" and shown as not available yet.
`role` says how a library is used: "search" libraries are picked for a search, "lookup"
libraries (Crossref) are used automatically and cannot be chosen as a search source.
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
    role: Literal["search", "lookup"]


LIBRARIES: list[Library] = [
    {"id": "openalex", "name": "OpenAlex", "tier": "open", "status": "available",
     "languages": ["en", "ar", "fr", "zh", "de", "es"], "access_modes": [], "role": "search"},
    {"id": "crossref", "name": "Crossref", "tier": "open", "status": "available",
     "languages": ["en", "ar", "fr", "zh", "de", "es"], "access_modes": [], "role": "lookup"},
    {"id": "arxiv", "name": "arXiv", "tier": "open", "status": "available",
     "languages": ["en"], "access_modes": [], "role": "search"},
    {"id": "europepmc", "name": "Europe PMC / PubMed", "tier": "open", "status": "available",
     "languages": ["en"], "access_modes": [], "role": "search"},
    {"id": "semanticscholar", "name": "Semantic Scholar", "tier": "open", "status": "planned",
     "languages": ["en"], "access_modes": [], "role": "search"},
    {"id": "core", "name": "CORE", "tier": "open", "status": "planned",
     "languages": ["en", "fr", "de", "es"], "access_modes": [], "role": "search"},
    {"id": "unpaywall", "name": "Unpaywall (legal open copies)", "tier": "open",
     "status": "planned", "languages": [], "access_modes": [], "role": "search"},
    {"id": "ieee", "name": "IEEE Xplore", "tier": "membership", "status": "planned",
     "languages": ["en"], "access_modes": ["api_key", "proxy_url", "upload"], "role": "search"},
    {"id": "elsevier", "name": "Elsevier / ScienceDirect", "tier": "membership",
     "status": "planned", "languages": ["en"],
     "access_modes": ["api_key", "proxy_url", "upload"], "role": "search"},
    {"id": "springer", "name": "Springer Nature", "tier": "membership", "status": "planned",
     "languages": ["en", "de"], "access_modes": ["api_key", "proxy_url", "upload"],
     "role": "search"},
    {"id": "wiley", "name": "Wiley", "tier": "membership", "status": "planned",
     "languages": ["en"], "access_modes": ["api_key", "proxy_url", "upload"], "role": "search"},
    {"id": "cnki", "name": "CNKI", "tier": "membership", "status": "unconfirmed",
     "languages": ["zh"], "access_modes": ["proxy_url", "upload"], "role": "search"},
    {"id": "wanfang", "name": "Wanfang", "tier": "membership", "status": "unconfirmed",
     "languages": ["zh"], "access_modes": ["proxy_url", "upload"], "role": "search"},
    {"id": "almanhal", "name": "Al Manhal", "tier": "membership", "status": "unconfirmed",
     "languages": ["ar"], "access_modes": ["proxy_url", "upload"], "role": "search"},
    {"id": "dialnet", "name": "Dialnet", "tier": "open", "status": "unconfirmed",
     "languages": ["es"], "access_modes": [], "role": "search"},
]
