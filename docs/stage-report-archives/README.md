# stage-report-archives

This directory stores completed major-version stage-report sets.

When a phase such as `v0.2.x` is closed, all of its `v0.<minor>.*` stage reports move out of active `docs/stage-reports/` and into a versioned archive directory here.

Archived sets:

| Archive | Range | Count | Phase closeout |
| --- | --- | ---: | --- |
| `v0.2.x/` | `v0.2.1` through `v0.2.144` | 144 | `docs/phase-reports/v0.2.0_experiment_productization_closeout.md` |
| `v0.3.x/` | `v0.3.0` through `v0.3.56` | 57 | `docs/phase-reports/v0.3.0_product_usability_buffer_closeout.md` |

The newest archived handoff can still be a valid next-task source when the next phase has not yet created its first active stage report.
