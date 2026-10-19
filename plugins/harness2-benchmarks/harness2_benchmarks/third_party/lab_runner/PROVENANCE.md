# harvey-labs runner assets

These runners derive from the harvey-labs integration and retain its
[upstream license](LICENSE). See the [pinned revision and patches](../../dependencies_meta/PINNED.md).

The integration adds editable harness installation and configurable models,
paths, and endpoints. Local model-provider names, environment variables, and
log paths use the `harness2` namespace. Setup installs these runners into the LAB checkout as overlays.

[ASSETS.json](../../dependencies_meta/ASSETS.json) records the shipped file checksums.
Verify them with `python plugins/harness2-benchmarks/scripts/check_provenance.py`.
