"""
step_build_rrids.py
Resolve RRIDs by bulk-joining cards against a local SciCrunch Tools export
(Tools467results.csv), NOT per-card live search. Match priority (safest first):
    1) homepage host exact           → confidence 1.00  (defeats cBioPortal/BioPortal)
    2) exact normalized name         → 0.95
    3) acronym match                 → 0.90
    4) fuzzy name (>= cutoff)        → cutoff  → review queue
No match → honest blank (no RRID written).

Then an OPTIONAL live-verify pass hits the SciCrunch resolver (uses the RRID key
from env/Colab secret if present) to confirm each matched SCR_ id actually resolves
and its name still matches — so a stale/wrong export row can't slip through.

Run (dry):   python step_build_rrids.py --tools Tools467results.csv
Verify+dry:  python step_build_rrids.py --tools Tools467results.csv --verify
Apply:       python step_build_rrids.py --tools Tools467results.csv --verify --apply

Params:
  --tools FILE     SciCrunch export in inputs/ (default: Tools467results.csv)
  --verify         live-check each matched RRID via resolver (recommended)
  --apply          write rrid links into cards (default: DRY RUN → proposal only)
  --fuzzy 0.90     fuzzy name cutoff
  --delay 0.34     seconds between live-verify calls (only used with --verify)
"""
import argparse, os, re, csv, time, json, difflib
from urllib.parse import urlparse
from common import (INP, slug, strip_acronym, all_cards, load_card_for_update, save_card,
                    card_transaction, gate, ledger_append, new_run_dir, NOW, name_matches, card_name_set, CREDENTIALS_ALLOWED)
import pandas as pd

YM = NOW.strftime("%Y-%m")
SCR_RE = re.compile(r"(SCR_\d+)", re.I)


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


def norm(s):
    return slug(strip_acronym(s)[0]) if s else ""


def acr(s):
    _, a = strip_acronym(s or "")
    return slug(a) if a else ""


def load_tools(path):
    """Index the SciCrunch export three ways: by homepage host, by name-slug, by acronym.
    Also keep the RAW records list so name-set matching can check ALL of a card's names
    against a record's name+abbreviations+synonyms, not just the canonical slug."""
    df = pd.read_csv(path); df.columns = [c.strip() for c in df.columns]
    name_c = "Resource Name"
    web_c  = "Resource Website" if "Resource Website" in df.columns else None
    id_c   = "Resource ID"
    abbr_c = "Abbreviations" if "Abbreviations" in df.columns else None
    syn_c  = "Synonyms" if "Synonyms" in df.columns else None

    by_host, by_name, by_acr, records = {}, {}, {}, []
    for _, r in df.iterrows():
        rid_raw = _clean(r.get(id_c)) or ""
        m = SCR_RE.search(rid_raw)
        if not m:
            continue
        rid = m.group(1)
        nm = _clean(r.get(name_c)) or ""
        alt_names = set()
        if abbr_c and _clean(r.get(abbr_c)):
            alt_names |= {a.strip() for a in re.split(r"[;,/]", str(r[abbr_c])) if a.strip()}
        if syn_c and _clean(r.get(syn_c)):
            alt_names |= {a.strip() for a in re.split(r"[;,/]", str(r[syn_c])) if a.strip()}
        rec = {"rrid": f"RRID:{rid}", "name": nm,
               "website": _clean(r.get(web_c)) if web_c else None,
               "alt_names": alt_names}
        records.append(rec)
        h = host(rec["website"])
        if h and h not in by_host:
            by_host[h] = rec
        ns = norm(nm)
        if ns and ns not in by_name:
            by_name[ns] = rec
        acrs = set(alt_names)
        if acr(nm): acrs.add(acr(nm))
        for a in acrs:
            by_acr.setdefault(a, rec)
    return records, by_host, by_name, by_acr


def match_card(card, records, by_host, name_index_unused, fuzzy_cut, name_index):
    """Homepage host first (safest), then NAME-SET match against each record's
    name + abbreviations + synonyms (not just one canonical slug), then fuzzy."""
    ident = card.get("identity", {})
    hp = ident.get("homepage", "")
    h = host(hp)
    # 1) homepage host — still the strongest signal
    if h and h in by_host:
        return by_host[h], 1.00, "homepage_host"
    # 2) name-SET match: does ANY of the card's names match this record's name/alt_names?
    for rec in records:
        matched, basis = name_matches(card, rec["name"], fuzzy=fuzzy_cut)
        if matched:
            return rec, (1.00 if basis in ("exact_name","acronym") else fuzzy_cut), f"nameset_{basis}"
        for alt in rec.get("alt_names", ()):
            matched, basis = name_matches(card, alt, fuzzy=fuzzy_cut)
            if matched:
                return rec, 0.95, f"nameset_alt_{basis}"
    return None, 0.0, "no_match"


