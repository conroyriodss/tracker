#!/usr/bin/env python3
"""
verify_external_ids.py  (v16.0)

Google Colab-ready verifier and candidate finder for YAML repository cards.

v16 repository-context integration layered on the retained v10 matching semantics
and v12 transaction/review hardening:

  - Environment-aware paths through common.py (dev/staging/prod).
  - Shared re3data catalog cache is read-only in dev/staging unless
    --allow-cache-write is supplied deliberately.
  - Credentials are ignored outside production unless explicitly overridden.
  - Direct apply is one locked, atomic, rollback-capable card transaction.
  - Every run emits immutable artifacts under runs/verify_ids/<timestamp>/.
  - Structured verify_adjudications.csv rejections suppress the same stable
    candidate from later review queues.

The bidirectional corroboration behavior introduced in v10 is retained:

BIDIRECTIONAL CROSS-SOURCE CORROBORATION (new in v10)

  CONFIRMED gap (2026-09, LONI/R229 spot-check after --apply): the v9
  corroboration function only flowed ONE WAY — a re3data candidate's
  confidence could be boosted when it matched an RRID/FAIRsharing DOI
  found elsewhere in the same run, but a re3data record's own listed
  RRID/DOI never flowed back to boost the RRID/FAIRsharing candidate
  itself. Concretely: LONI's re3data record (r3d100012840, confidence
  0.95, applied to the card) independently lists RRID:SCR_007283 as its
  own repositoryIdentifier — but LONI's RRID search that same run found
  exactly that RRID at only raw confidence 0.55 (a shared_acronym-style
  weak name match), which stayed stuck at "review-eligible only" even
  though the newly-applied re3data record corroborates it. The result:
  a card ended up with a re3data link whose own metadata confirms an
  RRID that was never added to the card.

  Fix: apply_cross_source_corroboration() is now symmetric. It builds a
  single pass over all three sources' top candidates plus the card's
  already-confirmed links, and for EACH source's top candidate, checks
  whether ANY other source (existing link OR another candidate this run)
  independently corroborates it:
    - a re3data candidate is boosted if its own listed RRID/DOI matches
      an RRID/FAIRsharing candidate or existing link
    - an RRID candidate is boosted if a re3data candidate's or existing
      re3data link's listed RRID matches it
    - a FAIRsharing candidate is boosted if a re3data candidate's or
      existing re3data link's listed DOI matches it
  All three directions use the same confidence bump (0.95) and the same
  match_basis suffix convention (+corroborated_<kind>) as before, so
  has_independent_evidence() and evaluate_direct_apply() need no changes
  — they already treat any "+corroborated_..." suffix as independent
  evidence regardless of which source carries it.

Everything else retained from v9:

  SHARED-INSTITUTIONAL-HOST GUARD: a homepage match against a domain in
  SHARED_INSTITUTIONAL_HOSTS (ncbi.nlm.nih.gov, nlm.nih.gov, nih.gov,
  ebi.ac.uk, cancer.gov) is tagged "shared_institutional_host" and
  explicitly excluded from independent-evidence status — confirmed
  necessary after NDARO and Protein Family Models both matched "GenBank"
  purely on shared NCBI domain.

  RE3DATA FULL-CATALOG LOCAL SCORING: re3data's search API does not rank
  by relevance (confirmed via direct API testing — a correct record can
  sit at position 1000+ of ~3500 regardless of query wording). The full
  catalog is downloaded once, cached to JSON with a timestamp, and every
  card's search scores locally against that cache. Reused until stale
  (default 14 days) or --refresh-catalog is passed.

  DIRECT-APPLY POLICY: confidence >= 0.95 alone is not sufficient. A
  candidate must also carry independent evidence (exact/acronym name
  match, genuine non-institutional homepage match, or cross-source
  corroboration). Every candidate carries `eligible` (review threshold)
  and `direct_apply_eligible` (write-safe) as separate flags.

Retained from v7 and earlier: --mode {verify,search,full}, --sources
subset, real FAIRsharing search (JWT sign-in flow), real SciCrunch search
(RIN Elasticsearch), per-card CONCURRENT search across missing id_types,
RE3DATA_DENYLIST, STRUCTURAL_BOOST_MIN_RATIO floor, multi-source-per-card
summary, structured RunTelemetry, --strict-errors.

Defaults:
    Project root:   selected by NIH_TRACKER_ENV (dev by default)
    Cards/report/proposals/run report: under that environment's project root
    re3data cache:  NIH_Tracker_shared/cache/re3data_catalog_cache.json
                    (read-only outside prod unless explicitly overridden)

Supported registry link types:
    rrid
    fairsharing_doi
    re3data

Security:
- Set FS_U / FS_PWD / SCICRUNCH_API_KEY in environment variables (e.g. via
  Colab's userdata.get() into os.environ), not in source code. Credentials are
  ignored outside prod unless NIH_TRACKER_ALLOW_NONPROD_CREDENTIALS=1.
- This script does not print, write, or expose any secret value.
- YAML cards are read-only unless --apply is explicitly provided; production
  writes also require NIH_TRACKER_PROD_WRITE_CONFIRMATION.

Typical Colab usage:

    !python verify_external_ids.py --smoke-test-only --verbose
    !python verify_external_ids.py --mode search --only-missing --verbose
    !python verify_external_ids.py --mode search --only-missing --apply --verbose
    !python verify_external_ids.py --mode search --only-missing --refresh-catalog --verbose
"""

import argparse
import csv
import datetime as dt
import difflib
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote

import requests
import yaml

try:
    from lxml import etree as LXML_ETREE
except ImportError:
    LXML_ETREE = None

try:
    import common as tracker_common
except ImportError:  # supports standalone use when only this file is copied
    tracker_common = None


def _truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _sha256_file(path):
    if tracker_common is not None:
        return tracker_common.sha256_file(path) or ""
    source = Path(path)
    if not source.is_file():
        return ""
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


TRACKER_ENV = getattr(tracker_common, "ENV", os.getenv("NIH_TRACKER_ENV", "dev"))
TRACKER_PROJECT = Path(
    getattr(
        tracker_common,
        "PROJECT",
        os.getenv("NIH_TRACKER", "/content/drive/MyDrive/NIH_Tracker_dev"),
    )
)
DEFAULT_CARDS = str(getattr(tracker_common, "CARDS", TRACKER_PROJECT / "cards"))
DEFAULT_REPORT = str(TRACKER_PROJECT / "verify_report.csv")
DEFAULT_PROPOSALS = str(TRACKER_PROJECT / "verify_external_ids_proposals.yaml")
DEFAULT_RUN_REPORT = str(TRACKER_PROJECT / "verify_run_report.json")
DEFAULT_RE3DATA_CATALOG_CACHE = str(
    getattr(
        tracker_common,
        "RE3DATA_CATALOG_CACHE",
        TRACKER_PROJECT / "re3data_catalog_cache.json",
    )
)
DEFAULT_RE3DATA_CACHE_READ_ONLY = bool(
    getattr(tracker_common, "RE3DATA_CACHE_READ_ONLY", TRACKER_ENV != "prod")
)
DEFAULT_RE3DATA_SEEDS = str(TRACKER_PROJECT / "reference" / "re3data_confirmed_mappings.csv")
DEFAULT_VERIFY_ADJUDICATIONS = str(
    getattr(
        tracker_common,
        "VERIFY_ADJUDICATIONS_CSV",
        TRACKER_PROJECT / "inputs" / "verify_adjudications.csv",
    )
)


def credential_use_allowed():
    return bool(
        getattr(tracker_common, "CREDENTIALS_ALLOWED", False)
        or TRACKER_ENV == "prod"
        or _truthy(os.getenv("NIH_TRACKER_ALLOW_NONPROD_CREDENTIALS"))
    )


def _atomic_write_text(path, text):
    path = Path(path)
    if tracker_common is not None:
        tracker_common.atomic_write_text(path, text)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_write_json(path, payload):
    _atomic_write_text(path, json.dumps(payload, indent=2, default=str) + "\n")

USER_AGENT = "NIH-Tracker-External-ID-Verifier/16.0 (registry QA tool; Google Colab)"
TIMEOUT = 30
RETRIES = 3
MIN_DELAY = 1.0
FUZZY_THRESHOLD = 0.60
CANDIDATE_THRESHOLD = 0.82

SOURCE_CANDIDATE_THRESHOLDS = {
    "re3data": 0.50,
    "fairsharing_doi": CANDIDATE_THRESHOLD,
    "rrid": CANDIDATE_THRESHOLD,
}
MAX_SEARCH_CANDIDATES = 25

RE3DATA_CATALOG_MAX_AGE_DAYS = 14
RE3DATA_CATALOG_SCHEMA = "nih-tracker-re3data-catalog/v2"

# Confirmed by manual review: re3data records that repeatedly surface as
# generic/irrelevant top hits regardless of the query, rather than
# genuine matches. Extend whenever a review confirms a new one.
RE3DATA_DENYLIST = {
    "r3d100000018",  # "Cell Centered Database" — confirmed false positive
                      # for both CTD (R166) and LONI (R229), 2026-09 review.
}

# Minimum RAW similarity ratio required before the structural-match
# confidence boost (acronym/containment/exact-normalized match) can fire.
# Set below the GWAS Catalog case (raw ratio 0.35), confirmed by manual
# review to be a CORRECT boost despite low string similarity.
STRUCTURAL_BOOST_MIN_RATIO = 0.30

# Hosts confirmed to serve many unrelated NIH/NCBI resources under the
# same domain (e.g. GenBank, NDARO, and Protein Family Models can all
# report an ncbi.nlm.nih.gov homepage despite being distinct resources).
# A homepage-host match against one of these is NOT independent evidence
# on its own — confirmed 2026-09 when both NDARO and Protein Family
# Models matched "GenBank" purely because they share NCBI's domain.
# Mirrors the shared-host guard already used in step_promote_review.py
# for the same reason. Extend as more are confirmed.
SHARED_INSTITUTIONAL_HOSTS = {
    "ncbi.nlm.nih.gov", "nlm.nih.gov", "nih.gov",
    "ebi.ac.uk", "cancer.gov",
}

# Direct-apply policy: confidence alone is not sufficient to write a card
# automatically. See has_independent_evidence() / evaluate_direct_apply().
DIRECT_APPLY_CONFIDENCE = 0.95
INDEPENDENT_EVIDENCE_BASES = {
    "exact_normalized",
    "exact_normalized_alias",
    "curated_exact_mapping",
    "cross_registry_identifier_rrid",
    "cross_registry_identifier_fairsharing_doi",
    "cross_registry_identifiers",
    "resolved_matches_card_acronym",
    "card_matches_resolved_acronym",
}

ALL_SOURCES = ("rrid", "fairsharing", "re3data")
SOURCE_TO_ID_TYPE = {"rrid": "rrid", "fairsharing": "fairsharing_doi", "re3data": "re3data"}
ID_TYPE_TO_SOURCE = {v: k for k, v in SOURCE_TO_ID_TYPE.items()}

PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)
TRAILING_PAREN_RE = re.compile(r"\s*\([^()]+\)\s*$")
ACRONYM_RE = re.compile(r"\(([^()]{2,30})\)")
R3D_RE = re.compile(r"^r3d\d+$", re.IGNORECASE)
RRID_RE = re.compile(r"^(?:RRID:)?SCR_\d+$", re.IGNORECASE)

TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
OG_TITLE_RE = re.compile(
    r'<meta[^>]+(?:property|name)\s*=\s*["\']og:title["\'][^>]+content\s*=\s*["\'](.*?)["\']',
    re.I | re.S,
)
OG_TITLE_RE_REVERSED = re.compile(
    r'<meta[^>]+content\s*=\s*["\'](.*?)["\'][^>]+(?:property|name)\s*=\s*["\']og:title["\']',
    re.I | re.S,
)


