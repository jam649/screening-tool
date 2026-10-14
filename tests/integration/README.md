# Packaged application integration checks

These are maintained CI tests of the actual production image and persistent services. They complement the backend unit/API suite and the frontend browser suite; public inputs remain under `fixtures/`. Credentials, databases, captures and generated reports are local runtime artifacts.

| Check | Purpose |
| --- | --- |
| `monitoring.py`, `monitoring_restart.py` | Scheduled operations, SMTP, reports, exports, protected retention and retained restart |
| `qbo.py`, `qbo-compose.yml` | QuickBooks OAuth/rotation/CDC/webhook contracts and native controls |
| `bc.py`, `bc-compose.yml` | Business Central company/subscription/poll/cleanup contracts and native controls |
| `models.py`, `models-compose.yml` | Optional model consent, probability rails, fallback, human training pairs and private export evidence |
| `matching.py` | Frozen independent recall and actual migrated cold/repeat large-book performance |
| `deployment.py`, `deployment-compose.yml` | Host/proxy trust, runtime notices, key binding and private operator exports |

The [CI workflow](../../.github/workflows/ci.yml) declares isolated fresh deployments, explicit opt-in fixture modes, resource limits, restart checks and cleanup of CI-owned data. QuickBooks/Business Central fixture tests do not establish live sandbox readiness. Synthetic model transports are enabled only by their test Compose overrides; production starts the normal application. Actual same-host Laya and the separate live OpenRouter comparison are recorded in research notes.

Earlier milestone records used local checkpoint filenames under `scripts/gates/`. Five Python harnesses are byte-identical to their earlier checkpoint copies; the BC and model harnesses change only three sibling imports to the new feature names and sort those import blocks. Compose, CI and test references use this feature-based layout. Those historical local originals remain ignored for existing retained test deployments. Source tests now use this directory; no meaningful check or frozen input was removed.
