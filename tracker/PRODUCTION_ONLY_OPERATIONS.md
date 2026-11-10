# NIH Tracker v19.0.2 production-only operations

## Retained roots

| Path | Purpose |
|---|---|
| `/content/drive/MyDrive/NIH_Tracker` | Authoritative cards, inputs, reviewed decisions, builds, runs, notebooks, and scripts |
| `/content/drive/MyDrive/NIH_Tracker_shared` | Shared re3data and resource-profile caches |

Development and staging roots are retired. The v19 notebooks do not expose environment selection or local tier promotion.

## Install or refresh release code

Use the first block of `nih_tracker_colab_v19_maintenance.ipynb`. Dry run is the default. Apply requires:

```text
INSTALL_PRODUCTION_RELEASE
```

The production installer publishes v19 notebooks and release-managed scripts, tests, templates, references, schemas, and documentation. It does not inspect or stage card/input data.

## Review workflow

```text
Review pending candidates -> Approve imported recommendations -> Finalize -> Pipeline
```

- Card/name queues remain manual.
- Identifier, RRID, FAIRsharing, and crosswalk queues may use sealed web-review batches.
- Imported recommendations require named human approval.
- Finalization is disabled while imported items await approval.
- Family-specific scripts remain available for forensic use.

## Pipeline

The default is cached/offline. Check **Refresh external API/web evidence** only for a deliberate live-evidence run. The v19 catalog compiler remains offline regardless; it compiles the evidence already present in the reviewed project.

The final pipeline stages are:

```text
build_spine
build_catalog
build_frontend
publish plan or publish apply
```

## Public payload

The GitHub Pages publication includes the task-oriented HTML, versioned static data exports, schemas, and file-level manifests. The public catalog is a derived release artifact, not a replacement for the card store.