def verify_rrid_live(rrid, card_name, delay):
    """Confirm the RRID resolves and its name still matches. Uses RRID key if set."""
    import requests
    key = ((os.getenv("RRID") or os.getenv("SCICRUNCH_API_KEY") or "").strip()
           if CREDENTIALS_ALLOWED else "")
    scr = SCR_RE.search(rrid).group(1)
    url = f"https://scicrunch.org/resolver/{scr}.json"
    headers = {"Accept": "application/json", "User-Agent": "NIH-Tracker/1.0"}
    if key: headers["apikey"] = key
    time.sleep(max(0.34, delay))
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        if resp.status_code >= 400:
            return "broken", "", resp.status_code
        data = resp.json()
        # pull a name out of the resolver payload (structure varies)
        def find_name(v):
            if isinstance(v, dict):
                for k in ("name", "title", "label"):
                    if isinstance(v.get(k), str) and v[k].strip(): return v[k].strip()
                for x in v.values():
                    r = find_name(x)
                    if r: return r
            if isinstance(v, list):
                for x in v:
                    r = find_name(x)
                    if r: return r
            return ""
        resolved = find_name(data)
        ratio = difflib.SequenceMatcher(None, norm(card_name), norm(resolved)).ratio()
        return ("verified" if ratio >= 0.6 or norm(resolved) in norm(card_name)
                or norm(card_name) in norm(resolved) else "name_mismatch"), resolved, resp.status_code
    except Exception as e:
        return "broken", str(e)[:60], ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tools", default="Tools467results.csv")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--fuzzy", type=float, default=0.90)
    ap.add_argument("--delay", type=float, default=0.34)
    args = ap.parse_args()

    network_allowed = str(os.getenv("NIH_TRACKER_EXTERNAL_NETWORK_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}
    if args.verify and not network_allowed:
        raise RuntimeError("--verify requires NIH_TRACKER_EXTERNAL_NETWORK_ENABLED=1; the pipeline is in offline/cached-only mode")

    tools_path = os.path.join(INP, args.tools)
    records, by_host, by_name, by_acr = load_tools(tools_path)
    name_index = list(by_name.keys())
    cards = all_cards()

    proposals, review = [], []
    for uid, c in cards.items():
        # skip cards that already have a real rrid
        has = any(l.get("id_type") == "rrid" and _clean(l.get("id_value"))
                  for l in c.get("links", []) or [])
        if has:
            continue
        rec, conf, basis = match_card(c, records, by_host, None, args.fuzzy, name_index)
        if not rec:
            continue
        row = {"repo_uid": uid, "card_name": c.get("identity", {}).get("name", ""),
               "rrid": rec["rrid"], "matched_name": rec["name"], "basis": basis,
               "confidence": conf, "verify": "", "resolved_name": ""}
        if args.verify:
            v, resolved, status = verify_rrid_live(rec["rrid"], row["card_name"], args.delay)
            row["verify"], row["resolved_name"] = v, resolved
            if v != "verified":
                review.append(row); continue      # don't auto-apply unverified
        # trust exact name-set / homepage matches; fuzzy-basis rows go to review
        if basis.startswith("fuzzy") or "fuzzy" in basis:
            review.append(row)
        else:
            proposals.append(row)

    checks = {
        "tools loaded":        (len(records) > 0, f"{len(records)} SciCrunch records with SCR_ ids"),
        "cards considered":    (len(cards) > 0, f"{len(cards)} cards"),
        "proposals well-formed":(all(SCR_RE.search(p["rrid"]) for p in proposals), "ok"),
        "no low-conf auto-apply":(all(p["confidence"] >= 0.90 for p in proposals),
                                  f"{len(proposals)} auto-apply candidates all >=0.90"),
    }
    ok = gate(checks)

    run_dir = new_run_dir("build_rrids")
    pd.DataFrame(proposals).to_csv(os.path.join(run_dir, "rrid_proposals.csv"), index=False)
    pd.DataFrame(review).to_csv(os.path.join(run_dir, "rrid_review.csv"), index=False)

    print(f"\n  auto-apply proposals : {len(proposals)}")
    print(f"  review queue (fuzzy/unverified): {len(review)}")
    if args.verify:
        vc = {}
        for r in proposals + review: vc[r["verify"]] = vc.get(r["verify"], 0) + 1
        print(f"  live-verify verdicts : {vc}")
    print("\n  sample proposals:")
    for p in proposals[:10]:
        print(f"    {p['repo_uid']:<6} {p['rrid']:<16} {p['basis']:<20} {p['card_name'][:40]}")

    applied = 0
    if args.apply and ok:
        with card_transaction("build_rrids"):
            proposal_uids = sorted({proposal["repo_uid"] for proposal in proposals})
            locked_cards = {
                uid: load_card_for_update(uid, cards[uid]) for uid in proposal_uids
            }
            for proposal in proposals:
                uid = proposal["repo_uid"]
                card = locked_cards[uid]
                card.setdefault("links", []).append(
                    {"id_type": "rrid", "id_value": proposal["rrid"],
                     "url": f"https://scicrunch.org/resolver/{proposal['rrid']}",
                     "tier": "registry", "as_of": YM})
                applied += 1
            for uid in proposal_uids:
                save_card(uid, locked_cards[uid])
        print(f"\n  ✓ transactionally applied {applied} RRID links")
    elif args.apply and not ok:
        print("\n  ⛔ gate failed — no card changes were written.")
    else:
        print("\n  DRY RUN — review rrid_proposals.csv + rrid_review.csv, then --apply")

    ledger_append("build_rrids",
                  params={"tools": args.tools, "verify": args.verify, "apply": args.apply,
                          "fuzzy": args.fuzzy, "matcher": "name_set"},
                  inputs={"tools": tools_path},
                  outputs={"tools_records": len(records), "proposals": len(proposals),
                           "review": len(review), "applied": applied},
                  gate_passed=ok, run_dir=run_dir)
    print(f"\n✓ {run_dir}")


if __name__ == "__main__":
    main()