def info(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def debug(message, verbose=False):
    if verbose:
        print(f"[{time.strftime('%H:%M:%S')}] DEBUG: {message}", file=sys.stderr, flush=True)


def in_colab():
    try:
        import google.colab  # noqa: F401
        return True
    except ImportError:
        return False


def drive_ready():
    return Path("/content/drive").is_dir() and Path("/content/drive/MyDrive").is_dir()


def ensure_drive(verbose=False):
    if drive_ready():
        debug("Google Drive already mounted at /content/drive/MyDrive.", verbose)
        return True
    if not in_colab():
        info("Not running in Colab; Drive mount skipped.")
        return True
    info("Google Drive not mounted; opening Colab authorization prompt.")
    try:
        from google.colab import drive
        drive.mount("/content/drive", force_remount=False)
    except Exception as exc:
        print(f"Warning: Google Drive mount failed: {exc}", file=sys.stderr)
        return False
    if drive_ready():
        info("Google Drive mounted successfully.")
        return True
    print("Warning: Drive mount completed but MyDrive is unavailable.", file=sys.stderr)
    return False


# ─── structured error/run tracking ──────────────────────────────────────────
class RunTelemetry:
    """Accumulates structured errors and per-source counters across the run."""

    def __init__(self):
        self.error_counts = Counter()
        self.errors = []
        self.source_calls = Counter()
        self.source_failures = Counter()
        self.max_detail_rows = 500

    def record_call(self, source):
        self.source_calls[source] += 1

    def record_error(self, source, error_class, detail, repo_uid="", id_type=""):
        self.error_counts[(source, error_class)] += 1
        self.source_failures[source] += 1
        if len(self.errors) < self.max_detail_rows:
            self.errors.append({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "source": source,
                "error_class": error_class,
                "repo_uid": repo_uid,
                "id_type": id_type,
                "detail": str(detail)[:500],
            })

    def has_errors(self):
        return bool(self.error_counts)

    def summary(self):
        return {
            "total_calls_by_source": dict(self.source_calls),
            "total_failures_by_source": dict(self.source_failures),
            "error_counts_by_source_and_class": {
                f"{src}:{cls}": n for (src, cls), n in self.error_counts.items()
            },
            "error_detail_rows_captured": len(self.errors),
            "error_detail_rows_truncated": len(self.errors) >= self.max_detail_rows,
        }

    def write_report(self, path, extra=None):
        payload = {
            "generated_at_local": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "environment": TRACKER_ENV,
            "project": str(TRACKER_PROJECT),
            "summary": self.summary(),
            "errors": self.errors,
        }
        if extra:
            payload.update(extra)
        _atomic_write_json(path, payload)


class Http:
    """Polite resilient HTTP client with request spacing, retries, and timeout."""

    def __init__(self, delay, verbose=False, telemetry=None):
        self.delay = max(MIN_DELAY, float(delay))
        self.verbose = verbose
        self.last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.telemetry = telemetry or RunTelemetry()

    def _throttle(self):
        elapsed = time.monotonic() - self.last_request
        if elapsed < self.delay:
            wait = self.delay - elapsed
            debug(f"Rate limiting for {wait:.2f}s.", self.verbose)
            time.sleep(wait)

    def get(self, url, *, headers=None, params=None, auth=None, source="unknown",
            repo_uid="", id_type=""):
        last_error = ""
        self.telemetry.record_call(source)

        for attempt in range(1, RETRIES + 1):
            self._throttle()
            try:
                self.last_request = time.monotonic()
                debug(f"GET [{attempt}/{RETRIES}] {url} params={params or {}} "
                     f"auth={'yes' if auth else 'no'}", self.verbose)

                response = self.session.get(
                    url, headers=headers or {}, params=params, auth=auth,
                    timeout=TIMEOUT, allow_redirects=True,
                )

                debug(f"HTTP={response.status_code} final_url={response.url} "
                     f"content_type={response.headers.get('Content-Type', '')!r} "
                     f"length={len(response.content)}", self.verbose)

                if response.status_code in {429, 500, 502, 503, 504}:
                    if attempt < RETRIES:
                        backoff = 2 ** (attempt - 1)
                        debug(f"Retryable HTTP {response.status_code}; sleeping {backoff}s.",
                             self.verbose)
                        time.sleep(backoff)
                        continue
                    self.telemetry.record_error(
                        source, f"http_{response.status_code}_retries_exhausted",
                        f"{url} gave up after {RETRIES} attempts", repo_uid, id_type)

                return response, ""

            except requests.Timeout as exc:
                last_error = str(exc)
                if attempt >= RETRIES:
                    self.telemetry.record_error(source, "timeout", last_error, repo_uid, id_type)
                else:
                    time.sleep(2 ** (attempt - 1))
            except requests.ConnectionError as exc:
                last_error = str(exc)
                if attempt >= RETRIES:
                    self.telemetry.record_error(source, "connection_error", last_error, repo_uid, id_type)
                else:
                    time.sleep(2 ** (attempt - 1))
            except requests.RequestException as exc:
                last_error = str(exc)
                if attempt >= RETRIES:
                    self.telemetry.record_error(source, "request_exception", last_error, repo_uid, id_type)
                else:
                    time.sleep(2 ** (attempt - 1))

        return None, last_error or "request failed after retries"

    def post_json(self, url, *, json_body, headers=None, source="unknown",
                  repo_uid="", id_type=""):
        last_error = ""
        self.telemetry.record_call(source)

        for attempt in range(1, RETRIES + 1):
            self._throttle()
            try:
                self.last_request = time.monotonic()
                debug(f"POST [{attempt}/{RETRIES}] {url} json_keys={list(json_body.keys())}",
                     self.verbose)

                response = self.session.post(
                    url, json=json_body, headers=headers or {}, timeout=TIMEOUT,
                    allow_redirects=True,
                )

                debug(f"HTTP={response.status_code} final_url={response.url} "
                     f"content_type={response.headers.get('Content-Type', '')!r} "
                     f"length={len(response.content)}", self.verbose)

                if response.status_code in {429, 500, 502, 503, 504}:
                    if attempt < RETRIES:
                        time.sleep(2 ** (attempt - 1))
                        continue
                    self.telemetry.record_error(
                        source, f"http_{response.status_code}_retries_exhausted",
                        f"{url} gave up after {RETRIES} attempts", repo_uid, id_type)

                return response, ""

            except requests.Timeout as exc:
                last_error = str(exc)
                if attempt >= RETRIES:
                    self.telemetry.record_error(source, "timeout", last_error, repo_uid, id_type)
                else:
                    time.sleep(2 ** (attempt - 1))
            except requests.ConnectionError as exc:
                last_error = str(exc)
                if attempt >= RETRIES:
                    self.telemetry.record_error(source, "connection_error", last_error, repo_uid, id_type)
                else:
                    time.sleep(2 ** (attempt - 1))
            except requests.RequestException as exc:
                last_error = str(exc)
                if attempt >= RETRIES:
                    self.telemetry.record_error(source, "request_exception", last_error, repo_uid, id_type)
                else:
                    time.sleep(2 ** (attempt - 1))

        return None, last_error or "request failed after retries"


# ─── name normalization / matching ───────────────────────────────────────────
def normalized(value):
    if not value:
        return ""
    value = html.unescape(str(value)).strip().lower()
    wrappers = ("fairsharing record for:", "fairsharing record:", "fairsharing:",
                "resource:", "database:", "repository:")
    for wrapper in wrappers:
        if value.startswith(wrapper):
            value = value[len(wrapper):].strip()
    value = TRAILING_PAREN_RE.sub("", value)
    value = value.replace("'", "")
    value = PUNCTUATION_RE.sub(" ", value)
    value = re.sub(r"\s+", " ", value).strip()
    singular_aliases = {"polymorphisms": "polymorphism", "databases": "database",
                        "repositories": "repository", "resources": "resource",
                        "atlases": "atlas", "maps": "map"}
    tokens = [singular_aliases.get(token, token) for token in value.split()]
    return " ".join(tokens)


def acronyms(value):
    if not value:
        return set()
    result = set()
    for match in ACRONYM_RE.finditer(str(value)):
        token = normalized(match.group(1))
        if token:
            result.add(token)
    return result


def similarity(left, right):
    left = normalized(left)
    right = normalized(right)
    if not left or not right:
        return ""
    return round(difflib.SequenceMatcher(None, left, right).ratio(), 4)


def name_match(card_name, registry_name, ratio):
    card = normalized(card_name)
    registry = normalized(registry_name)
    if not card or not registry:
        return False, "missing_name"
    if card == registry:
        return True, "exact_normalized"
    if len(card) >= 4 and len(registry) >= 4:
        if card in registry or registry in card:
            return True, "name_containment"
    card_acronyms = acronyms(card_name)
    registry_acronyms = acronyms(registry_name)
    if registry in card_acronyms:
        return True, "resolved_matches_card_acronym"
    if card in registry_acronyms:
        return True, "card_matches_resolved_acronym"
    if card_acronyms.intersection(registry_acronyms):
        return True, "shared_acronym"
    card_tokens = set(card.split())
    registry_tokens = set(registry.split())
    stopwords = {"the", "of", "and", "for", "in", "a", "an", "database", "data",
                "repository", "resource", "system", "portal"}
    meaningful_card_tokens = card_tokens - stopwords
    meaningful_registry_tokens = registry_tokens - stopwords
    if meaningful_card_tokens and meaningful_registry_tokens:
        overlap = meaningful_card_tokens.intersection(meaningful_registry_tokens)
        smaller_set = min(len(meaningful_card_tokens), len(meaningful_registry_tokens))
        overlap_ratio = len(overlap) / smaller_set if smaller_set else 0.0
        if overlap_ratio >= 0.75:
            return True, "meaningful_token_overlap"
    if ratio != "" and float(ratio) >= FUZZY_THRESHOLD:
        return True, "fuzzy_ratio"
    return False, "below_threshold"


def html_title(body):
    for regex in (OG_TITLE_RE, OG_TITLE_RE_REVERSED, TITLE_RE):
        match = regex.search(body or "")
        if match:
            value = html.unescape(match.group(1))
            value = re.sub(r"\s+", " ", value).strip()
            if value:
                return value
    return ""


def xml_first_text(root, wanted_local_name):
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == wanted_local_name:
            if element.text and element.text.strip():
                return element.text.strip()
    return ""


def xml_all_text(root, wanted_local_names):
    wanted = {str(name) for name in wanted_local_names}
    values, seen = [], set()
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] not in wanted:
            continue
        text = (element.text or "").strip()
        key = normalized(text)
        if text and key and key not in seen:
            seen.add(key)
            values.append(text)
    return values


def parse_re3data_record_details(xml_content):
    """Return primary name, aliases, homepage, and related identifiers.

    re3data exposes alternate repository names in ``additionalName`` fields.
    Retaining them is required for deterministic matching of records such as
    NDA/NIMH Data Archive and FITBIR whose card name is an official short name
    rather than the record's primary expanded name.
    """
    try:
        root = (LXML_ETREE.fromstring(xml_content) if LXML_ETREE is not None
                else ET.fromstring(xml_content))
    except Exception:
        return {"name": "", "aliases": [], "homepage": "", "fairsharing_doi": [], "rrid": []}
    names = xml_all_text(root, {"repositoryName"})
    aliases = xml_all_text(root, {"additionalName"})
    primary = names[0] if names else ""
    aliases = [value for value in [*names[1:], *aliases] if normalized(value) != normalized(primary)]
    related = parse_re3data_related_identifiers(xml_content)
    return {
        "name": primary,
        "aliases": aliases,
        "homepage": xml_first_text(root, "repositoryURL"),
        "fairsharing_doi": related["fairsharing_doi"],
        "rrid": related["rrid"],
    }


def parse_re3data_record(xml_content):
    details = parse_re3data_record_details(xml_content)
    return details["name"], details["homepage"]


def parse_re3data_related_identifiers(xml_content):
    """
    Extract cross-registry identifiers a re3data record lists about itself
    (repositoryIdentifier elements). CONFIRMED via direct API inspection
    (2026-09) that real records store BARE values with no type prefix,
    e.g. for CTD (r3d100011530):
        10.25504/FAIRsharing.h3tjtr
        OMICS_01578
        SCR_006530
        nif-0000-02683
    Classification is done by VALUE SHAPE, not by string prefix:
      - a DOI-shaped value (matches ^10\\.\\d+/) is a FAIRsharing DOI
      - a value containing "SCR_<digits>" is treated as an RRID

    Returns {"fairsharing_doi": [...], "rrid": [...]}.
    """
    result = {"fairsharing_doi": [], "rrid": []}
    try:
        root = (LXML_ETREE.fromstring(xml_content) if LXML_ETREE is not None
                else ET.fromstring(xml_content))
    except Exception:
        return result

    for element in root.iter():
        local_name = element.tag.rsplit("}", 1)[-1]
        if local_name != "repositoryIdentifier":
            continue
        text = (element.text or "").strip()
        if not text:
            continue

        if re.match(r"^10\.\d{4,9}/", text):
            result["fairsharing_doi"].append(text)
            continue

        rrid_match = re.search(r"(SCR_\d+)", text, flags=re.I)
        if rrid_match:
            result["rrid"].append(f"RRID:{rrid_match.group(1).upper()}")

    return result


def parse_re3data_search(xml_content):
    try:
        root = (LXML_ETREE.fromstring(xml_content) if LXML_ETREE is not None
                else ET.fromstring(xml_content))
    except Exception:
        return []
    ids, seen = [], set()
    for element in root.iter():
        possible_values = [element.text or "", *element.attrib.values()]
        for value in possible_values:
            match = re.search(r"(r3d\d+)", str(value), flags=re.I)
            if match:
                r3d_id = match.group(1).lower()
                if r3d_id not in seen:
                    seen.add(r3d_id)
                    ids.append(r3d_id)
    return ids


def host(url):
    if not url:
        return ""
    parsed = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(
        str(url) if "://" in str(url) else f"https://{url}"
    )
    return (parsed.hostname or "").lower().removeprefix("www.")


def fairsharing_auth():
    if not credential_use_allowed():
        return None
    username = (os.getenv("FS_U") or "").strip()
    password = os.getenv("FS_PWD") or ""
    if username and password:
        return username, password
    return None


