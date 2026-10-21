# Operational scripts

| Script | Purpose | Status |
|---|---|---|
| `pull_databases.sh` | rsync derived DB snapshots from m1 to `web/data/` | ready |
| `build_catalog.py` | OSTI JSON API → catalog.sqlite | **stub — port from osti-corpus-fetch** |
| `fetch_osti_corpus.py` | PURL + discovery → PDF corpus | **stub — port from osti-corpus-fetch** |

The catalog/corpus-fetch logic lives in the existing `osti-corpus-fetch` tooling on m1
(`~/code/osti-replication-candidates/` + SG-1-8TB scripts). These stubs document the
interface; port the production fetchers in when consolidating.
