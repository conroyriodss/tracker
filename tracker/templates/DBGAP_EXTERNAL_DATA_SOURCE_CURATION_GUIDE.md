# dbGaP external-data-source curation

Use `inputs/dbgap_external_data_sources.csv` only for an explicit, authoritative assertion about a resource's **specific dbGaP External Data Source (EDS) registration or current status**.

Required columns:

- `repo_uid`
- `status`: `registered_eds`, `former_eds`, or `not_current`
- `listed_name`
- `source_url`
- `source_as_of`
- `uses_dbgap_access_system`: `true`, `false`, or blank when not established
- `note`

The automated NIH CADR snapshot supplies a separate status, `considered_eds_via_cadr_definition`, for non-dbGaP repositories on the official NIH CADR list. That status establishes EDS **class membership under the dbGaP definition**, but does not assert that a particular dbGaP study currently uses the repository.

Do not infer specific EDS registration from a repository name occurring in a dbGaP study title, description, or collection. The public study export is used only to record source-bound textual study associations. dbGaP itself is an NIH CADR but is marked `not_applicable_dbgap_repository`, because the submission guide defines an EDS as a non-dbGaP entity.
