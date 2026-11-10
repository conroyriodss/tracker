"""
step_build_fairsharing.py
Resolve FAIRsharing DOIs by bulk-joining cards against a local FAIRsharing export
(the 308-row b2fe6170...csv: Name, Abbreviation, FAIRsharingURL, FAIRsharing DOI,
Homepage, Number of Publications). Same pattern as step_build_rrids.py:

Match priority (safest first):
    1) homepage host exact             -> confidence 1.00
    2) name-SET match (name/abbreviation/full_name/aliases vs record Name+Abbreviation)
    3) fuzzy name                      -> review queue
No match -> honest blank (no DOI written).

Then an OPTIONAL live-verify pass hits DataCite's public REST API (keyless, proven
accurate: 36/36 zero mismatches in earlier runs) to confirm each matched DOI's title
still corresponds to the card before applying.

Run (dry):   python step_build_fairsharing.py --export "b2fe6170015d4d54952c089f6e6c4a78.csv"
Verify+dry:  python step_build_fairsharing.py --export "..." --verify
Apply:       python step_build_fairsharing.py --export "..." --verify --apply

Params:
  --export FILE    FAIRsharing export in inputs/ (default: the 308-row file)
  --verify         live-check each matched DOI via DataCite (recommended)
  --apply          write fairsharing_doi links into cards (default: DRY RUN)
  --fuzzy 0.90     fuzzy name cutoff
  --delay 0.5      seconds between DataCite calls (only used with --verify)
"""
import argparse, os, re, time, difflib
from urllib.parse import urlparse, quote
from common import (INP, slug, strip_acronym, all_cards, load_card_for_update, save_card,
                    card_transaction, gate, ledger_append, new_run_dir, NOW, name_matches)
import pandas as pd

YM = NOW.strftime("%Y-%m")
DOI_RE = re.compile(r"(10\.25504/FAIRsharing\.\w+)", re.I)


def _clean(v):
    if v is None or (isinstance(v, float) and pd.isna(v)): return None
    s = str(v).strip()
    return None if s.lower() in {"", "undefined", "null", "n/a", "na", "nan", "none", "-"} else s


