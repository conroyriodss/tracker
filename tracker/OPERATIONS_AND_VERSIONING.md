# NIH Tracker v19.2.3: visible progress and consistent notebook execution

This release adds explicit review-source pins, conservative cleanup with verified
recovery archives, source and input SHA-256 provenance, and two offline reports.
It preserves human approval as the authority for operational decisions.

The v19.2.1 patch reports an already-absent card lock as a successful no-op,
retaining `removed=false` and an explicit `already_unlocked` audit outcome.
Cleanup now lists saved sealed plans and binds inspection to their SHA-256.
An empty selection never starts cleanup. The earlier FAIL ledger entry remains
historical; rerunning the corrected command records the corrected outcome.

## First use in Colab

1. Upload `nih_tracker_v19_2_3_bundle.zip` to `/content` and open the accompanying
   `nih_tracker_colab_v19_maintenance.ipynb` in Colab.
2. In section 1, leave `APPLY_PREFILL=False` to inspect the installation plan.
   Preview does not install code. Review the plan, set `APPLY_PREFILL=True`, and
   rerun section 1 to install it. Wait for **INSTALLATION COMPLETE**. The ZIP
   inventory is verified before installation. Restart the Colab runtime after
   installing, then run setup; do not repeat installation after restarting.
   Setup requires version 19.2.3 and verifies nine critical installed file hashes.
   A newer notebook cannot be used with older or incompletely installed scripts.
3. Start the production maintenance runtime. Stop other notebooks and background
   writers before inventory or cleanup; the maintenance/card locks do not lock
   every possible evidence collector or external Drive writer.
4. Section 3a defaults to **Inspect existing pins**. This validates only the
   files named in the pin manifest. It does not scan historical queues or report
   current pending counts. **Full inventory (slow audit)** is optional; it reads
   project history, snapshots and prior reports, and can take many minutes on
   mounted Drive. Open its `index.html` and review the selected source hashes.
   JSON and CSV retain the observations. Existing project records are not modified.
5. If those queues are the intended review sources, choose **Pin current review
   sources** and acknowledge that configuration change. This writes
   `inputs/active_review_sources.json` and a selection audit record. It neither
   approves candidates nor applies decisions. Existing pins are never silently
   replaced. If pins already exist, use **Inspect existing pins** to validate and
   display the current selection without rewriting it. Missing or changed pinned bytes stop review instead of substituting
   a different run.
6. Use the review workbench for the remaining human decisions. Before deliberately
   selecting new run outputs, inspect the intended queues and hashes, then use
   **Unpin review sources** and pin the reviewed selection. Unpinning retains the
   old selection in the audit. A pipeline refresh never replaces pins silently.
7. For cleanup, run section 6 and select a saved plan, or click **Create plan**.
   Click **Inspect selected plan** and review its protected and eligible paths.
   Check both acknowledgments to enable **Apply reviewed plan**. The selection
   is initially empty and changes invalidate prior review. All other writers must be
   stopped. Plan and apply both check retention; every eligible file is archived
   and re-read for SHA-256 verification before any source is removed.

Current selection without pins retains the prior modification-time rule and
adds a deterministic full-path tie breaker. This fixes unstable tie ordering;
it does not assert that the newest timestamp is the scientifically authoritative
source. Pins express that operational choice explicitly. Source row counts,
stable review keys, adjudications and model approval rules are unchanged.

## Progress, interruption and Drive diagnostics

Full inventory reports its phase, current path, observed file/byte counts and
elapsed time every ten seconds. The counter increases only for work observed;
there is no inferred completion percentage or finish time. A heartbeat with
unchanged counts can indicate a blocked Drive read, not productive progress.

The shared runner provides a heartbeat when a subprocess is silent, writes the
live log on the Colab VM, and copies the completed log and result to Drive once.
If that copy or ledger verification fails, the result does not pass; the displayed
local paths retain diagnostics until the runtime is deleted. Installer logs also
use local storage before a final Drive copy. No automatic Drive remount or retry
of a potentially mutating command occurs.

Stopping a runtime-managed cell requests termination of its subprocess group and
records an interrupted result. The result's `termination_confirmed` field records
whether its leader exited; an unconfirmed termination blocks safe restart of a
writer. This does not claim that every interrupted write rolled back.
Inspect transaction snapshots, locks and step receipts before retrying a mutating
operation. The pipeline records stopped status and creates no success marker.

Use the updated pipeline notebook after the matching release installation.
Start with **Validate/propose (no card writes)** and external refresh disabled.
The pipeline prints each step's position, retains its existing gate checks, and
requires a successful complete run plus a separately reviewed publication plan
before publication. Review decisions, source selection and cleanup remain in
their respective notebooks.

## What cleanup retains