def fairsharing_headers():
    headers = {"Accept": "application/json, text/html;q=0.9, application/xhtml+xml;q=0.8"}
    if not credential_use_allowed():
        return headers
    token = (os.getenv("FAIRSHARING_API_TOKEN") or "").strip()
    api_key = (os.getenv("FAIRSHARING_API_KEY") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif api_key:
        headers["X-API-Key"] = api_key
    return headers


def scicrunch_api_key():
    if not credential_use_allowed():
        return ""
    return (os.getenv("SCICRUNCH_API_KEY") or "").strip()


def scicrunch_headers():
    headers = {"Accept": "application/json, text/html;q=0.9"}
    api_key = scicrunch_api_key()
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def scicrunch_name(response):
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return ""
    priority_keys = ("name", "resource_name", "resourceName", "Resource Name", "title", "label")

    def scan(value):
        if isinstance(value, dict):
            for key in priority_keys:
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            for nested in value.values():
                found = scan(nested)
                if found:
                    return found
        if isinstance(value, list):
            for nested in value:
                found = scan(nested)
                if found:
                    return found
        return ""

    return scan(payload)


def datacite_metadata(http, doi, verbose=False, repo_uid="", id_type=""):
    if not doi:
        return "", "", "", "no DOI"
    endpoint = f"https://api.datacite.org/dois/{quote(str(doi).strip(), safe='')}"
    response, error = http.get(
        endpoint, headers={"Accept": "application/vnd.api+json"},
        source="datacite", repo_uid=repo_uid, id_type=id_type,
    )
    if response is None:
        return "", "", "", error or "DataCite request failed"
    if not response.ok:
        return response.status_code, "", response.url, f"HTTP {response.status_code}"
    try:
        payload = response.json()
        attributes = payload.get("data", {}).get("attributes", {})
        titles = attributes.get("titles") or []
        title = ""
        for title_item in titles:
            if isinstance(title_item, dict) and title_item.get("title"):
                title = str(title_item["title"]).strip()
                break
        if not title:
            title = str(attributes.get("title") or "").strip()
        return response.status_code, title, response.url, ""
    except (ValueError, AttributeError, TypeError) as exc:
        debug(f"DataCite JSON parse failure: {exc}", verbose)
        return response.status_code, "", response.url, "invalid DataCite JSON"


# ─── per-source VERIFY (existing link) functions ────────────────────────────
def verify_rrid(http, card_name, id_value, url, verbose=False, repo_uid=""):
    raw = str(id_value or "").strip()
    rrid = re.sub(r"^RRID:", "", raw, flags=re.I)
    if not rrid or not RRID_RE.fullmatch(raw):
        return "", "", "", "", "broken", "invalid_rrid", "Invalid RRID format."

    json_url = f"https://scicrunch.org/resolver/{rrid}.json"
    response, error = http.get(json_url, headers=scicrunch_headers(),
                               source="rrid", repo_uid=repo_uid, id_type="rrid")

    if response is not None and response.ok:
        title = scicrunch_name(response)
        final_url = response.url
        if not title:
            html_response, html_error = http.get(
                url or f"https://scicrunch.org/resolver/{rrid}", headers={"Accept": "text/html"},
                source="rrid", repo_uid=repo_uid, id_type="rrid",
            )
            if html_response is not None and html_response.ok:
                title = html_title(html_response.text)
                final_url = html_response.url
            else:
                debug(f"SciCrunch HTML fallback failed: {html_error}", verbose)

        ratio = similarity(card_name, title)
        matched, basis = name_match(card_name, title, ratio)
        verdict = "verified" if matched else "name_mismatch"
        return (response.status_code, final_url, title, ratio, verdict, basis,
                "Direct SciCrunch resolver check.")

    fallback, fallback_error = http.get(
        url or f"https://scicrunch.org/resolver/{rrid}", headers={"Accept": "text/html"},
        source="rrid", repo_uid=repo_uid, id_type="rrid",
    )
    if fallback is None or not fallback.ok:
        status = (fallback.status_code if fallback is not None
                 else (response.status_code if response is not None else ""))
        return (status, "", "", "", "broken", "resolver_failed",
                f"SciCrunch JSON error={error or 'none'}; HTML error={fallback_error or 'none'}.")

    title = html_title(fallback.text)
    ratio = similarity(card_name, title)
    matched, basis = name_match(card_name, title, ratio)
    return (fallback.status_code, fallback.url, title, ratio,
            "verified" if matched else "name_mismatch", basis,
            "SciCrunch HTML resolver fallback.")


def fairsharing_record_title(response):
    content_type = response.headers.get("Content-Type", "").lower()
    if "json" in content_type:
        try:
            payload = response.json()
            if isinstance(payload, dict):
                for key in ("name", "title", "label"):
                    value = payload.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
                data = payload.get("data")
                if isinstance(data, dict):
                    attributes = data.get("attributes", {})
                    if isinstance(attributes, dict):
                        for key in ("name", "title", "label"):
                            value = attributes.get(key)
                            if isinstance(value, str) and value.strip():
                                return value.strip()
        except (ValueError, json.JSONDecodeError):
            pass
    return html_title(response.text)


def verify_fairsharing(http, card_name, id_value, url, verbose=False, repo_uid=""):
    doi = str(id_value or "").strip()
    target = str(url or f"https://fairsharing.org/{doi}").strip()
    if not doi and not target:
        return "", "", "", "", "broken", "missing_doi_and_url", "No DOI or URL."

    auth = fairsharing_auth()
    headers = fairsharing_headers()
    debug(f"FAIRsharing check DOI={doi!r}, target={target!r}, "
         f"FS credentials configured={bool(auth)}.", verbose)

    response, error = http.get(target, headers=headers, auth=auth,
                               source="fairsharing", repo_uid=repo_uid, id_type="fairsharing_doi")

    if response is not None and response.ok:
        title = fairsharing_record_title(response)
        source = "FAIRsharing authenticated/public record response"
        if not title:
            html_response, html_error = http.get(
                response.url, headers={"Accept": "text/html"}, auth=auth,
                source="fairsharing", repo_uid=repo_uid, id_type="fairsharing_doi",
            )
            if html_response is not None and html_response.ok:
                response = html_response
                title = html_title(html_response.text)
                source = "FAIRsharing authenticated/public HTML fallback"
            else:
                debug(f"FAIRsharing HTML title extraction failed: {html_error}", verbose)

        dc_status, dc_title, dc_url, dc_error = datacite_metadata(
            http, doi, verbose=verbose, repo_uid=repo_uid, id_type="fairsharing_doi")
        resolved_name = title or dc_title
        ratio = similarity(card_name, resolved_name)
        matched, basis = name_match(card_name, resolved_name, ratio)
        notes = (f"{source}; FAIRsharing HTTP={response.status_code}; "
                f"DataCite HTTP={dc_status or 'unavailable'}; "
                f"DataCite title available={bool(dc_title)}.")
        return (response.status_code, response.url, resolved_name, ratio,
                "verified" if matched else "name_mismatch", basis, notes)

    dc_status, dc_title, dc_url, dc_error = datacite_metadata(
        http, doi, verbose=verbose, repo_uid=repo_uid, id_type="fairsharing_doi")

    if dc_title:
        ratio = similarity(card_name, dc_title)
        matched, basis = name_match(card_name, dc_title, ratio)
        return (dc_status, dc_url, dc_title, ratio,
                "verified" if matched else "name_mismatch", f"datacite_fallback_{basis}",
                ("FAIRsharing page did not resolve, but public DataCite DOI metadata was "
                 f"available. FAIRsharing HTTP="
                 f"{response.status_code if response else 'network_error'}; DataCite HTTP={dc_status}."))

    status = response.status_code if response is not None else ""
    body_hint = ""
    if response is not None:
        body_hint = re.sub(r"\s+", " ", response.text[:200]).strip()
    return (status, response.url if response is not None else "", "", "", "broken",
            "fairsharing_and_datacite_unresolved",
            (f"FAIRsharing HTTP={status or 'network_error'}; request_error={error or 'none'}; "
             f"DataCite={dc_error or dc_status or 'unavailable'}; body_hint={body_hint!r}"))


def verify_re3data(http, card_name, homepage, id_value, url, verbose=False, repo_uid=""):
    if not id_value:
        if url:
            return ("", url, "", "", "unresolved_search_only", "search_only",
                    "Honest blank: re3data search URL exists but no r3d ID is recorded.")
        return "", "", "", "", "broken", "missing_id_and_url", "No re3data ID or URL."

    r3d_id = str(id_value).strip().lower()
    if not R3D_RE.fullmatch(r3d_id):
        return "", "", "", "", "broken", "invalid_r3d_id", "Invalid re3data ID format."

    endpoint = f"https://www.re3data.org/api/v1/repository/{r3d_id}"
    response, error = http.get(endpoint, headers={"Accept": "application/xml"},
                               source="re3data", repo_uid=repo_uid, id_type="re3data")

    if response is None or not response.ok:
        return (response.status_code if response is not None else "", "", "", "", "broken",
                "re3data_api_failed",
                f"re3data HTTP={getattr(response, 'status_code', '')}; error={error or 'none'}.")

    title, repository_url = parse_re3data_record(response.content)
    ratio = similarity(card_name, title)
    matched_name, name_basis = name_match(card_name, title, ratio)
    repository_host = host(repository_url)
    homepage_host = host(homepage)
    matched_host = bool(repository_host and homepage_host and repository_host == homepage_host)
    verdict = "verified" if matched_name or matched_host else "name_mismatch"
    basis = name_basis if matched_name else ("homepage_host" if matched_host else "both_disagree")
    return (response.status_code, repository_url, title, ratio, verdict, basis,
            f"repository_host={repository_host or 'missing'}; "
            f"card_homepage_host={homepage_host or 'missing'}; host_match={matched_host}.")


VERIFY_FUNCS = {"rrid": verify_rrid, "fairsharing_doi": verify_fairsharing, "re3data": verify_re3data}


# ─── re3data full-catalog cache ──────────────────────────────────────────
def _re3data_catalog_is_fresh(cache_path, max_age_days):
    if not os.path.exists(cache_path):
        return False
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if payload.get("schema") != RE3DATA_CATALOG_SCHEMA:
            return False
        fetched_at = payload.get("fetched_at")
        if not fetched_at:
            return False
        fetched_dt = dt.datetime.fromisoformat(fetched_at)
        age_days = (dt.datetime.now() - fetched_dt).total_seconds() / 86400
        return age_days <= max_age_days and bool(payload.get("records"))
    except Exception:
        return False


def load_re3data_catalog_cache(cache_path):
    """Load a full re3data catalog cache.

    v1 records remain readable for non-production diagnostics, but production
    considers them stale because they lack alternate names needed by v16.
    """
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        records = payload.get("records")
        if not isinstance(records, list):
            return None
        return records
    except Exception:
        return None


def load_re3data_seed_mappings(path):
    source = Path(path)
    if not source.is_file() or source.stat().st_size == 0:
        return {}
    result = {}
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"repo_uid", "r3d_id", "record_url", "evidence_basis"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(
                f"re3data confirmed-mapping file lacks columns: {sorted(required - set(reader.fieldnames or []))}"
            )
        for row in reader:
            uid = str(row.get("repo_uid") or "").strip()
            r3d_id = str(row.get("r3d_id") or "").strip().lower()
            if not uid or uid.startswith("#"):
                continue
            if not R3D_RE.fullmatch(r3d_id):
                raise ValueError(f"Invalid confirmed re3data identifier for {uid}: {r3d_id!r}")
            if uid in result and result[uid]["r3d_id"] != r3d_id:
                raise ValueError(f"Conflicting confirmed re3data mappings for {uid}")
            result[uid] = {
                "repo_uid": uid,
                "r3d_id": r3d_id,
                "record_url": str(row.get("record_url") or f"https://www.re3data.org/repository/{r3d_id}").strip(),
                "expected_name": str(row.get("expected_name") or "").strip(),
                "evidence_basis": str(row.get("evidence_basis") or "curated_exact_mapping").strip(),
                "reviewed_at": str(row.get("reviewed_at") or "").strip(),
            }
    return result


def build_re3data_catalog(http, verbose=False, telemetry=None, max_records=None):
    """
    Download re3data's FULL repository catalog once: discover the ID list,
    then fetch the full record (including repositoryIdentifier) for every
    ID. This is the one-time cost that replaces per-card search
    truncation — confirmed necessary because re3data's search API does
    not rank by relevance (a correct record can sit at position 1000+ of
    ~3500 regardless of query wording; verified directly against the
    live API, 2026-09).

    Returns a list of record dicts:
      {"id": "r3d...", "name": ..., "aliases": [...], "homepage": ...,
       "fairsharing_doi": [...], "rrid": [...]}
    """
    info("Downloading full re3data catalog (one-time; ~3500 individual "
        "record fetches at the configured rate limit — this will take a while).")

    response, error = http.get(
        "https://www.re3data.org/api/v1/repositories", params={"query": "a"},
        headers={"Accept": "application/xml"}, source="re3data_catalog",
    )
    if response is None or not response.ok:
        info(f"re3data catalog ID discovery failed: HTTP="
            f"{getattr(response, 'status_code', '')}; error={error}. Catalog build aborted.")
        return []

    all_ids = parse_re3data_search(response.content)
    debug(f"re3data catalog discovery returned {len(all_ids)} candidate IDs.", verbose)
    if max_records:
        all_ids = all_ids[:max_records]

    records = []
    total = len(all_ids)
    for index, r3d_id in enumerate(all_ids, start=1):
        rec_response, rec_error = http.get(
            f"https://www.re3data.org/api/v1/repository/{r3d_id}",
            headers={"Accept": "application/xml"}, source="re3data_catalog",
        )
        if rec_response is None or not rec_response.ok:
            if telemetry is not None:
                telemetry.record_error("re3data_catalog", "record_fetch_failed",
                                       f"{r3d_id}: HTTP="
                                       f"{getattr(rec_response, 'status_code', '')}")
            continue

        details = parse_re3data_record_details(rec_response.content)
        records.append({
            "id": r3d_id,
            "name": details["name"],
            "aliases": details["aliases"],
            "homepage": details["homepage"],
            "fairsharing_doi": details["fairsharing_doi"],
            "rrid": details["rrid"],
        })

        if index % 200 == 0 or index == total:
            info(f"re3data catalog build: {index}/{total} records fetched.")

    info(f"re3data catalog build complete: {len(records)}/{total} records cached.")
    return records


def save_re3data_catalog_cache(cache_path, records):
    payload = {
        "schema": RE3DATA_CATALOG_SCHEMA,
        "fetched_at": dt.datetime.now().isoformat(),
        "record_count": len(records),
        "records": records,
    }
    _atomic_write_json(cache_path, payload)
    info(f"re3data catalog cache saved: {cache_path} ({len(records)} records).")


def get_re3data_catalog(
    http,
    cache_path,
    max_age_days=RE3DATA_CATALOG_MAX_AGE_DAYS,
    verbose=False,
    telemetry=None,
    force_refresh=False,
    read_only=DEFAULT_RE3DATA_CACHE_READ_ONLY,
):
    """Return the full re3data catalog under an explicit cache-write policy.

    In dev/staging the shared cache is read-only by default. A stale shared
    snapshot remains usable (with a warning) so exploratory runs do not mutate
    shared state. Only prod, or an explicit --allow-cache-write run, refreshes it.
    """
    fresh = _re3data_catalog_is_fresh(cache_path, max_age_days)
    cached = load_re3data_catalog_cache(cache_path) if os.path.exists(cache_path) else None

    if not force_refresh and fresh and cached:
        debug(
            f"Using cached re3data catalog ({len(cached)} records) from {cache_path}.",
            verbose,
        )
        return cached

    if read_only:
        if force_refresh:
            raise PermissionError(
                "--refresh-catalog requested while the re3data cache is read-only. "
                "Run in prod or add --allow-cache-write deliberately."
            )
        if cached:
            info(
                f"Using stale/unvalidated shared re3data cache read-only: {cache_path} "
                f"({len(cached)} records)."
            )
            return cached
        info(
            f"Shared re3data cache is read-only and unavailable at {cache_path}; "
            "re3data candidate discovery is disabled for this run."
        )
        return []

    if cached and not force_refresh:
        debug("re3data catalog cache is stale; rebuilding because cache writes are allowed.", verbose)
    records = build_re3data_catalog(http, verbose=verbose, telemetry=telemetry)
    if records:
        save_re3data_catalog_cache(cache_path, records)
    return records


# ─── per-source SEARCH (candidate discovery for missing/bad links) ─────────
def _best_re3data_name_match(card_names, registry_names):
    best = None
    priority = {
        "exact_normalized": 7,
        "exact_normalized_alias": 7,
        "resolved_matches_card_acronym": 6,
        "card_matches_resolved_acronym": 6,
        "name_containment": 5,
        "meaningful_token_overlap": 4,
        "shared_acronym": 3,
        "fuzzy_ratio": 2,
        "below_threshold": 0,
    }
    for card_name in card_names:
        for index, registry_name in enumerate(registry_names):
            ratio = similarity(card_name, registry_name)
            matched, basis = name_match(card_name, registry_name, ratio)
            if matched and index > 0 and basis == "exact_normalized":
                basis = "exact_normalized_alias"
            score = (1 if matched else 0, priority.get(basis, 1), float(ratio or 0))
            if best is None or score > best[0]:
                best = (score, card_name, registry_name, ratio, matched, basis)
    if best is None:
        return "", "", "", False, "missing_name"
    _, card_name, registry_name, ratio, matched, basis = best
    return card_name, registry_name, ratio, matched, basis


def search_re3data_candidates_from_catalog(
    catalog,
    card_name,
    homepage,
    verbose=False,
    repo_uid="",
    card_names=None,
    card_identifiers=None,
):
    """Score every cached re3data record using card names, aliases and host.

    v16 retains primary and alternate re3data names. The best pair across the
    card's canonical name, full name, abbreviation, aliases, and parenthetical
    acronyms is used. Exact alternate-name matches are independent evidence.
    """
    names = [str(value).strip() for value in (card_names or [card_name]) if str(value).strip()]
    if card_name and card_name not in names:
        names.insert(0, card_name)
    if not names or not catalog:
        return []

    candidates = []
    card_identifiers = card_identifiers or {}
    known_rrids = {
        str(value).strip().upper().removeprefix("RRID:")
        for value in (card_identifiers.get("rrid") or [])
        if str(value).strip()
    }
    known_dois = {
        str(value).strip().casefold()
        for value in (card_identifiers.get("fairsharing_doi") or [])
        if str(value).strip()
    }

    for record in catalog:
        r3d_id = str(record.get("id") or "").lower()
        if not r3d_id or r3d_id in RE3DATA_DENYLIST:
            continue
        registry_names = [
            value for value in [record.get("name", ""), *(record.get("aliases") or [])]
            if str(value).strip()
        ]
        matched_card_name, matched_registry_name, ratio, matched_name, basis = (
            _best_re3data_name_match(names, registry_names)
        )
        record_host = host(record.get("homepage", ""))
        card_host = host(homepage)
        matched_host = bool(record_host and card_host and record_host == card_host)
        matched_host_is_shared_institutional = matched_host and record_host in SHARED_INSTITUTIONAL_HOSTS
        record_rrids = {
            str(value).strip().upper().removeprefix("RRID:")
            for value in (record.get("rrid") or [])
            if str(value).strip()
        }
        record_dois = {
            str(value).strip().casefold()
            for value in (record.get("fairsharing_doi") or [])
            if str(value).strip()
        }
        rrid_hit = bool(known_rrids & record_rrids)
        doi_hit = bool(known_dois & record_dois)
        if not matched_name and not matched_host and not rrid_hit and not doi_hit:
            continue

        confidence = float(ratio) if ratio != "" else 0.0
        if rrid_hit and doi_hit:
            basis = "cross_registry_identifiers"
        elif rrid_hit:
            basis = "cross_registry_identifier_rrid"
        elif doi_hit:
            basis = "cross_registry_identifier_fairsharing_doi"
        elif not matched_name:
            basis = "shared_institutional_host" if matched_host_is_shared_institutional else "homepage_host"
        if (
            confidence >= STRUCTURAL_BOOST_MIN_RATIO
            and basis in {
                "exact_normalized",
                "exact_normalized_alias",
                "name_containment",
                "resolved_matches_card_acronym",
                "card_matches_resolved_acronym",
                "shared_acronym",
            }
        ):
            confidence = max(confidence, 0.90)
        if basis in {"exact_normalized", "exact_normalized_alias"}:
            confidence = 1.0
        if rrid_hit or doi_hit:
            confidence = max(confidence, 0.95)
        if matched_host and not matched_host_is_shared_institutional:
            confidence = min(1.0, confidence + 0.12)

        candidates.append({
            "id_type": "re3data",
            "id_value": r3d_id,
            "url": f"https://www.re3data.org/repository/{r3d_id}",
            "http_status": 200,
            "resolved_url": record.get("homepage", ""),
            "resolved_name": matched_registry_name or record.get("name", ""),
            "name_match_ratio": ratio,
            "match_basis": basis,
            "confidence": round(confidence, 4),
            "source": "re3data_local_catalog_scan_v2",
            "_fairsharing_doi": record.get("fairsharing_doi", []),
            "_rrid": record.get("rrid", []),
            "_homepage_host": record_host,
            "_matched_card_name": matched_card_name,
            "_record_primary_name": record.get("name", ""),
            "_record_aliases": record.get("aliases", []),
            "_cross_identifier_rrid": sorted(known_rrids & record_rrids),
            "_cross_identifier_fairsharing_doi": sorted(known_dois & record_dois),
        })
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate["confidence"],
            candidate["name_match_ratio"] if candidate["name_match_ratio"] != "" else 0,
        ),
        reverse=True,
    )


