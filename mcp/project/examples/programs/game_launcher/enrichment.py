"""Optional, explicit online metadata enrichment.

Nothing here runs unless the user enables it and supplies a provider in
``config.json``.  The default configuration has no provider, so normal use never
touches the network.  Returned values are persisted by ``Catalog`` with a
provenance label and remain lower priority than hand-written local sidecars.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

_PROGRAMS = Path(__file__).resolve().parent.parent
if str(_PROGRAMS) not in sys.path:
    sys.path.insert(0, str(_PROGRAMS))

from .metadata import ResolvedMetadata  # noqa: E402

DEFAULT_ART_URL_TEMPLATE = (
    "https://thumbnails.libretro.com/Atari%20-%20ST/Named_Boxarts/{stem}.png"
)

ALLOWED_FIELDS = {
    "title",
    "year",
    "publisher",
    "genre",
    "description",
    "developer",
    "players",
    "machine",
    "resolution",
    "art",
}


class EnrichmentProvider(Protocol):
    name: str

    def enrich(
        self, entry: Any, metadata: ResolvedMetadata
    ) -> dict[str, Any] | None: ...


@dataclass
class HttpEnricher:
    """Fetch a JSON object for one entry from a user-provided URL template.

    The template may use ``{id}``, ``{title}``, ``{filename}`` or ``{stem}``;
    values are URL-encoded.  Only the allow-listed metadata keys are passed back
    to the catalog, and the caller persists the provider name as provenance.
    """

    url_template: str
    name: str = "http"
    timeout: float = 8.0
    art_url_template: str | None = None

    def _format(self, template: str, entry: Any, metadata: ResolvedMetadata) -> str:
        filename = str(getattr(entry, "filename", "") or "")
        values = {
            "id": urllib.parse.quote(str(getattr(entry, "id", "") or metadata.entry_id)),
            "title": urllib.parse.quote(metadata.title),
            "filename": urllib.parse.quote(filename),
            "stem": urllib.parse.quote(Path(filename).stem if filename else metadata.title),
        }
        try:
            return template.format(**values)
        except (KeyError, IndexError, ValueError):
            return template

    def _url(self, entry: Any, metadata: ResolvedMetadata) -> str | None:
        if not self.url_template:
            return None
        return self._format(self.url_template, entry, metadata)

    def art_url(self, entry: Any, metadata: ResolvedMetadata) -> str | None:
        if not self.art_url_template:
            return None
        value = self._format(self.art_url_template, entry, metadata).strip()
        return value if value.startswith(("http://", "https://")) else None

    def enrich(self, entry: Any, metadata: ResolvedMetadata) -> dict[str, Any] | None:
        url = self._url(entry, metadata)
        if url is None:
            return None
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "hatari-mcp-game-launcher/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError):
            return None
        if not isinstance(payload, dict):
            return None
        values = {
            key: value
            for key, value in payload.items()
            if key in ALLOWED_FIELDS and value not in (None, "")
        }
        if not values:
            return None
        values["provenance"] = {"source": f"online:{self.name}"}
        return values


def default_art_provider() -> HttpEnricher:
    """Built-in, keyless box-art lookup using Libretro thumbnails.

    The launcher only calls this after the online checkbox is enabled.  The
    TOSEC ``{stem}`` usually matches the thumbnail filename exactly, including
    the ``[cr ...]``/``[a]`` release tags used by the corpus.
    """
    return HttpEnricher(
        url_template="",
        name="libretro",
        art_url_template=DEFAULT_ART_URL_TEMPLATE,
    )


def provider_from_config(config: Any) -> EnrichmentProvider | None:
    """Build a provider only when ``online`` is explicitly configured."""
    try:
        online = config.get("online", {}) or {}
    except Exception:
        return None
    if not isinstance(online, dict):
        return None
    provider = str(online.get("provider", "")).casefold()
    if provider in {"libretro", "libretro-thumbnails"}:
        return default_art_provider()
    if provider in {"http", "https", "url"}:
        template = online.get("url") or online.get("url_template")
        art_template = online.get("art_url") or online.get("art_url_template")
        if not isinstance(template, str) or not template.strip():
            template = ""
        if not isinstance(art_template, str) or not art_template.strip():
            art_template = ""
        if not template and not art_template:
            return None
        return HttpEnricher(
            url_template=template.strip(),
            name=str(online.get("name") or "http"),
            timeout=float(online.get("timeout", 8.0)),
            art_url_template=art_template.strip() or None,
        )
    return None


def enrichment_values(
    provider: EnrichmentProvider,
    entry: Any,
    metadata: ResolvedMetadata,
) -> dict[str, Any] | None:
    values = provider.enrich(entry, metadata)
    if not isinstance(values, dict) or not values:
        return None
    provenance = dict(values.get("provenance") or {})
    provenance.setdefault("source", f"online:{getattr(provider, 'name', 'provider')}")
    values["provenance"] = provenance
    return values


def config_example() -> Mapping[str, Any]:
    return {
        "online": {
            "provider": "http",
            "url": "https://example.invalid/api/game/{id}",
            "name": "example",
            "timeout": 8,
        }
    }
