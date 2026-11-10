"""
step_materialize_cards.py
Create one cards/<repo_uid>.yaml per BMIC row: identity (multi-name ready) + links +
a bmic evidence block, plus any already-built blocks (interoperability from latest/).

SAFETY: refuses to build into a NON-EMPTY cards/ unless --reset (archive+clear) or
--overwrite (rewrite in place, warns about rename-orphans). This prevents the
duplicate-card explosion that happens when a prior reconcile renamed B->R.

Run (first build / clean rebuild):
    python step_materialize_cards.py --bmic "BMIC_download.csv" --attach interoperability --reset
Params:
  --bmic FILE      BMIC export in inputs/ (default: BMIC_download.csv)
  --attach BLOCKS  comma list of latest/<block> to fold in (default: interoperability)
  --reset          archive cards/ to a zip, clear it, then build clean
  --overwrite      rewrite same-id files in place (risky after reconcile; warns)
"""
import argparse, os, re
from common import (INP, CARDS, LATEST, slug, strip_acronym, load_card, save_card,
                    card_transaction, snapshot_cards_zip, all_cards, gate,
                    ledger_append, new_run_dir, NOW)
import pandas as pd, yaml, json

NOW_YM = NOW.strftime("%Y-%m")


def _clean(v):
    if v is None or (isinstance(v, float) and pd.isna(v)): return None
    s = str(v).strip()
    return None if s.lower() in {"", "undefined", "null", "n/a", "na", "nan", "none", "-"} else s


def _split_ics(value):
    v = _clean(value)
    return [p.strip() for p in re.split(r"[;,/]", v) if p.strip()] if v else []


def _ev(value, as_of):
    v = _clean(value)
    return {"value": v, "source": "BMIC-list", "tier": "attestation", "as_of": as_of} if v else None


def _ev_bool(value, as_of):
    if value is None or (isinstance(value, float) and pd.isna(value)): return None
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"true", "yes", "1"}: value = True
        elif s in {"false", "no", "0"}: value = False
        else: return None
    return {"value": bool(value), "source": "BMIC-list", "tier": "attestation", "as_of": as_of}


def guard_cards_dir(reset: bool, overwrite: bool):
    """Validate the requested materialization mode without mutating cards/."""
    existing = sorted(f for f in os.listdir(CARDS) if f.endswith((".yaml", ".yml")))
    if not existing:
        return existing
    if reset:
        return existing
    if overwrite:
        r_ids = [f for f in existing if f.startswith("R")]
        b_ids = [f for f in existing if f.startswith("B")]
        print(f"  ⚠ --overwrite into NON-EMPTY cards/ ({len(r_ids)} R, {len(b_ids)} B).")
        print("    If a prior reconcile renamed B→R, overwrite can duplicate repositories.")
        print("    Safer: --reset for a clean rebuild. Proceeding only because --overwrite was explicit.")
        return existing
    raise SystemExit(
        f"⛔ cards/ already has {len(existing)} cards. Use --reset for a clean rebuild "
        f"(archives first), or --overwrite to rewrite in place. Refusing to add duplicates.")


def load_crosswalk():
    p = os.path.join(INP, "crosswalk.csv")
    if not os.path.exists(p): return None
    cw = pd.read_csv(p); cw.columns = [c.strip() for c in cw.columns]
    return cw


def bmic_to_uid_map(bmic, namecol, cw):
    idx2uid, cw_by_slug = {}, {}
    if cw is not None and "registry_name" in cw.columns and "repo_uid" in cw.columns:
        for _, r in cw.iterrows():
            if pd.notna(r.get("registry_name")):
                cw_by_slug[slug(strip_acronym(r["registry_name"])[0])] = str(r["repo_uid"])
    for i, r in bmic.iterrows():
        base, _ = strip_acronym(r[namecol])
        s = slug(base)
        if s in cw_by_slug:
            idx2uid[i] = cw_by_slug[s]
        elif "id" in bmic.columns and pd.notna(r.get("id")):
            idx2uid[i] = f"B{int(r['id'])}"
        else:
            idx2uid[i] = f"B{i}"
    return idx2uid


def build_identity(r, namecol):
    name = _clean(r[namecol]) if namecol in r else None
    base, acro = strip_acronym(name or "")
    ident = {"name": name}
    if acro: ident["abbreviation"] = acro
    if acro and base and base != name: ident["full_name"] = base
    hp = _clean(r.get("Repository_Url")) or _clean(r.get("Repository_URL"))
    if hp: ident["homepage"] = hp
    desc = _clean(r.get("Description"))
    if desc: ident["description"] = desc
    ident["status"] = {"value": "active", "source": "BMIC-list",
                       "tier": "attestation", "as_of": NOW_YM}
    return ident


def build_links(r, uid):
    links = []
    for ic in _split_ics(r.get("ICO")):
        links.append({"id_type": "nih_ic", "id_value": ic, "url": None, "tier": "attestation"})
    for col, idt in [("Data_Access_Url", "data_access_url"),
                     ("Data_Submission_Url", "data_submission_url")]:
        u = _clean(r.get(col))
        if u:
            links.append({"id_type": idt, "id_value": u, "url": u, "tier": "attestation"})
    links.append({"id_type": "bmic_row", "id_value": uid, "url": None, "tier": "attestation"})
    return links