def search_re3data_candidates(
    http,
    card_name,
    homepage,
    verbose=False,
    repo_uid="",
    card_names=None,
    card_identifiers=None,
):
    found = search_re3data_candidates_from_catalog(
        _ACTIVE_RE3DATA_CATALOG,
        card_name,
        homepage,
        verbose=verbose,
        repo_uid=repo_uid,
        card_names=card_names,
        card_identifiers=card_identifiers,
    )
    seed = _ACTIVE_RE3DATA_SEEDS.get(repo_uid)
    if seed:
        status, resolved_url, resolved_name, ratio, verdict, basis, notes = verify_re3data(
            http,
            card_name,
            homepage,
            seed["r3d_id"],
            seed["record_url"],
            verbose=verbose,
            repo_uid=repo_uid,
        )
        seed_candidate = {
            "id_type": "re3data",
            "id_value": seed["r3d_id"],
            "url": seed["record_url"],
            "http_status": status,
            "resolved_url": resolved_url,
            "resolved_name": resolved_name or seed.get("expected_name", ""),
            "name_match_ratio": ratio,
            "match_basis": (
                "curated_exact_mapping+live_verified"
                if verdict == "verified"
                else f"curated_exact_mapping+{basis}"
            ),
            "confidence": 1.0 if verdict == "verified" else 0.0,
            "source": "curated_re3data_mapping",
            "_fairsharing_doi": [],
            "_rrid": [],
            "_homepage_host": host(resolved_url),
            "_seed_evidence_basis": seed.get("evidence_basis", ""),
            "_seed_reviewed_at": seed.get("reviewed_at", ""),
            "_seed_verdict": verdict,
            "_seed_notes": notes,
        }
        by_id = {candidate["id_value"]: candidate for candidate in found}
        if verdict == "verified" or seed["r3d_id"] not in by_id:
            by_id[seed["r3d_id"]] = seed_candidate
        found = sorted(
            by_id.values(),
            key=lambda candidate: (
                candidate["confidence"],
                candidate["name_match_ratio"] if candidate["name_match_ratio"] != "" else 0,
            ),
            reverse=True,
        )
    return found

def search_fairsharing_candidates(http, card_name, homepage, verbose=False, repo_uid=""):
    """
    Search FAIRsharing's real search endpoint for a matching record.

    POST https://api.fairsharing.org/users/sign_in -> JWT, then POST
    https://api.fairsharing.org/search/fairsharing_records with
    Authorization: Bearer <jwt>, body {"q": <query>}.

    Requires FS_U / FS_PWD. Without them, returns [] with a debug skip
    logged — FAIRsharing has no keyless search API.
    """
    if not card_name:
        return []

    auth = fairsharing_auth()
    if not auth:
        debug("FAIRsharing candidate search skipped: no FS_U/FS_PWD configured "
             "(FAIRsharing search requires JWT auth, not keyless).", verbose)
        return []

    username, password = auth
    signin_response, signin_error = http.post_json(
        "https://api.fairsharing.org/users/sign_in",
        json_body={"user": {"login": username, "password": password}},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        source="fairsharing", repo_uid=repo_uid, id_type="fairsharing_doi",
    )
    if signin_response is None or not signin_response.ok:
        debug(f"FAIRsharing sign-in failed: HTTP="
             f"{getattr(signin_response, 'status_code', '')}; error={signin_error}", verbose)
        return []

    try:
        jwt = signin_response.json().get("jwt")
    except (ValueError, json.JSONDecodeError):
        jwt = None
    if not jwt:
        debug("FAIRsharing sign-in succeeded but no JWT found in response.", verbose)
        return []

    response, error = http.post_json(
        "https://api.fairsharing.org/search/fairsharing_records",
        json_body={"q": card_name},
        headers={"Content-Type": "application/json", "Accept": "application/json",
                "Authorization": f"Bearer {jwt}"},
        source="fairsharing", repo_uid=repo_uid, id_type="fairsharing_doi",
    )
    if response is None or not response.ok:
        debug(f"FAIRsharing candidate search failed for {card_name!r}: "
             f"HTTP={getattr(response, 'status_code', '')}; error={error}", verbose)
        return []

    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        debug(f"FAIRsharing search JSON parse failure: {exc}", verbose)
        return []

    hits = payload.get("data") if isinstance(payload, dict) else []
    if not isinstance(hits, list):
        hits = []

    candidates = []
    for hit in hits[:MAX_SEARCH_CANDIDATES]:
        if not isinstance(hit, dict):
            continue
        attributes = hit.get("attributes", hit)
        doi = str(attributes.get("doi") or "").strip()
        title = str(attributes.get("name") or "").strip()
        if not doi:
            continue

        resolved_name = title
        if not resolved_name:
            _, dc_title, _, _ = datacite_metadata(http, doi, verbose=verbose,
                                                   repo_uid=repo_uid, id_type="fairsharing_doi")
            resolved_name = dc_title

        ratio = similarity(card_name, resolved_name)
        matched, basis = name_match(card_name, resolved_name, ratio)
        confidence = float(ratio) if ratio != "" else 0.0
        if (matched and confidence >= STRUCTURAL_BOOST_MIN_RATIO
                and basis in {"exact_normalized", "name_containment",
                              "resolved_matches_card_acronym",
                              "card_matches_resolved_acronym", "shared_acronym"}):
            confidence = max(confidence, 0.90)

        candidates.append({
            "id_type": "fairsharing_doi", "id_value": doi,
            "url": f"https://fairsharing.org/{doi}",
            "http_status": response.status_code, "resolved_url": f"https://fairsharing.org/{doi}",
            "resolved_name": resolved_name, "name_match_ratio": ratio, "match_basis": basis,
            "confidence": round(confidence, 4), "source": "fairsharing_authenticated_search",
        })

    return sorted(candidates, key=lambda c: (c["confidence"],
                 c["name_match_ratio"] if c["name_match_ratio"] != "" else 0), reverse=True)


