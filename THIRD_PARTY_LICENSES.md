# Dependency notices

The application is MIT licensed. Its dependency packages keep their original licenses and copyright notices, including the license metadata installed alongside them in the container. Dependencies are pinned in backend/uv.lock and frontend/package-lock.json.

- [text-unidecode 1.3](https://github.com/kmike/text-unidecode): distributed unchanged under its **Artistic License** option. Its full dual-license notice is retained at docs/licenses/text-unidecode-LICENSE.txt. The standard source is freely available at https://pypi.org/project/text-unidecode/1.3/ and the upstream repository. This replaces GPL-only Unidecode while retaining Unicode-to-ASCII transliteration.
- [pycountry](https://github.com/pycountry/pycountry): LGPL-2.1 library, distributed unchanged and dynamically imported. Its standard source and license are available from the upstream repository and https://pypi.org/project/pycountry/. Recipients may replace the installed library with a compatible modified version; it is not statically linked into the application.
- [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz): MIT.
- [Metaphone](https://github.com/oubiwann/metaphone): BSD-3-Clause.
- [openpyxl](https://foss.heptapod.net/openpyxl/openpyxl): MIT.
- [defusedxml](https://github.com/tiran/defusedxml): Python Software Foundation license.

Frozen CSL sample data is public U.S. government data; fixture party records are synthetic. Optional Laya is separately installed by the operator and retains its publisher's Apache-2.0 terms.

- [APScheduler 3.11.3](https://pypi.org/project/APScheduler/3.11.3/): MIT; stable 3.x scheduler with the application’s persistent JSON job store in SQLite.
- [fpdf2 2.8.9](https://pypi.org/project/fpdf2/2.8.9/): LGPL-3.0-only, distributed unchanged and dynamically imported. Pinned source is available on PyPI and https://github.com/py-pdf/fpdf2/tree/2.8.9; its full license is also retained at docs/licenses/fpdf2-LICENSE.txt; installed notices are preserved and operators may replace the importable library with a compatible modified version.
- [fontTools 4.66.1](https://pypi.org/project/fonttools/4.66.1/): MIT; [uharfbuzz 0.56.3](https://pypi.org/project/uharfbuzz/0.56.3/): Apache-2.0; Pillow: MIT-CMU. Installed dependency license notices are preserved.
- Packaged PDF fonts: SIL Open Font License 1.1. Original Noto font copyright and licenses are under backend/app/reports/fonts/. The renamed Screening CJK derivative and reproducible source hashes are documented in docs/research/pdf-export.md.

- [Tokenizers 0.23.3](https://github.com/huggingface/tokenizers): Apache-2.0; used offline with the unchanged pinned Laya multilingual tokenizer, compressed with its provenance in `backend/app/adjudicators/data/NOTICE.txt`. The checkpoint is Apache-2.0 and its mmBERT encoder/tokenizer source is MIT. The original Laya runtime license, publisher/upstream model cards and MIT permission notice are retained under `docs/licenses/`; no model weights or automatic downloads are included.

Frontend runtime packages retain their exact installed copyright and permission notices in `docs/licenses/frontend-runtime-LICENSES.txt` and the served `/third-party-notices.txt`. The reproducible generator enumerates the pinned nondevelopment dependency tree; production builds verify both copies before Vite packages the public notice. This includes React/React DOM/Scheduler, React Router, TanStack Query and their runtime dependencies. Application MIT licensing does not replace these notices.