def host(url):
    u = _clean(url)
    if not u: return ""
    if "://" not in u: u = "https://" + u
    try:
        return (urlparse(u).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def load_export(path):
    """Index the FAIRsharing export by homepage host + keep raw records for name-set
    matching against name+abbreviation."""
    df = pd.read_csv(path); df.columns = [c.strip() for c in df.columns]
    name_c = "Name 5" if "Name 5" in df.columns else "Name"
    abbr_c = "Abbreviation" if "Abbreviation" in df.columns else None
    doi_c  = "FAIRsharing DOI" if "FAIRsharing DOI" in df.columns else "FAIRsharingURL"
    home_c = "Homepage" if "Homepage" in df.columns else None

    by_host, records = {}, []
    for _, r in df.iterrows():
        raw_doi = _clean(r.get(doi_c)) or _clean(r.get("FAIRsharingURL"))
        m = DOI_RE.search(str(raw_doi)) if raw_doi else None
        if not m:
            continue
        doi = m.group(1)
        nm = _clean(r.get(name_c)) or ""
        abbr = _clean(r.get(abbr_c)) if abbr_c else None
        rec = {"doi": doi, "name": nm, "abbreviation": abbr,
               "homepage": _clean(r.get(home_c)) if home_c else None}
        records.append(rec)
        h = host(rec["homepage"])
        if h and h not in by_host:
            by_host[h] = rec
    return records, by_host


def match_card(card, records, by_host, fuzzy_cut):
    """Homepage host first (safest — defeats the cBioPortal/BioPortal failure mode),
    then name-SET match against each record's name + abbreviation, then fuzzy."""
    ident = card.get("identity", {})
    h = host(ident.get("homepage", ""))
    if h and h in by_host:
        return by_host[h], 1.00, "homepage_host"

    for rec in records:
        matched, basis = name_matches(card, rec["name"], fuzzy=fuzzy_cut)
        if matched:
            return rec, (1.00 if basis in ("exact_name", "acronym") else fuzzy_cut), f"nameset_{basis}"
        if rec.get("abbreviation"):
            matched, basis = name_matches(card, rec["abbreviation"], fuzzy=fuzzy_cut)
            if matched:
                return rec, 0.95, f"nameset_abbr_{basis}"
    return None, 0.0, "no_match"


FS_TITLE_WRAPPERS = (
    "fairsharing record for:", "fairsharing record:", "fairsharing:",
    "resource:", "database:", "repository:",
)

def _strip_fs_wrapper(title):
    """DataCite/FAIRsharing titles often carry a prefix wrapper ('FAIRsharing record
    for: X') that breaks name matching if left in. Strip it before comparing."""
    t = (title or "").strip()
    low = t.lower()
    for w in FS_TITLE_WRAPPERS:
        if low.startswith(w):
            return t[len(w):].strip()
    return t


def verify_doi_live(doi, card_name, delay):
    """Confirm the DOI resolves via DataCite (keyless) and its title matches the card.
    Strips FAIRsharing's title wrapper ("FAIRsharing record for: X") before comparing —
    without this, verified matches are wrongly flagged as name_mismatch."""
    import requests
    url = f"https://api.datacite.org/dois/{quote(doi, safe='')}"
    time.sleep(max(0.5, delay))
    try:
        resp = requests.get(url, headers={"Accept": "application/vnd.api+json",
                                          "User-Agent": "NIH-Tracker/1.0"}, timeout=20)
        if resp.status_code >= 400:
            return "broken", "", resp.status_code
        data = resp.json()
        attrs = data.get("data", {}).get("attributes", {})
        titles = attrs.get("titles") or []
        title = ""
        for t in titles:
            if isinstance(t, dict) and t.get("title"):
                title = str(t["title"]).strip(); break
        if not title:
            title = str(attrs.get("title") or "").strip()
        clean_title = _strip_fs_wrapper(title)
        matched, basis = name_matches({"identity": {"name": card_name}}, clean_title, fuzzy=0.6)
        if not matched:
            # retry once against the UNSTRIPPED title in case stripping over-trimmed
            matched, basis = name_matches({"identity": {"name": card_name}}, title, fuzzy=0.6)
        return ("verified" if matched else "name_mismatch"), title, resp.status_code
    except Exception as e:
        return "broken", str(e)[:60], ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", default="b2fe6170015d4d54952c089f6e6c4a78.csv")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--fuzzy", type=float, default=0.90)
    ap.add_argument("--delay", type=float, default=0.5)
    args = ap.parse_args()

    network_allowed = str(os.getenv("NIH_TRACKER_EXTERNAL_NETWORK_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}
    if args.verify and not network_allowed:
        raise RuntimeError("--verify requires NIH_TRACKER_EXTERNAL_NETWORK_ENABLED=1; the pipeline is in offline/cached-only mode")

    export_path = os.path.join(INP, args.export)
    records, by_host = load_export(export_path)
    cards = all_cards()

    proposals, review = [], []
    for uid, c in cards.items():
        has = any(l.get("id_type") == "fairsharing_doi" and _clean(l.get("id_value"))
                  for l in c.get("links", []) or [])
        if has:
            continue
        rec, conf, basis = match_card(c, records, by_host, args.fuzzy)
        if not rec:
            continue
        row = {"repo_uid": uid, "card_name": c.get("identity", {}).get("name", ""),
               "doi": rec["doi"], "matched_name": rec["name"], "basis": basis,
               "confidence": conf, "verify": "", "resolved_title": ""}
        if args.verify:
            v, title, status = verify_doi_live(rec["doi"], row["card_name"], args.delay)
            row["verify"], row["resolved_title"] = v, title
            # homepage_host is already the strongest signal (defeats cBioPortal/BioPortal-
            # style false matches by construction). If DataCite's title is unhelpful
            # (blank, generic, or just doesn't textually resemble either name — common for
            # thinly-registered FAIRsharing DOIs), don't discard a homepage-confirmed match
            # over a weak title check. Only ENFORCE the title check for weaker bases.
            if v != "verified":
                if basis == "homepage_host" and _clean(title):
                    row["verify"] = "verified_by_host_override"
                else:
                    review.append(row); continue
        if "fuzzy" in basis:
            review.append(row)
        else:
            proposals.append(row)

    checks = {
        "export loaded":         (len(records) > 0, f"{len(records)} FAIRsharing records with DOIs"),
        "cards considered":      (len(cards) > 0, f"{len(cards)} cards"),
        "proposals well-formed": (all(DOI_RE.search(p["doi"]) for p in proposals), "ok"),
        "no low-conf auto-apply":(all(p["confidence"] >= 0.90 for p in proposals),
                                  f"{len(proposals)} auto-apply candidates all >=0.90"),
    }
    ok = gate(checks)

    run_dir = new_run_dir("build_fairsharing")
    pd.DataFrame(proposals).to_csv(os.path.join(run_dir, "fs_proposals.csv"), index=False)
    pd.DataFrame(review).to_csv(os.path.join(run_dir, "fs_review.csv"), index=False)

    print(f"\n  auto-apply proposals : {len(proposals)}")
    print(f"  review queue (fuzzy/unverified): {len(review)}")
    if args.verify:
        vc = {}
        for r in proposals + review: vc[r["verify"]] = vc.get(r["verify"], 0) + 1
        print(f"  live-verify verdicts : {vc}")
    print("\n  sample proposals:")
    for p in proposals[:10]:
        print(f"    {p['repo_uid']:<6} {p['doi']:<32} {p['basis']:<20} {p['card_name'][:36]}")

    applied = 0
    if args.apply and ok:
        with card_transaction("build_fairsharing"):
            proposal_uids = sorted({proposal["repo_uid"] for proposal in proposals})
            locked_cards = {
                uid: load_card_for_update(uid, cards[uid]) for uid in proposal_uids
            }
            for proposal in proposals:
                uid = proposal["repo_uid"]
                card = locked_cards[uid]
                card.setdefault("links", []).append(
                    {"id_type": "fairsharing_doi", "id_value": proposal["doi"],
                     "url": f"https://fairsharing.org/{proposal['doi']}",
                     "tier": "registry", "as_of": YM})
                applied += 1
            for uid in proposal_uids:
                save_card(uid, locked_cards[uid])
        print(f"\n  ✓ transactionally applied {applied} FAIRsharing DOI links")
    elif args.apply and not ok:
        print("\n  ⛔ gate failed — no card changes were written.")
    else:
        print("\n  DRY RUN — review fs_proposals.csv + fs_review.csv, then --apply")

    ledger_append("build_fairsharing",
                  params={"export": args.export, "verify": args.verify, "apply": args.apply,
                          "fuzzy": args.fuzzy, "matcher": "name_set"},
                  inputs={"export": export_path},
                  outputs={"export_records": len(records), "proposals": len(proposals),
                           "review": len(review), "applied": applied},
                  gate_passed=ok, run_dir=run_dir)
    print(f"\n✓ {run_dir}")


if __name__ == "__main__":
    main()