def search_rrid_candidates(http, card_name, homepage, verbose=False, repo_uid=""):
    """
    Search SciCrunch's real RIN Elasticsearch index for a matching RRID.

    GET https://api.scicrunch.io/elastic/v1/RIN_Tool_pr/_search?q=... with
    an 'apikey' header. Response is raw Elasticsearch JSON:
    hits.hits[]._source.item.name / .rrid.curie.

    Searches only the RIN_Tool_pr index (general tool/resource registry).
    Requires SCICRUNCH_API_KEY; returns [] with a debug skip if unset.
    """
    if not card_name:
        return []

    api_key = scicrunch_api_key()
    if not api_key:
        debug("SciCrunch candidate search skipped: no SCICRUNCH_API_KEY configured.", verbose)
        return []

    response, error = http.get(
        "https://api.scicrunch.io/elastic/v1/RIN_Tool_pr/_search",
        params={"q": card_name}, headers={"apikey": api_key},
        source="rrid", repo_uid=repo_uid, id_type="rrid",
    )
    if response is None or not response.ok:
        debug(f"SciCrunch candidate search failed for {card_name!r}: "
             f"HTTP={getattr(response, 'status_code', '')}; error={error}", verbose)
        return []

    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        debug(f"SciCrunch search JSON parse failure: {exc}", verbose)
        return []

    hits = (payload.get("hits", {}) or {}).get("hits", []) if isinstance(payload, dict) else []

    candidates = []
    for hit in hits[:MAX_SEARCH_CANDIDATES]:
        if not isinstance(hit, dict):
            continue
        source_doc = hit.get("_source", {}) or {}
        item = source_doc.get("item", {}) or {}
        rrid_block = source_doc.get("rrid", {}) or {}

        rrid_curie = str(rrid_block.get("curie") or "").strip()
        rrid_match = re.search(r"(SCR_\d+)", rrid_curie, flags=re.I)
        if not rrid_match:
            identifier = str(item.get("identifier") or "").strip()
            rrid_match = re.search(r"(SCR_\d+)", identifier, flags=re.I)
        if not rrid_match:
            continue
        rrid = rrid_match.group(1).upper()

        title = str(item.get("name") or "").strip()
        if not title:
            continue

        ratio = similarity(card_name, title)
        matched, basis = name_match(card_name, title, ratio)
        confidence = float(ratio) if ratio != "" else 0.0
        if (matched and confidence >= STRUCTURAL_BOOST_MIN_RATIO
                and basis in {"exact_normalized", "name_containment",
                              "resolved_matches_card_acronym",
                              "card_matches_resolved_acronym", "shared_acronym"}):
            confidence = max(confidence, 0.90)

        candidates.append({
            "id_type": "rrid", "id_value": f"RRID:{rrid}",
            "url": f"https://scicrunch.org/resolver/RRID:{rrid}",
            "http_status": response.status_code,
            "resolved_url": f"https://scicrunch.org/resolver/RRID:{rrid}",
            "resolved_name": title, "name_match_ratio": ratio, "match_basis": basis,
            "confidence": round(confidence, 4), "source": "scicrunch_rin_elasticsearch_search",
        })

    return sorted(candidates, key=lambda c: (c["confidence"],
                 c["name_match_ratio"] if c["name_match_ratio"] != "" else 0), reverse=True)


# Module-level holder for the current run's loaded re3data catalog. Set
# once in run_scan() before any search happens. This lets the re3data
# entry in SEARCH_FUNCS share the same fixed call signature
# (http, card_name, homepage, verbose, repo_uid) as the other two sources
# without needing to thread an extra catalog argument through every caller.
_ACTIVE_RE3DATA_CATALOG = []
_ACTIVE_RE3DATA_SEEDS = {}

SEARCH_FUNCS = {
    "rrid": search_rrid_candidates,
    "fairsharing_doi": search_fairsharing_candidates,
    "re3data": search_re3data_candidates,
}


# ─── per-card concurrent search dispatch ────────────────────────────────────
def search_missing_for_card(http, repository_name, homepage, missing_id_types,
                            verbose=False, repo_uid="", telemetry=None,
                            max_workers=3, card_names=None,
                            card_identifiers=None):
    """
    Search every id_type in missing_id_types CONCURRENTLY for one card.
    Each id_type is an independent slot on the card. All requested sources
    are always searched regardless of what any other source returns;
    nothing is skipped early.

    Returns: {id_type: [candidates sorted best-first]} — a source that
    found nothing or errored maps to an empty list, not a missing key.
    """
    results = {id_type: [] for id_type in missing_id_types}
    if not missing_id_types:
        return results

    def run_one(id_type):
        search_fn = SEARCH_FUNCS[id_type]
        try:
            kwargs = {"verbose": verbose, "repo_uid": repo_uid}
            if id_type == "re3data":
                kwargs["card_names"] = card_names
                kwargs["card_identifiers"] = card_identifiers
            return id_type, search_fn(http, repository_name, homepage, **kwargs)
        except Exception as exc:
            if telemetry is not None:
                telemetry.record_error(
                    ID_TYPE_TO_SOURCE.get(id_type, id_type), "unhandled_exception",
                    f"{exc}\n{traceback.format_exc()}", repo_uid, id_type,
                )
            return id_type, []

    with ThreadPoolExecutor(max_workers=min(max_workers, len(missing_id_types))) as pool:
        futures = [pool.submit(run_one, id_type) for id_type in missing_id_types]
        for future in as_completed(futures):
            id_type, found = future.result()
            results[id_type] = found

    return results


def _existing_link_values(existing_links, id_type):
    """Return the set of id_values already on the card for a given id_type,
    normalized (RRIDs upper-cased with an RRID: prefix)."""
    values = set()
    for link in existing_links:
        link_type = str(link.get("id_type", "")).strip().lower()
        if link_type != id_type:
            continue
        raw = str(link.get("id_value") or "").strip()
        if not raw:
            continue
        if id_type == "rrid":
            values.add(raw if raw.upper().startswith("RRID:") else f"RRID:{raw.upper()}")
        else:
            values.add(raw)
    return values


def apply_cross_source_corroboration(results_by_type, existing_links, verbose=False, repo_uid=""):
    """
    BIDIRECTIONAL cross-source corroboration (v10). For each source's top
    candidate, check whether ANY other source — an existing confirmed
    link on the card, OR another candidate found in this same run —
    independently corroborates it via a shared identifier:

      - a re3data candidate is boosted if its own catalog-listed RRID or
        FAIRsharing DOI matches an existing/candidate RRID or FAIRsharing
        DOI for this card
      - an RRID candidate is boosted if a re3data candidate's (or
        existing re3data link's) listed RRID matches it
      - a FAIRsharing candidate is boosted if a re3data candidate's (or
        existing re3data link's) listed DOI matches it

    CONFIRMED gap this fixes (2026-09, LONI/R229): the prior one-way
    version only ever boosted re3data candidates. LONI's re3data record
    (applied to the card, confidence 0.95) independently lists
    RRID:SCR_007283 — but LONI's own RRID search that same run found
    exactly that RRID at only raw confidence 0.55 and stayed stuck below
    threshold, because nothing fed the re3data record's identifier list
    back into scoring the RRID candidate itself.

    All directions use the same confidence bump (0.95) and the same
    match_basis suffix convention (+corroborated_<kind>), so
    has_independent_evidence() needs no changes — it already treats any
    "+corroborated_..." suffix as independent evidence regardless of
    which source carries it.

    Mutates candidate dicts in place.
    """
    re3data_candidates = results_by_type.get("re3data") or []
    rrid_candidates = results_by_type.get("rrid") or []
    fairsharing_candidates = results_by_type.get("fairsharing_doi") or []

    if not (re3data_candidates or rrid_candidates or fairsharing_candidates):
        return

    existing_rrids = _existing_link_values(existing_links, "rrid")
    existing_dois = _existing_link_values(existing_links, "fairsharing_doi")

    candidate_rrids = {c["id_value"] for c in rrid_candidates}
    candidate_dois = {c["id_value"] for c in fairsharing_candidates}

    known_rrids = existing_rrids | candidate_rrids
    known_dois = existing_dois | candidate_dois

    # Direction 1: boost re3data candidates whose OWN record cites an
    # already-known (existing or same-run) RRID/DOI.
    any_corroboration = False
    for candidate in re3data_candidates:
        record_dois = set(candidate.get("_fairsharing_doi", []))
        record_rrids = set(candidate.get("_rrid", []))
        doi_hit = bool(known_dois & record_dois)
        rrid_hit = bool(known_rrids & record_rrids)
        if doi_hit or rrid_hit:
            hit_kind = "fairsharing_doi" if doi_hit else "rrid"
            any_corroboration = True
            debug(f"re3data candidate {candidate['id_value']} corroborated via "
                 f"shared {hit_kind} for {repo_uid}.", verbose)
            candidate["confidence"] = max(candidate["confidence"], 0.95)
            if "+corroborated_" not in candidate["match_basis"]:
                candidate["match_basis"] = f"{candidate['match_basis']}+corroborated_{hit_kind}"

    # Gather the RRID/DOI values any re3data candidate (post-boost) OR an
    # existing re3data link independently lists, for directions 2 and 3.
    re3data_listed_rrids = set()
    re3data_listed_dois = set()
    for candidate in re3data_candidates:
        re3data_listed_rrids |= set(candidate.get("_rrid", []))
        re3data_listed_dois |= set(candidate.get("_fairsharing_doi", []))
    # Existing re3data links on the card don't carry cached identifier
    # lists in this script (that data lives in the catalog cache, not on
    # the card), so only same-run re3data candidates contribute here.

    # Direction 2: boost RRID candidates that a re3data candidate's own
    # record independently confirms.
    for candidate in rrid_candidates:
        if candidate["id_value"] in re3data_listed_rrids:
            any_corroboration = True
            debug(f"RRID candidate {candidate['id_value']} corroborated via "
                 f"re3data record for {repo_uid}.", verbose)
            candidate["confidence"] = max(candidate["confidence"], 0.95)
            if "+corroborated_" not in candidate["match_basis"]:
                candidate["match_basis"] = f"{candidate['match_basis']}+corroborated_re3data"

    # Direction 3: boost FAIRsharing candidates that a re3data candidate's
    # own record independently confirms.
    for candidate in fairsharing_candidates:
        if candidate["id_value"] in re3data_listed_dois:
            any_corroboration = True
            debug(f"FAIRsharing candidate {candidate['id_value']} corroborated via "
                 f"re3data record for {repo_uid}.", verbose)
            candidate["confidence"] = max(candidate["confidence"], 0.95)
            if "+corroborated_" not in candidate["match_basis"]:
                candidate["match_basis"] = f"{candidate['match_basis']}+corroborated_re3data"

    if not any_corroboration:
        debug(f"Corroboration checked {len(re3data_candidates)} re3data / "
             f"{len(rrid_candidates)} rrid / {len(fairsharing_candidates)} fairsharing "
             f"candidates for {repo_uid} — no cross-source match found.", verbose)

    for candidates in (re3data_candidates, rrid_candidates, fairsharing_candidates):
        candidates.sort(key=lambda c: (c["confidence"],
                        c["name_match_ratio"] if c["name_match_ratio"] != "" else 0),
                        reverse=True)


def has_independent_evidence(candidate):
    """
    Direct-apply policy: confidence alone is not sufficient to write a
    card automatically. The candidate must ALSO carry at least one
    independently-verifiable signal, distinct from raw string similarity:
      - exact normalized name match, or an acronym match in either
        direction (INDEPENDENT_EVIDENCE_BASES)
      - a genuine homepage/domain match against the card's own homepage
        (NOT a shared institutional domain — see SHARED_INSTITUTIONAL_HOSTS)
      - cross-source corroboration (a shared FAIRsharing DOI or RRID
        found independently by another source this run, or already
        confirmed on the card) — now bidirectional as of v10, so this
        applies equally whether the candidate IS a re3data record or
        WAS corroborated BY a re3data record.

    A bare shared_acronym or fuzzy_ratio match, even boosted to a high
    confidence number, is NOT independent evidence on its own. A homepage
    match against a SHARED_INSTITUTIONAL_HOST is likewise NOT independent
    evidence — confirmed necessary 2026-09 after NDARO and Protein Family
    Models both matched "GenBank" purely on shared NCBI domain.
    """
    basis = str(candidate.get("match_basis", ""))
    base_basis = basis.split("+corroborated_")[0].split("+live_verified")[0]

    if base_basis in {
        "cross_registry_identifier_rrid",
        "cross_registry_identifier_fairsharing_doi",
        "cross_registry_identifiers",
    }:
        return True, "cross_registry_identifier_match"
    if base_basis in INDEPENDENT_EVIDENCE_BASES:
        return True, "exact_or_acronym_name_match"
    if base_basis == "homepage_host":
        return True, "homepage_domain_match"
    if base_basis == "shared_institutional_host":
        return False, "shared_institutional_host_not_independent"
    if "+corroborated_fairsharing_doi" in basis:
        return True, "corroborated_fairsharing_doi"
    if "+corroborated_rrid" in basis:
        return True, "corroborated_rrid"
    if "+corroborated_re3data" in basis:
        return True, "corroborated_re3data"
    return False, "none"