def build_bmic_block(r, as_of):
    blk = {"_meta": {"block_version": "1.0", "source": "BMIC-list",
                     "tier": "attestation", "as_of": as_of, "review_state": "auto"}}
    fields = {
        "subject_area": _ev(r.get("Subject_Area"), as_of),
        "model_system": _ev(r.get("Model_System"), as_of),
        "access_type":  _ev(r.get("Access_Type"), as_of),
        "data_access":  _ev(r.get("Data_Access"), as_of),
        "nih_funding_support":        _ev_bool(r.get("NIH_Funding_Support"), as_of),
        "open_data_submission":       _ev_bool(r.get("Open_Data_Submission"), as_of),
        "open_timeframe_for_deposit": _ev_bool(r.get("Open_time_frame_for_data_deposit"), as_of),
        "sustained_support":          _ev_bool(r.get("Sustained_support"), as_of),
    }
    for k, v in fields.items():
        if v is not None:
            blk[k] = v
    return blk


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bmic", default="BMIC_download.csv")
    ap.add_argument("--attach", default="interoperability")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    bmic_path = os.path.join(INP, args.bmic)
    bmic = pd.read_csv(bmic_path)
    bmic.columns = [c.strip() for c in bmic.columns]
    namecol = "Repository_Name" if "Repository_Name" in bmic.columns else bmic.columns[2]
    cw = load_crosswalk()
    idx2uid = bmic_to_uid_map(bmic, namecol, cw)

    attach = [b.strip() for b in args.attach.split(",") if b.strip()]
    block_maps = {}
    for block_name in attach:
        path = os.path.join(LATEST, block_name, f"{block_name}_blocks.yaml")
        if not os.path.exists(path):
            path = os.path.join(LATEST, block_name, "interoperability_blocks.yaml")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                block_maps[block_name] = yaml.safe_load(handle) or {}
        else:
            print(f"  (no built block found for '{block_name}' — skipping attach)")

    created = skipped = attached = 0
    applied = 0
    archive_path = ""
    cards = {}
    ok = False

    # Materialization is itself a writing step, so the lock covers preflight,
    # optional reset, all card writes, and the post-write sanity gate.
    with card_transaction("materialize_cards", auto_commit=False) as transaction:
        existing_files = guard_cards_dir(args.reset, args.overwrite)
        pending = {}
        for index, row in bmic.iterrows():
            uid = idx2uid[index]
            if load_card(uid) and not (args.overwrite or args.reset):
                skipped += 1
                continue
            card = {
                "schema_version": "1.0",
                "repo_uid": uid,
                "kind": "repository",
                "identity": build_identity(row, namecol),
                "links": build_links(row, uid),
                "blocks": {"bmic": build_bmic_block(row, NOW_YM)},
                "curation": {
                    "review_state": "needs_review",
                    "last_reviewed": None,
                    "reviewed_by": None,
                    "confidence": 0.4,
                },
            }
            for block_name, block_map in block_maps.items():
                if uid in block_map:
                    block = block_map[uid]
                    card["blocks"][block_name] = (
                        block.get(block_name, block) if isinstance(block, dict) else block
                    )
                    attached += 1
            pending[uid] = card
            created += 1

        if args.reset and existing_files:
            archive_path = snapshot_cards_zip(label="pre_materialize_reset")
            for filename in existing_files:
                path = os.path.join(CARDS, filename)
                transaction.delete(path, operation="materialize_reset_delete")
            print(
                f"  --reset: archived {len(existing_files)} cards → {archive_path}; "
                "cleared top-level cards inside the transaction"
            )

        for uid, card in pending.items():
            save_card(uid, card)
            applied += 1

        cards = all_cards()
        no_undef = not any(
            "undefined" in json.dumps(card.get("blocks", {}).get("bmic", {})).lower()
            for card in cards.values()
        )
        checks = {
            "cards created or skipped": (
                created > 0 or skipped > 0,
                f"{created} prepared, {skipped} skipped",
            ),
            "uids unique on disk": (
                len(cards) == len({card.get('repo_uid') for card in cards.values()}),
                f"{len(cards)} cards",
            ),
            "every card has identity": (
                all("identity" in card for card in cards.values()),
                "ok",
            ),
            "no undefined in bmic block": (no_undef, "clean"),
        }
        ok = gate(checks)
        if ok:
            transaction.commit()
        else:
            transaction.rollback(error="materialize_cards sanity gate failed")
            applied = 0
            cards = all_cards()
            print("\n  ⛔ sanity gate failed — all materialization writes were rolled back.")

    run_dir = new_run_dir("materialize_cards")
    pd.DataFrame(
        [
            {
                "repo_uid": uid,
                "name": card.get("identity", {}).get("name"),
                "abbreviation": card.get("identity", {}).get("abbreviation"),
                "has_interop": "interoperability" in card.get("blocks", {}),
            }
            for uid, card in cards.items()
        ]
    ).to_csv(os.path.join(run_dir, "cards_index.csv"), index=False)

    ledger_append(
        "materialize_cards",
        params={
            "bmic": args.bmic,
            "attach": attach,
            "reset": args.reset,
            "overwrite": args.overwrite,
        },
        inputs={
            "bmic": bmic_path,
            "crosswalk": os.path.join(INP, "crosswalk.csv"),
        },
        outputs={
            "prepared": created,
            "applied": applied,
            "skipped": skipped,
            "blocks_attached": attached,
            "total_cards": len(cards),
            "pre_reset_archive": archive_path,
        },
        gate_passed=ok,
        run_dir=run_dir,
    )
    print(
        f"\n✓ {applied} written, {attached} blocks attached, "
        f"{len(cards)} total → {os.path.dirname(run_dir)}"
    )


if __name__ == "__main__":
    main()