- Structured run outputs, captured structured evidence, review queues, plans and
  audit artifacts, regardless of age/count retention settings.
- Runs referenced by the ledger, inputs, cards, blocks, current build, reference
  files, or `latest/`, where a supported text reference is detected.
- Every card transaction snapshot, current pointers and active debug directory.
- All recovery ZIPs under `snapshots/maintenance/`.

Old standalone logs and abandoned temporary files may be eligible. A small or
empty cleanup plan is expected when the ledger references most historical runs.
The operation reduces clutter; archives remain on Drive, so no storage saving is
promised. Identical file hashes are reported only as duplicate-byte observations;
they do not establish that runs or their provenance are interchangeable.

Reference detection is deliberately conservative, not a universal dependency
parser. Unknown formats can contain additional references. Verified archives
provide a recovery layer for eligible removals. Scanning errors stop cleanup.
Concurrent uncooperative writers remain a limitation; do not run cleanup while
another notebook, Drive synchronization task or collector is changing files.

## Recovery

The cleanup receipt records the ZIP path and SHA-256. The ZIP contains original
project-relative paths, empty directories and `CLEANUP_ARCHIVE_MANIFEST.json`
with each file's hash and size. Verify the ZIP hash, extract into a separate
inspection folder, and verify the member hashes before selectively restoring.
Do not extract over a live project or overwrite newer files automatically.
These are byte-content recovery archives, not backups of POSIX ownership,
permissions, extended attributes or Google Drive revision history.

## Installation protects source evidence

Code refresh now preserves existing reference snapshots and curated mappings,
including dbGaP, CADR, publisher/status sources and confirmed crosswalks. Missing
distribution snapshots can still be installed. Versioned catalog vocabularies,
the capability rubric and reference documentation remain code-owned. Use the
existing explicit reference-refresh controls for intentional evidence updates.
Recreate old installation plans: their overwrite policies differ from v19.2.

## GitHub layout and reproducibility

The existing repository keeps public site output under `docs/`. The source
distribution lives under `tracker/`, with an independent test workflow under
`.github/workflows/`. Source changes are reviewed through pull requests; public
catalog publication continues through the existing sealed publication plan.

Keep the Git checkout separate from `/content/drive/MyDrive/NIH_Tracker`. Version
scripts, notebooks with empty outputs, schemas, templates, tests, documentation
and explicitly distributed reference snapshots. Exclude operational cards,
inputs, raw model exchanges, runs, caches, credentials, recovery archives and
builds. `.gitignore` is an aid, not a substitute for reviewing the staged diff.
There is no automatic commit, push, merge, site publish, or recurring job in the
maintenance notebook.

Every newly logged step records full input SHA-256 hashes, a source-tree SHA-256,
resolved dependency versions and a content-addressed code-provenance receipt.
Directory input hashes cover a sorted map of relative file names to file hashes;
as with the legacy SHA-1 helper, `.lock` and `.tmp` members are excluded. A Git
commit and dirty flag are recorded only when the executing source is actually
tracked in a checkout. ZIP-installed code reports a null Git commit and its
observed source fingerprint. Historical runs are not relabeled retroactively.
These hashes identify bytes; they do not validate the scientific accuracy of
upstream claims or make a changing filesystem an atomic snapshot.

The initial v19.1.1 distribution is preserved in a separate source-import commit.
`UPSTREAM_SOURCE.json` records the supplied archive's SHA-256 and verified file
inventory. Subsequent implementation changes are a separate commit. The original
validation report is retained in `reference/release_history/` and is historical.

## Visualization

**Operations:** `scripts/step_project_inventory.py inventory` produces the internal
queue/provenance/retention report. It includes project paths and should not be
published with the public catalog.

**Catalog coverage:** the independent report builder verifies the coverage CSV
hash against its catalog manifest and checks the denominator and unique IDs:

```bash
python scripts/step_build_coverage_report.py \
  --catalog /content/drive/MyDrive/NIH_Tracker/build/catalog \
  --out /content/NIH_Tracker_coverage.html
```

The coverage view reports existing documentation flags, with field selection,
recorded/not-recorded filtering and resource search. A recorded negative finding
can still count as documented. Zero does not mean the capability is absent.
Resource-type applicability and the cause of missing coverage cannot be derived
from this CSV alone. Do not turn these indicators into an overall FAIRness score.
The separately delivered example uses the published September 29, 2026 catalog
at Git commit `f553d6df220db4f84aac2533520234dedf57f386`, containing 222 resources;
it is separate from the November 10 operational status log.

## Validation boundary

See `VALIDATION_REPORT.txt` for the executed checks and remaining environment
limits. Tests use isolated synthetic projects. No production Drive cleanup,
candidate adjudication, live cloud call or new catalog publication is claimed.