def evaluate_direct_apply(candidate, threshold):
    """
    Combined eligibility check for a candidate. Returns:
      (eligible_for_review, eligible_for_direct_apply, evidence_kind)

    eligible_for_review: confidence clears the source's discovery
      threshold — worth showing to a human.

    eligible_for_direct_apply: confidence >= DIRECT_APPLY_CONFIDENCE
      (0.95) AND has_independent_evidence() is True. Only these may
      become write-proposals without further review.
    """
    confidence = candidate["confidence"]
    eligible_for_review = confidence >= threshold

    has_evidence, evidence_kind = has_independent_evidence(candidate)
    eligible_for_direct_apply = (
        confidence >= DIRECT_APPLY_CONFIDENCE and has_evidence
    )
    return eligible_for_review, eligible_for_direct_apply, evidence_kind


# ─── card IO helpers ─────────────────────────────────────────────────────────
def load_card(path, verbose=False):
    try:
        with path.open("r", encoding="utf-8") as handle:
            card = yaml.safe_load(handle)
        if isinstance(card, dict):
            return card
        print(f"Warning: {path.name} is not a YAML mapping.", file=sys.stderr)
        return {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"Warning: failed to read {path}: {exc}", file=sys.stderr)
        debug(f"YAML load failure: {exc!r}", verbose)
        return {}


def get_name(card):
    identity = card.get("identity")
    identity = identity if isinstance(identity, dict) else {}
    return str(identity.get("name", "")).strip()


def get_names(card):
    identity = card.get("identity")
    identity = identity if isinstance(identity, dict) else {}
    values = []
    for key in ("name", "full_name", "abbreviation"):
        value = str(identity.get(key) or "").strip()
        if value:
            values.append(value)
    aliases = identity.get("aliases") if isinstance(identity.get("aliases"), list) else []
    values.extend(str(value).strip() for value in aliases if str(value).strip())
    for value in list(values):
        values.extend(match.group(1).strip() for match in ACRONYM_RE.finditer(value) if match.group(1).strip())
    return list(dict.fromkeys(values))


def get_homepage(card):
    identity = card.get("identity")
    identity = identity if isinstance(identity, dict) else {}
    homepage = identity.get("homepage", "")
    if isinstance(homepage, list):
        return str(homepage[0]).strip() if homepage else ""
    return str(homepage).strip() if homepage else ""


def get_relevant_links(card, active_id_types):
    links = card.get("links")
    if not isinstance(links, list):
        return []
    return [link for link in links if isinstance(link, dict)
            and str(link.get("id_type", "")).strip().lower() in active_id_types]


def get_card_registry_identifiers(card):
    """Return confirmed RRID and FAIRsharing identifiers already on a card.

    These identifiers are independent registry evidence that can recover a
    re3data record even when names or repository URLs have changed. URL-only
    placeholders are excluded.
    """
    result = {"rrid": set(), "fairsharing_doi": set()}
    links = card.get("links") if isinstance(card.get("links"), list) else []
    for link in links:
        if not isinstance(link, dict) or not link_has_confirmed_identifier(link):
            continue
        id_type = str(link.get("id_type") or "").strip().lower()
        value = str(link.get("id_value") or "").strip()
        if id_type == "rrid":
            result["rrid"].add(
                value if value.upper().startswith("RRID:") else f"RRID:{value.upper()}"
            )
        elif id_type == "fairsharing_doi":
            result["fairsharing_doi"].add(value)
    return result


def link_has_confirmed_identifier(link):
    """Return True only when a link carries a usable registry identifier.

    URL-only re3data search placeholders are deliberately not treated as
    satisfying ``--only-missing``.  They remain visible in the card, but the
    verifier must continue candidate discovery so a confirmed r3d identifier
    can replace the placeholder rather than being skipped forever.
    """
    if not isinstance(link, dict):
        return False
    id_type = str(link.get("id_type") or "").strip().lower()
    id_value = str(link.get("id_value") or "").strip()
    if not id_value:
        return False
    if id_type == "re3data":
        return bool(R3D_RE.fullmatch(id_value))
    if id_type == "rrid":
        return bool(RRID_RE.fullmatch(id_value))
    if id_type == "fairsharing_doi":
        return bool(re.match(r"^10\.\d{4,9}/fairsharing\.", id_value, flags=re.I))
    return True


def report_row(repo_uid, card_name, id_type, id_value, url, status, resolved_url,
               resolved_name, ratio, verdict, basis, notes, candidate=None):
    return {
        "repo_uid": repo_uid, "card_name": card_name, "id_type": id_type,
        "id_value": "" if id_value is None else str(id_value),
        "url": "" if url is None else str(url),
        "http_status": "" if status is None else str(status),
        "resolved_url": resolved_url or "", "resolved_name": resolved_name or "",
        "name_match_ratio": "" if ratio == "" else str(ratio),
        "verdict": verdict, "match_basis": basis,
        "candidate_id": candidate["id_value"] if candidate else "",
        "candidate_url": candidate["url"] if candidate else "",
        "candidate_name": candidate["resolved_name"] if candidate else "",
        "candidate_ratio": (str(candidate["name_match_ratio"])
                            if candidate and candidate["name_match_ratio"] != "" else ""),
        "candidate_confidence": str(candidate["confidence"]) if candidate else "",
        "candidate_source": candidate["source"] if candidate else "",
        "candidate_eligible": (str(candidate.get("eligible", "")) if candidate else ""),
        "candidate_direct_apply_eligible": (str(candidate.get("direct_apply_eligible", ""))
                                            if candidate else ""),
        "candidate_evidence_kind": (str(candidate.get("evidence_kind", "")) if candidate else ""),
        "candidate_threshold_used": (str(candidate.get("threshold_used", ""))
                                     if candidate else ""),
        "notes": notes or "",
    }


def build_proposal(path, repo_uid, card_name, id_type, action, existing_link, candidate):
    return {
        "repo_uid": repo_uid, "card_file": str(path), "card_name": card_name,
        # Bind direct apply to the exact card bytes that were evaluated. The
        # apply phase repeats this check after acquiring cards/.lock.
        "expected_card_sha256": _sha256_file(path),
        "id_type": id_type, "action": action,
        "reason": (f"High-confidence {ID_TYPE_TO_SOURCE.get(id_type, id_type)} candidate "
                  "identified via public search and validated the same way an existing "
                  "link would be verified. Direct-apply requires confidence "
                  f">= {DIRECT_APPLY_CONFIDENCE} plus independent evidence "
                  f"({candidate.get('evidence_kind', 'unknown')})."),
        "existing_link": existing_link,
        "proposed_link": {"id_type": candidate["id_type"], "id_value": candidate["id_value"],
                          "url": candidate["url"], "tier": "registry"},
        "validation": {"http_status": candidate["http_status"],
                       "resolved_name": candidate["resolved_name"],
                       "resolved_url": candidate["resolved_url"],
                       "name_match_ratio": candidate["name_match_ratio"],
                       "match_basis": candidate["match_basis"],
                       "confidence": candidate["confidence"], "source": candidate["source"],
                       "evidence_kind": candidate.get("evidence_kind", "")},
    }


def review_candidate_key(repo_uid, id_type, candidate_id):
    return (
        str(repo_uid or "").strip(),
        str(id_type or "").strip().lower(),
        str(candidate_id or "").strip().lower(),
    )


