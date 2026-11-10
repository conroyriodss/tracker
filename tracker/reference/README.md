# Pinned reference artifacts

This directory contains source-bound reference artifacts distributed with NIH Tracker v17.

- `re3data_confirmed_mappings.csv`: reviewed exact re3data records for NIMH Data Archive, FITBIR, and PhysioNet. Live verification is still required before direct application.
- `nih_cadr_registry.csv`: transcription of the official NIH Controlled-Access Data Repository list, including access system and NIH-designated genomic-repository status.
- `dbGaP_Studies.csv`: pinned public dbGaP study metadata. It does not contain an explicit External Data Source registration field.
- `dbgap_eds_definition.json`: structured interpretation of official dbGaP EDS guidance.
- `repository_status_overrides.csv`: reviewed resource-specific operational/intake evidence for status cases that cannot be represented safely by BMIC alone. Rows are attached only by active UID, exact name/alias, or unique exact homepage host.
- `publisher_policy_sources.csv`: authoritative publisher policy pages, expected anchor terms, response hashes, and refresh state.
- `publisher_repository_mentions.csv`: human-coded repository mentions with mandatory, recommended, preferred, fallback, example, or directory-referral posture.

Each pinned artifact has adjacent metadata where applicable. Runtime outputs record exact input hashes. Source-page update commands store content-addressed snapshots and never auto-convert changed web content into new semantic assertions.