def load_rejected_candidates(path):
    """Load reviewer-rejected candidates so they do not re-enter the queue."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return set()
    try:
        import pandas as pd
        frame = pd.read_csv(path, dtype=str).fillna("")
    except Exception as exc:
        info(f"Could not read verify adjudications at {path}: {exc}; no rejections suppressed.")
        return set()
    required = {"repo_uid", "id_type", "candidate_id", "decision"}
    if not required.issubset(frame.columns):
        info(
            f"Verify adjudications at {path} lack {sorted(required - set(frame.columns))}; "
            "no rejections suppressed."
        )
        return set()
    rejected = set()
    for row in frame.to_dict("records"):
        if str(row.get("decision", "")).strip().upper() == "REJECT":
            rejected.add(
                review_candidate_key(
                    row.get("repo_uid"), row.get("id_type"), row.get("candidate_id")
                )
            )
    return rejected


# ─── main scan loop ──────────────────────────────────────────────────────────
def run_scan(cards_dir, delay, limit, mode, active_id_types, only_missing,
            verbose=False, telemetry=None,
            re3data_catalog_cache=DEFAULT_RE3DATA_CATALOG_CACHE,
            re3data_catalog_max_age_days=RE3DATA_CATALOG_MAX_AGE_DAYS,
            force_refresh_catalog=False,
            re3data_cache_read_only=DEFAULT_RE3DATA_CACHE_READ_ONLY,
            rejected_candidates=None,
            re3data_seed_mappings=DEFAULT_RE3DATA_SEEDS):
    """
    mode: 'verify' | 'search' | 'full'
      verify -> check existing links of the active types only
      search -> for cards missing (or, if not only_missing, also mismatched/
                broken) links of the active types, search for candidates
      full   -> both, for every active type
    only_missing: if True, search mode/full-mode search only fires for
      id_types the card has NO link for at all. If False, search also
      tries to replace broken/mismatched links.

    re3data candidates come from a locally-scored full catalog (see
    get_re3data_catalog) rather than a live truncated search, since the
    live API does not rank by relevance. Cross-source corroboration is
    bidirectional (v10): a re3data record's own listed RRID/DOI can boost
    an RRID or FAIRsharing candidate found the same run, not just the
    reverse. Every candidate is recorded with both a review-eligibility
    flag and a direct-apply-eligibility flag (confidence >= 0.95 AND
    independent evidence); only direct-apply-eligible candidates become
    write-proposals.
    """
    global _ACTIVE_RE3DATA_CATALOG, _ACTIVE_RE3DATA_SEEDS
    telemetry = telemetry or RunTelemetry()
    _ACTIVE_RE3DATA_SEEDS = load_re3data_seed_mappings(re3data_seed_mappings)
    if _ACTIVE_RE3DATA_SEEDS:
        info(f"Loaded {len(_ACTIVE_RE3DATA_SEEDS)} confirmed re3data seed mapping(s) from {re3data_seed_mappings}.")
    rejected_candidates = rejected_candidates or set()
    rejected_suppressed_count = 0
    http = Http(delay, verbose=verbose, telemetry=telemetry)

    if "re3data" in active_id_types:
        _ACTIVE_RE3DATA_CATALOG = get_re3data_catalog(
            http, re3data_catalog_cache, max_age_days=re3data_catalog_max_age_days,
            verbose=verbose, telemetry=telemetry, force_refresh=force_refresh_catalog,
            read_only=re3data_cache_read_only,
        )
        info(f"re3data catalog ready: {len(_ACTIVE_RE3DATA_CATALOG)} records available "
            f"for local scoring.")

    files = sorted(set(cards_dir.glob("*.yaml")) | set(cards_dir.glob("*.yml")))
    if limit > 0:
        files = files[:limit]

    do_verify = mode in ("verify", "full")
    do_search = mode in ("search", "full")

    info(f"Cards selected: {len(files)} from {cards_dir} | mode={mode} | "
        f"sources={sorted(active_id_types)} | only_missing={only_missing}")
    debug(f"Selected files: {[f.stem for f in files]}", verbose)

    rows, proposals = [], []
    already_present_count = 0
    searched_count = 0

    for index, path in enumerate(files, start=1):
        try:
            card = load_card(path, verbose=verbose)
        except Exception as exc:
            telemetry.record_error("card_io", "unhandled_exception",
                                   f"{path}: {exc}\n{traceback.format_exc()}")
            continue
        if not card:
            continue

        repo_uid = str(card.get("repo_uid", "")).strip()
        repository_name = get_name(card)
        repository_names = get_names(card)
        homepage = get_homepage(card)
        card_identifiers = get_card_registry_identifiers(card)
        links = get_relevant_links(card, active_id_types)
        all_card_links = [
            link
            for link in (card.get("links") or [])
            if isinstance(link, dict)
        ]
        confirmed_present_types = {
            str(link.get("id_type", "")).strip().lower()
            for link in links
            if link_has_confirmed_identifier(link)
        }
        unresolved_links_by_type = {}
        for link in links:
            id_type = str(link.get("id_type", "")).strip().lower()
            if id_type and not link_has_confirmed_identifier(link):
                unresolved_links_by_type.setdefault(id_type, link)

        # ── verify existing links ───────────────────────────────────────
        if do_verify:
            for link in links:
                id_type = str(link.get("id_type", "")).strip().lower()
                id_value = link.get("id_value")
                url = link.get("url", "")
                verify_fn = VERIFY_FUNCS.get(id_type)
                if verify_fn is None:
                    continue

                info(f"[verify] {index}/{len(files)} | {repo_uid or path.stem} | "
                    f"{id_type} | {id_value or '(blank)'}")

                try:
                    if id_type == "re3data":
                        result = verify_fn(http, repository_name, homepage, id_value, url,
                                          verbose=verbose, repo_uid=repo_uid)
                    else:
                        result = verify_fn(http, repository_name, id_value, url,
                                          verbose=verbose, repo_uid=repo_uid)
                except Exception as exc:
                    telemetry.record_error(ID_TYPE_TO_SOURCE.get(id_type, id_type),
                                           "unhandled_exception",
                                           f"{exc}\n{traceback.format_exc()}", repo_uid, id_type)
                    result = ("", "", "", "", "broken", "unhandled_exception", str(exc))

                status, resolved_url, resolved_name, ratio, verdict, basis, notes = result
                candidate = None

                needs_candidate = (
                    do_search and not only_missing
                    and verdict in {"broken", "name_mismatch", "unresolved_search_only"}
                )
                if needs_candidate:
                    search_fn = SEARCH_FUNCS.get(id_type)
                    if search_fn and id_type == "re3data":
                        found = search_fn(http, repository_name, homepage, verbose=verbose,
                                          repo_uid=repo_uid, card_names=repository_names,
                                          card_identifiers=card_identifiers)
                    else:
                        found = search_fn(http, repository_name, homepage, verbose=verbose,
                                          repo_uid=repo_uid) if search_fn else []
                    before_filter = len(found)
                    found = [
                        candidate for candidate in found
                        if review_candidate_key(repo_uid, id_type, candidate.get("id_value"))
                        not in rejected_candidates
                    ]
                    rejected_suppressed_count += before_filter - len(found)
                    threshold = SOURCE_CANDIDATE_THRESHOLDS.get(id_type, CANDIDATE_THRESHOLD)
                    if found:
                        eligible_review, eligible_direct, evidence_kind = evaluate_direct_apply(
                            found[0], threshold)
                        candidate = dict(found[0], eligible=eligible_review,
                                        direct_apply_eligible=eligible_direct,
                                        evidence_kind=evidence_kind, threshold_used=threshold)
                        if eligible_direct:
                            action = ("fill_search_only_blank" if not id_value
                                     else "replace_mismatched_or_invalid_link")
                            proposals.append(build_proposal(
                                path, repo_uid, repository_name, id_type, action,
                                {"id_type": id_type, "id_value": id_value, "url": url,
                                 "tier": link.get("tier", "registry")}, found[0]))

                rows.append(report_row(repo_uid, repository_name, id_type, id_value, url,
                                       status, resolved_url, resolved_name, ratio, verdict,
                                       basis, notes, candidate))

        # ── search for missing confirmed identifiers ─────────────────
        # A URL-only registry search placeholder is not a confirmed link.
        # Under --only-missing it is searched and, when direct-apply eligible,
        # replaced in place rather than causing a duplicate link.
        if do_search:
            missing_id_types = [
                id_type for id_type in active_id_types
                if id_type not in confirmed_present_types
            ]
            already_present_count += len(active_id_types) - len(missing_id_types)

            if missing_id_types:
                info(f"[search-missing] {index}/{len(files)} | {repo_uid or path.stem} | "
                    f"searching {sorted(missing_id_types)} concurrently")
                searched_count += len(missing_id_types)

                results_by_type = search_missing_for_card(
                    http, repository_name, homepage, missing_id_types,
                    verbose=verbose, repo_uid=repo_uid, telemetry=telemetry,
                    card_names=repository_names,
                    card_identifiers=card_identifiers,
                )

                for id_type, found in list(results_by_type.items()):
                    before_filter = len(found)
                    results_by_type[id_type] = [
                        candidate for candidate in found
                        if review_candidate_key(repo_uid, id_type, candidate.get("id_value"))
                        not in rejected_candidates
                    ]
                    rejected_suppressed_count += before_filter - len(results_by_type[id_type])

                apply_cross_source_corroboration(
                    results_by_type, all_card_links, verbose=verbose, repo_uid=repo_uid,
                )

                card_top_candidates = []
                for id_type, found in results_by_type.items():
                    if not found:
                        continue
                    top = found[0]
                    threshold = SOURCE_CANDIDATE_THRESHOLDS.get(id_type, CANDIDATE_THRESHOLD)
                    eligible_review, eligible_direct, evidence_kind = evaluate_direct_apply(
                        top, threshold)
                    card_top_candidates.append(
                        (id_type, top, eligible_review, eligible_direct, evidence_kind, threshold))

                card_top_candidates.sort(key=lambda t: t[1]["confidence"], reverse=True)

                for id_type, candidate, eligible_review, eligible_direct, evidence_kind, threshold in card_top_candidates:
                    candidate_with_flag = dict(candidate, eligible=eligible_review,
                                               direct_apply_eligible=eligible_direct,
                                               evidence_kind=evidence_kind,
                                               threshold_used=threshold)
                    rows.append(report_row(
                        repo_uid, repository_name, id_type, "", "",
                        candidate["http_status"], candidate["resolved_url"],
                        candidate["resolved_name"], candidate["name_match_ratio"],
                        "unresolved_search_only",
                        "missing_link_candidate_found" if eligible_review
                        else "missing_link_candidate_below_threshold",
                        (f"No link existed for this type; top candidate found "
                        f"({'meets' if eligible_review else 'below'} {id_type} threshold "
                        f"of {threshold}); direct-apply "
                        f"{'ELIGIBLE (' + evidence_kind + ')' if eligible_direct else 'not eligible'}."),
                        candidate_with_flag))
                    if eligible_direct:
                        existing_unresolved = unresolved_links_by_type.get(id_type)
                        if existing_unresolved is None:
                            action = "add_missing_link"
                        elif not str(existing_unresolved.get("id_value") or "").strip():
                            action = "fill_search_only_blank"
                        else:
                            action = "replace_mismatched_or_invalid_link"
                        proposals.append(build_proposal(
                            path, repo_uid, repository_name, id_type,
                            action, existing_unresolved, candidate))

        if index % 25 == 0 or index == len(files):
            info(f"Progress checkpoint: {index}/{len(files)} cards processed.")

    if do_search:
        info(f"Search-missing tally: {searched_count} (card, id_type) pairs searched, "
            f"{already_present_count} skipped (already had that id_type).")
        if rejected_suppressed_count:
            info(
                f"Reviewer rejection filter suppressed {rejected_suppressed_count} "
                "candidate occurrence(s)."
            )

    proposals.sort(key=lambda p: p["validation"]["confidence"], reverse=True)

    return rows, proposals


def write_csv(rows, output_path):
    fields = ["repo_uid", "card_name", "id_type", "id_value", "url", "http_status",
              "resolved_url", "resolved_name", "name_match_ratio", "verdict", "match_basis",
              "candidate_id", "candidate_url", "candidate_name", "candidate_ratio",
              "candidate_confidence", "candidate_source", "candidate_eligible",
              "candidate_direct_apply_eligible", "candidate_evidence_kind",
              "candidate_threshold_used", "notes"]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    _atomic_write_text(output_path, buffer.getvalue())


def write_proposals(proposals, output_path):
    document = {
        "generated_at_local": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "environment": TRACKER_ENV,
        "proposal_count": len(proposals),
        "review_required": True,
        "proposals": proposals,
    }
    text = yaml.safe_dump(
        document,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    _atomic_write_text(output_path, text)


def apply_proposals(proposals, cards_dir=None, verbose=False):
    """Apply direct-eligible proposals as one locked, rollback-capable batch."""
    if tracker_common is None:
        raise RuntimeError(
            "common.py is required for --apply so card writes can be locked, "
            "snapshotted, and rolled back. Standalone verifier use is read-only."
        )
    by_file = defaultdict(list)
    for proposal in proposals:
        by_file[proposal["card_file"]].append(proposal)

    changed, skipped = [], []
    cards_root = Path(cards_dir or DEFAULT_CARDS).expanduser().resolve()
    project_root = TRACKER_PROJECT.expanduser().resolve()
    try:
        cards_root.relative_to(project_root)
    except ValueError:
        project_root = cards_root.parent

    write_context = tracker_common.card_transaction(
        "verify_external_ids",
        project_root=project_root,
        cards_dir=cards_root,
    )

    with write_context as transaction:
        for file_name, card_proposals in by_file.items():
            path = Path(file_name).expanduser().resolve()
            try:
                path.relative_to(cards_root)
            except ValueError:
                skipped.append((file_name, f"Card path is outside selected cards directory {cards_root}."))
                continue

            expected_hashes = {
                str(item.get("expected_card_sha256") or "").strip()
                for item in card_proposals
                if str(item.get("expected_card_sha256") or "").strip()
            }
            if len(expected_hashes) > 1:
                skipped.append(
                    (file_name, "Proposals for this card were built from different card versions.")
                )
                continue
            if expected_hashes and _sha256_file(path) != next(iter(expected_hashes)):
                skipped.append(
                    (
                        file_name,
                        "Card bytes changed after verification; rerun verification before apply.",
                    )
                )
                continue

            card = load_card(path, verbose=verbose)
            if not card:
                skipped.append((file_name, "Could not reload card."))
                continue

            before = deepcopy(card)
            links = card.get("links")
            if not isinstance(links, list):
                links = []
                card["links"] = links

            for proposal in card_proposals:
                action = proposal["action"]
                id_type = proposal["id_type"]
                replacement = proposal["proposed_link"]

                if action == "add_missing_link":
                    same_type = [
                        link for link in links
                        if isinstance(link, dict)
                        and str(link.get("id_type", "")).lower() == id_type
                    ]
                    exists = any(
                        str(link.get("id_value", "")).lower()
                        == str(replacement["id_value"]).lower()
                        for link in same_type
                    )
                    if not exists and same_type:
                        skipped.append(
                            (
                                file_name,
                                f"A different {id_type} link appeared after proposal; "
                                "did not add a second identifier.",
                            )
                        )
                    elif not exists:
                        links.append(replacement)
                else:
                    original = proposal["existing_link"] or {}
                    replaced = False
                    for link in links:
                        if not isinstance(link, dict):
                            continue
                        if (
                            str(link.get("id_type", "")).lower() == id_type
                            and str(link.get("id_value") or "").strip()
                            == str(original.get("id_value") or "").strip()
                            and str(link.get("url") or "").strip()
                            == str(original.get("url") or "").strip()
                        ):
                            link.clear()
                            link.update(replacement)
                            replaced = True
                            break
                    if not replaced:
                        skipped.append(
                            (
                                file_name,
                                f"Original {id_type} link changed since proposal; did not overwrite.",
                            )
                        )

            if card != before:
                transaction.write_yaml(path, card, operation="verify_external_ids_apply")
                changed.append(file_name)

    return changed, skipped


def print_summary(rows, proposals, telemetry, active_id_types):
    counts = defaultdict(Counter)
    for row in rows:
        counts[row["id_type"]][row["verdict"]] += 1

    print("\nVerification summary")
    print("=" * 100)
    for id_type in sorted(active_id_types):
        total = counts[id_type]
        print(f"{id_type}: verified={total['verified']}, name_mismatch={total['name_mismatch']}, "
             f"broken={total['broken']}, unresolved_search_only={total['unresolved_search_only']}")

    eligible_rows = [r for r in rows if r.get("candidate_eligible") == "True"]
    below_threshold_rows = [r for r in rows if r.get("candidate_eligible") == "False"]
    direct_apply_rows = [r for r in rows if r.get("candidate_direct_apply_eligible") == "True"]

    print(f"\nTotal report rows: {len(rows)}")
    print(f"Candidates found and review-ELIGIBLE (met that source's threshold): {len(eligible_rows)}")
    print(f"Candidates found but BELOW threshold (shown for review, not proposed): "
         f"{len(below_threshold_rows)}")
    print(f"Candidates DIRECT-APPLY eligible (confidence >= {DIRECT_APPLY_CONFIDENCE} "
         f"AND independent evidence): {len(direct_apply_rows)}")
    print(f"High-confidence candidate proposals written: {len(proposals)}")

    print("\nRun health (structured errors)")
    print("=" * 100)
    summary = telemetry.summary()
    if not telemetry.has_errors():
        print("No transport-level errors (timeouts, 5xx-after-retry, exceptions) this run.")
    else:
        print(f"Calls attempted by source:  {summary['total_calls_by_source']}")
        print(f"Failed calls by source:     {summary['total_failures_by_source']}")
        print("Error breakdown (source:error_class -> count):")
        for key, n in sorted(summary["error_counts_by_source_and_class"].items(),
                             key=lambda kv: -kv[1]):
            print(f"  {key:<45} {n}")
        affected_uids = sorted({e["repo_uid"] for e in telemetry.errors if e.get("repo_uid")})
        if affected_uids:
            print(f"Cards affected by at least one transport error: {', '.join(affected_uids)}")
        if summary["error_detail_rows_truncated"]:
            print(f"  (detail capped at {telemetry.max_detail_rows} rows; "
                 "full counts above are exact, see verify_run_report.json for all captured detail)")

    for heading, verdict in (("Name-mismatch worklist", "name_mismatch"),
                             ("Broken-link worklist", "broken"),
                             ("Honest-blank / unresolved-search-only worklist", "unresolved_search_only")):
        print(f"\n{heading}")
        print("=" * 100)
        subset = [row for row in rows if row["verdict"] == verdict]
        if not subset:
            print("None")
            continue
        for row in subset:
            eligibility = ""
            if row.get("candidate_direct_apply_eligible") == "True":
                eligibility = f" [DIRECT-APPLY: {row.get('candidate_evidence_kind', '')}]"
            elif row.get("candidate_eligible") == "True":
                eligibility = " [review-eligible only]"
            elif row.get("candidate_eligible") == "False":
                eligibility = " [below threshold]"
            candidate = (f" | candidate={row['candidate_id']} "
                        f"confidence={row['candidate_confidence']}{eligibility}"
                        if row["candidate_id"] else "")
            print(f"{row['repo_uid'] or '(missing uid)'} | {row['id_type']} | "
                 f"id={row['id_value'] or '(blank)'} | card={row['card_name']!r} | "
                 f"resolved={row['resolved_name']!r} | HTTP={row['http_status'] or '(none)'} | "
                 f"basis={row['match_basis']}{candidate}")

    by_card = defaultdict(set)
    for row in rows:
        if row["candidate_id"]:
            by_card[row["repo_uid"] or row["card_name"]].add(row["id_type"])

    multi_source_cards = {card: types for card, types in by_card.items() if len(types) > 1}
    if multi_source_cards:
        print("\nCards with candidates from multiple sources this run")
        print("=" * 100)
        for card, types in sorted(multi_source_cards.items()):
            print(f"  {card}: {sorted(types)}")


def smoke_test(delay, verbose=False, telemetry=None):
    info("Running smoke test.")
    telemetry = telemetry or RunTelemetry()
    http = Http(delay, verbose=verbose, telemetry=telemetry)

    tests = [
        ("SciCrunch RRID (verify)", lambda: verify_rrid(
            http, "Brain Analysis Library of Spatial maps and Atlases (BALSA)",
            "RRID:SCR_022960", "https://scicrunch.org/resolver/RRID:SCR_022960", verbose=verbose)),
        ("FAIRsharing DOI (verify)", lambda: verify_fairsharing(
            http, "FAIRsharing", "10.25504/FAIRsharing.533ac3",
            "https://fairsharing.org/10.25504/FAIRsharing.533ac3", verbose=verbose)),
        ("re3data record (verify)", lambda: verify_re3data(
            http, "FAIRsharing", "https://fairsharing.org/", "r3d100010142", "", verbose=verbose)),
    ]

    print("\nSmoke-test results (verify)")
    print("=" * 100)
    for label, callback in tests:
        try:
            status, resolved_url, title, ratio, verdict, basis, notes = callback()
            print(f"{label}: HTTP={status or '(none)'} | verdict={verdict} | title={title!r} | "
                 f"ratio={ratio or '(unavailable)'} | basis={basis} | final_url={resolved_url or '(none)'}")
            debug(f"{label} notes: {notes}", verbose)
        except Exception as exc:
            print(f"{label}: isolated smoke-test failure: {exc}")

    print("\nSmoke-test results (search)")
    print("=" * 100)
    print("NOTE: re3data smoke search now uses local catalog scoring; skipped here "
         "since it requires a full catalog load (~3500 records). Run a real "
         "--mode search pass to exercise it.")
    search_tests = [
        ("FAIRsharing search", lambda: search_fairsharing_candidates(http, "UniProt", "", verbose=verbose)),
        ("SciCrunch search", lambda: search_rrid_candidates(http, "WormBase", "", verbose=verbose)),
    ]
    for label, callback in search_tests:
        try:
            found = callback()
            top = found[0] if found else None
            print(f"{label}: {len(found)} candidate(s) | top="
                 f"{(top['id_value'], top['confidence']) if top else 'none'}")
        except Exception as exc:
            print(f"{label}: isolated smoke-test failure: {exc}")

    if telemetry.has_errors():
        print("\nSmoke test produced transport errors — see summary below.")
        print(json.dumps(telemetry.summary(), indent=2))

    info("Smoke test complete.")


def parse_sources(raw):
    if not raw:
        return set(ALL_SOURCES)
    tokens = {t.strip().lower() for t in raw.split(",") if t.strip()}
    unknown = tokens - set(ALL_SOURCES)
    if unknown:
        print(f"Warning: unknown --sources value(s) {sorted(unknown)}; "
             f"valid values are {ALL_SOURCES}. Ignoring unknown values.", file=sys.stderr)
        tokens -= unknown
    return tokens or set(ALL_SOURCES)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cards", default=DEFAULT_CARDS)
    parser.add_argument("--out", default=DEFAULT_REPORT)
    parser.add_argument("--proposals-out", default=DEFAULT_PROPOSALS)
    parser.add_argument("--run-report-out", default=DEFAULT_RUN_REPORT)
    parser.add_argument("--re3data-catalog-cache", default=DEFAULT_RE3DATA_CATALOG_CACHE,
                        help="Path to the cached full re3data catalog JSON.")
    parser.add_argument("--re3data-seeds", default=DEFAULT_RE3DATA_SEEDS,
                        help="Source-bound confirmed re3data mappings for deterministic recovery of known records.")
    parser.add_argument(
        "--allow-cache-write",
        action="store_true",
        help="Allow this run to refresh/write the shared re3data cache. "
             "Dev/staging are read-only by default.",
    )
    parser.add_argument(
        "--verify-adjudications",
        default=DEFAULT_VERIFY_ADJUDICATIONS,
        help="Structured reviewer decisions; REJECT rows suppress the same candidate "
             "from future review queues.",
    )
    parser.add_argument("--re3data-catalog-max-age-days", type=int,
                        default=RE3DATA_CATALOG_MAX_AGE_DAYS,
                        help="Re-download the full re3data catalog if the cache is older "
                             "than this many days.")
    parser.add_argument("--refresh-catalog", action="store_true",
                        help="Force a fresh re3data catalog download even if the cache "
                             "is not yet stale.")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--smoke-test-only", action="store_true")
    parser.add_argument("--mode", choices=["verify", "search", "full"], default="verify",
                        help="verify = check existing links; search = find candidates for "
                             "missing/bad links; full = both. Default: verify.")
    parser.add_argument("--sources", default="",
                        help="Comma-separated subset of rrid,fairsharing,re3data. "
                             "Default: all three.")
    parser.add_argument("--only-missing", action="store_true",
                        help="In search/full mode, only search for id_types the card has "
                             "NO link for. Without this flag, search/full also tries to "
                             "find replacement candidates for broken/mismatched links.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--strict-errors", action="store_true",
                        help="Exit code 1 if any transport-level errors occurred, even "
                             "though partial results were still written.")
    parser.add_argument("--find-candidates", action="store_true",
                        help="Deprecated alias for --mode full (kept for old scripts/notebooks).")
    return parser.parse_args()


def main():
    args = parse_args()

    network_allowed = str(os.getenv("NIH_TRACKER_EXTERNAL_NETWORK_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}
    if not network_allowed:
        raise RuntimeError("verify_external_ids.py requires NIH_TRACKER_EXTERNAL_NETWORK_ENABLED=1; the pipeline is in offline/cached-only mode")

    if args.find_candidates and args.mode == "verify":
        args.mode = "full"
        info("Note: --find-candidates is deprecated; treating as --mode full.")

    if args.delay < MIN_DELAY:
        print(f"Warning: delay below {MIN_DELAY}; using {MIN_DELAY}.", file=sys.stderr)
        args.delay = MIN_DELAY
    if args.limit < 0:
        print("Warning: negative limit changed to 0 (all cards).", file=sys.stderr)
        args.limit = 0
    if args.apply and args.mode == "verify":
        print("Warning: --apply needs --mode search or --mode full; applying disabled.",
             file=sys.stderr)
        args.apply = False

    active_id_types = {SOURCE_TO_ID_TYPE[s] for s in parse_sources(args.sources)}
    telemetry = RunTelemetry()

    info("verify_external_ids.py (v16.0) starting.")
    cache_read_only = DEFAULT_RE3DATA_CACHE_READ_ONLY and not args.allow_cache_write
    debug(f"env={TRACKER_ENV} | project={TRACKER_PROJECT} | Colab={in_colab()} | "
         f"lxml={LXML_ETREE is not None} | credential_policy={credential_use_allowed()} | "
         f"FS Basic Auth={bool(fairsharing_auth())} | "
         f"FAIRsharing token={bool(credential_use_allowed() and (os.getenv('FAIRSHARING_API_TOKEN') or os.getenv('FAIRSHARING_API_KEY')))} | "
         f"SciCrunch key={bool(scicrunch_api_key())} | "
         f"re3data_cache_read_only={cache_read_only} | "
         f"mode={args.mode} | sources={sorted(active_id_types)} | "
         f"only_missing={args.only_missing} | apply={args.apply} | "
         f"re3data_seeds={args.re3data_seeds} | "
         f"direct_apply_confidence={DIRECT_APPLY_CONFIDENCE}.", args.verbose)
    if not credential_use_allowed() and any(
        os.getenv(name) for name in
        ("FS_U", "FS_PWD", "FAIRSHARING_API_TOKEN", "FAIRSHARING_API_KEY", "SCICRUNCH_API_KEY")
    ):
        info(
            f"Credential variables are present but ignored in {TRACKER_ENV}; "
            "set NIH_TRACKER_ALLOW_NONPROD_CREDENTIALS=1 only for a deliberate exception."
        )

    ensure_drive(verbose=args.verbose)

    if args.smoke_test or args.smoke_test_only:
        smoke_test(args.delay, verbose=args.verbose, telemetry=telemetry)
    if args.smoke_test_only:
        info("Smoke-test-only workflow finished.")
        return 1 if (args.strict_errors and telemetry.has_errors()) else 0

    cards_dir = Path(args.cards)
    report_path = Path(args.out)
    proposals_path = Path(args.proposals_out)
    run_report_path = Path(args.run_report_out)

    if not cards_dir.is_dir():
        print(f"Warning: cards directory unavailable: {cards_dir}", file=sys.stderr)
        try:
            write_csv([], report_path)
            write_proposals([], proposals_path)
            telemetry.write_report(run_report_path, extra={"fatal": "cards_dir_missing"})
        except Exception as exc:
            print(f"Warning: could not write fallback outputs: {exc}", file=sys.stderr)
        return 1

    rejected_candidates = load_rejected_candidates(args.verify_adjudications)
    if rejected_candidates:
        info(
            f"Loaded {len(rejected_candidates)} reviewer-rejected candidate key(s) "
            f"from {args.verify_adjudications}."
        )

    run_dir = None
    changed, skipped = [], []
    try:
        rows, proposals = run_scan(
            cards_dir, args.delay, args.limit, args.mode, active_id_types,
            args.only_missing, verbose=args.verbose, telemetry=telemetry,
            re3data_catalog_cache=args.re3data_catalog_cache,
            re3data_catalog_max_age_days=args.re3data_catalog_max_age_days,
            force_refresh_catalog=args.refresh_catalog,
            re3data_cache_read_only=cache_read_only,
            rejected_candidates=rejected_candidates,
            re3data_seed_mappings=args.re3data_seeds,
        )
        write_csv(rows, report_path)
        write_proposals(proposals, proposals_path)

        if tracker_common is not None:
            run_dir = Path(tracker_common.new_run_dir("verify_ids"))
            write_csv(rows, run_dir / "verify_report.csv")
            write_proposals(proposals, run_dir / "verify_external_ids_proposals.yaml")

        print_summary(rows, proposals, telemetry, active_id_types)

        if args.apply:
            info("Applying direct-apply-eligible candidate proposals.")
            changed, skipped = apply_proposals(
                proposals,
                cards_dir=cards_dir,
                verbose=args.verbose,
            )
            info(f"Apply complete: cards_changed={len(changed)} updates_skipped={len(skipped)}.")
            if skipped:
                print("\nSkipped update operations")
                print("=" * 100)
                for file_name, reason in skipped:
                    print(f"{file_name}: {reason}")
        else:
            info("Read-only mode: YAML cards were not modified.")

        run_extra = {
            "mode": args.mode,
            "sources": sorted(active_id_types),
            "only_missing": args.only_missing,
            "rows": len(rows),
            "proposals": len(proposals),
            "apply": bool(args.apply),
            "cards_changed": len(changed),
            "updates_skipped": len(skipped),
            "re3data_catalog_cache": args.re3data_catalog_cache,
            "re3data_seed_mappings": args.re3data_seeds,
            "re3data_seed_count": len(_ACTIVE_RE3DATA_SEEDS),
            "re3data_cache_read_only": cache_read_only,
            "verify_adjudications": args.verify_adjudications,
            "rejected_candidate_keys_loaded": len(rejected_candidates),
            "run_dir": str(run_dir or ""),
        }
        telemetry.write_report(run_report_path, extra=run_extra)
        if run_dir is not None:
            telemetry.write_report(run_dir / "verify_run_report.json", extra=run_extra)
            apply_rows = [
                {"card_file": file_name, "status": "changed", "reason": ""}
                for file_name in changed
            ] + [
                {"card_file": file_name, "status": "skipped", "reason": reason}
                for file_name, reason in skipped
            ]
            buffer = io.StringIO(newline="")
            writer = csv.DictWriter(buffer, fieldnames=["card_file", "status", "reason"])
            writer.writeheader()
            writer.writerows(apply_rows)
            _atomic_write_text(run_dir / "apply_log.csv", buffer.getvalue())

        info(f"CSV report written: {report_path}")
        info(f"Proposal YAML written: {proposals_path}")
        info(f"Run report written: {run_report_path}")
        if run_dir is not None:
            info(f"Immutable run artifacts written: {run_dir}")

    except Exception as exc:
        print(f"Warning: unexpected workflow error: {exc}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        telemetry.record_error(
            "workflow",
            "unhandled_top_level_exception",
            f"{exc}\n{traceback.format_exc()}",
        )
        try:
            if not report_path.exists():
                write_csv([], report_path)
            if not proposals_path.exists():
                write_proposals([], proposals_path)
            telemetry.write_report(
                run_report_path,
                extra={
                    "fatal": str(exc),
                    "run_dir": str(run_dir or ""),
                    "re3data_cache_read_only": cache_read_only,
                },
            )
            if run_dir is not None:
                telemetry.write_report(
                    run_dir / "verify_run_report.json",
                    extra={"fatal": str(exc), "run_dir": str(run_dir)},
                )
        except Exception as fallback_error:
            print(f"Warning: fallback report write failed: {fallback_error}", file=sys.stderr)
        return 1

    info("Workflow complete.")
    if args.strict_errors and telemetry.has_errors():
        info("Exiting with code 1 due to --strict-errors and transport-level errors this run.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
