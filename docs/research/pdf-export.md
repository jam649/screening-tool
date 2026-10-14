# PDF audit export research

Verified 2026-10-13. This is preparation only: milestone 5 application implementation remains gated on the real Reviewer milestone 4 browser pass. No application code, migration, production dependencies, lockfiles or fixture contract changed for this investigation.

## Recommendation

Use unmodified `fpdf2==2.8.9`, `uharfbuzz==0.56.3`, and `fonttools==4.66.1`, with packaged OFL Noto fonts. The isolated Python 3.12 prototype installed all from public PyPI. fpdf2 offers embedded font subsets, fallback fonts and HarfBuzz shaping with built-in bidi handling. [Unicode/fonts documentation](https://py-pdf.github.io/fpdf2/Unicode.html), [shaping documentation](https://py-pdf.github.io/fpdf2/TextShaping.html).

Keep application fonts in a package-owned asset directory, explicitly include them in wheels and the Compose image, and load only those files. No runtime external font downloads or system-font discovery. Direct `multi_cell` text is literal text; do not route untrusted customer fields through HTML/Markdown interpretation. Use the same typed evidence document for PDF, CSV, embedded JSON and ZIP outputs.

The observed safe shaping configuration is `set_text_shaping(True, features={"liga": False, "rlig": False, "dlig": False})`. The Latin base font falls back to Arabic, Devanagari and CJK regular fonts. This retains joined Arabic and Hindi conjunct rendering in the tested sample while avoiding an Arabic lam/alef ligature extraction error. It is a measured configuration for the pinned stack, not a claim that every script or font has been validated.

Attach the exact sanitized evidence document as UTF-8 `evidence.json` using the public `embed_file(bytes=..., basename="evidence.json", mime_type="application/json", compress=True)` API. This ensures audit originals survive text-extractor ordering differences and missing glyphs. Include an ordinary visible sentence explaining the attachment, with the same schema/hash policy as CSV. Browser PDF viewers may not expose attachments; CSV remains an explicit downloadable alternative. [Attachment API](https://py-pdf.github.io/fpdf2/FileAttachments.html), [API reference](https://py-pdf.github.io/fpdf2/fpdf/fpdf.html).

## Observed rendering and extraction

Synthetic-only prototypes live in ignored `.workspace/export-research/`; the venv, raw fonts, comparison scripts and all dependency metadata stay there. The review copy is `~/Desktop/artifacts/screening-tool/pdf-export-research/unicode-recommended.pdf`; it contains no application/customer records. `final-prototype.py` and `final-checks.json` retain the reproducible assertions.

The final three-page prototype is 39,460 bytes, including its compressed exact-original JSON attachment. It includes long unbroken strings, multi-script wrapping, page footers, quoted/XML-like text, unsupported supplementary-plane characters, and these names:

- Latin: `José François — München`
- Greek: `Αλέξανδρος Παπαδόπουλος`
- Cyrillic: `Александр Иванов`
- Arabic: `شركة السلام للتجارة`, mixed with `ABC 123`
- CJK: `北京贸易有限公司 東京株式会社 한국무역`
- Hindi: `नमस्ते भारत व्यापार कंपनी`, `श्री लक्ष्मी इंटरनेशनल`
- Mixed: `José 東京 شركة السلام नमस्ते`

Actual PNG renders were inspected; the tested Arabic is joined with appropriate visual word order, Devanagari conjuncts render, and CJK glyphs are regular weight. `pdftotext` default layout output preserves the exact Latin, Greek, Cyrillic, Arabic, CJK and Hindi sample text after excluding extractor-generated bidi controls for comparisons. Mixed-direction spans preserve their characters, but the extracted whole-string ordering can differ from source. `pypdf==6.20.1` retains characters but orders Arabic words differently and sometimes introduces spacing around Hindi combining marks. Verify embedded attachment equality byte-for-byte with `PdfReader(...).attachments["evidence.json"][0]` instead of treating one extractor's output as the audit authority. No claim of universal native-language typographic review is made.

A naive logical `/ActualText` wrapper was tested and rejected: Poppler double-reverses the Arabic content, and pypdf does not use that wrapper to repair text extraction. Do not add it as an assumed fix.

Preflight every output character against the union of packaged cmap coverage. The sample missing characters `😀`, `𒀀`, and `𠀀` render as visible `[U+1F600]`, `[U+12000]`, `[U+20000]` with a missing-glyph explanation. Original strings remain exact in the attachment/CSV. Do not silently use `?`, drop glyphs, transliterate originals, or disable warnings as a substitute for validation. Handle intentional zero-width joiners and bidi controls as shaping inputs with explicit preservation, rather than rejecting legitimate script sequences merely because controls have no glyph.

## Why not ReportLab for this release

`reportlab==5.0.1` is BSD licensed and embeds Unicode TTF subsets. [PyPI](https://pypi.org/project/reportlab/), [official font guide](https://docs.reportlab.com/reportlab/userguide/ch3_fonts/).

The experiment found two shipping failures. `reportlab[bidi,shaping]==5.0.1` cannot resolve from public PyPI because `rlbidi` is absent. Installing its public instructions' official index URL returned HTTP 401 for `rlbidi-0.1.1`. ReportLab's [RTL documentation](https://docs.reportlab.com/rl-arabic/) confirms that package is separately hosted and describes the shaping API as experimental.

Shaping-only PDF renders Hindi but maps shaped glyphs to private-use code points during extraction; Arabic word order is wrong without bidi, and a CJK `株式会社` sample extracts as `㍿`. Without shaping, Hindi/Arabic appearance is inadequate. The generated comparison PDFs are approximately 49–50 KB. Avoid private-registry dependencies and hand-rolled bidi adapters for this milestone.

## Asset provenance and licenses

The three static non-CJK fonts below are unmodified from the official archived Noto repository at commit `ffebf8c1ee449e544955a7e813c54f9b73848eac`; this is an explicit stable artifact choice, not a claim of newest per-script release. CJK is the official `Sans2.004` tag at commit `523d033d6cb47f4a80c58a35753646f5c3608a78`. [Noto repository](https://github.com/notofonts/noto-fonts), [CJK deployment guide](https://github.com/notofonts/noto-cjk/blob/main/Sans/README.md?plain=1).

| Asset | Bytes | SHA-256 | Official pinned URL |
| --- | ---: | --- | --- |
| NotoSans-Regular.ttf | 569208 | `b85c38ecea8a7cfb39c24e395a4007474fa5a4fc864f6ee33309eb4948d232d5` | [source](https://raw.githubusercontent.com/notofonts/noto-fonts/ffebf8c1ee449e544955a7e813c54f9b73848eac/hinted/ttf/NotoSans/NotoSans-Regular.ttf) |
| NotoSansArabic-Regular.ttf | 240456 | `ceea25b464a656dc3b26849bab9356740401af62aedf1bfa8b7f0d9b75925b1b` | [source](https://raw.githubusercontent.com/notofonts/noto-fonts/ffebf8c1ee449e544955a7e813c54f9b73848eac/hinted/ttf/NotoSansArabic/NotoSansArabic-Regular.ttf) |
| NotoSansDevanagari-Regular.ttf | 219212 | `385e78e6359a9d88a0f243d53b1209d7548361ba2194e2b9ec779bcaa7e8949d` | [source](https://raw.githubusercontent.com/notofonts/noto-fonts/ffebf8c1ee449e544955a7e813c54f9b73848eac/hinted/ttf/NotoSansDevanagari/NotoSansDevanagari-Regular.ttf) |
| NotoSansCJKsc-VF.ttf | 36144788 | `990c807e79c25662a5a9ecf7f971baeb2bf2eab9a559e5ecf15cdfdb8561d21f` | [source](https://raw.githubusercontent.com/notofonts/noto-cjk/523d033d6cb47f4a80c58a35753646f5c3608a78/Sans/Variable/TTF/NotoSansCJKsc-VF.ttf) |
| OFL-noto-fonts.txt | 4377 | `0dab92d0544f7b233403f14b84a663bdbfa746982eda629e7f4f9ffe1b036feb` | [source](https://raw.githubusercontent.com/notofonts/noto-fonts/ffebf8c1ee449e544955a7e813c54f9b73848eac/LICENSE) |
| OFL-noto-cjk.txt | 4301 | `6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2` | [source](https://raw.githubusercontent.com/notofonts/noto-cjk/523d033d6cb47f4a80c58a35753646f5c3608a78/LICENSE) |

The CJK variable source is 36,144,788 bytes and defaults to weight 100. Instantiate `wght=400` using pinned fontTools at build preparation, preserve source `head.created/modified` with `recalcTimestamp=False`, and rename family/PostScript/name IDs to `Screening CJK` / `ScreeningCJK-Regular`. The static derivative is 21,659,476 bytes; SHA-256 `4b37980a3dffafea33cddd16e6732a29411f4916419dae38813230a6b2128730`. The research build script is `instantiate-cjk.py`. Package the static artifact once; each PDF embeds only its used subset. Preserve font copyright/name records and OFL text. [fontTools instancer](https://fonttools.readthedocs.io/en/latest/varLib/instancer.html), [OFL FAQ](https://openfontlicense.org/ofl-faq/).

fpdf2 is LGPL-3.0-only. Use the unmodified importable library, include LGPL/GPL notices and pinned source links in third-party notices, and retain normal library replacement/reinstallation capability in the image. fontTools is MIT, uharfbuzz is Apache-2.0, and Pillow is MIT-CMU according to installed metadata. Keep all bundled font licenses separate from the application MIT license. [fpdf2 release metadata](https://pypi.org/project/fpdf2/2.8.9/).

Pinned public dependency artifact checksums (sdists shown where platform-specific wheels vary):

| Package | Artifact | SHA-256 |
| --- | --- | --- |
| fpdf2 2.8.9 | [fpdf2-2.8.9-py3-none-any.whl](https://files.pythonhosted.org/packages/41/16/42cc18bba1561692a235fd232b38947e54f059150065d43d631b57a0085a/fpdf2-2.8.9-py3-none-any.whl) | `6e1d94af6d6311950a23dec7fb5fc84b000203eb59aee8e76c1e701b12a14976` |
| uharfbuzz 0.56.3 | [uharfbuzz-0.56.3.tar.gz](https://files.pythonhosted.org/packages/04/55/0b4e05cfb5134e8902c56e9a0d2d629c5de4b89806a0b698f422ec06bd55/uharfbuzz-0.56.3.tar.gz) | `dbb6cc2c36b42929e4059290a980640f2391d858f6eab36e369ed4f373f96caa` |
| fonttools 4.66.1 | [fonttools-4.66.1-py3-none-any.whl](https://files.pythonhosted.org/packages/f6/10/d45b74135d5d642cb3a4fb0a957c1613ef93de4c8548671dfc3a5bf38299/fonttools-4.66.1-py3-none-any.whl) | `7234ae9e28db64273fbbfa72caebd0a97e3bdba6b05064114741b9539ef339d0` |
| pillow 12.3.0 | [pillow-12.3.0.tar.gz](https://files.pythonhosted.org/packages/1c/3d/bb7fca845737cf9d7dbde16ed1843984665ff2e0a518f5db43e77ec540b9/pillow-12.3.0.tar.gz) | `3b8182a766685eaa002637e28b4ec8d6b18819a0c71f579bf0dbaa5830297cce` |


## Application evidence and ownership handoff

Suggested owner files after the milestone 4 gate: `backend/app/reports/export.py` for evidence selection and serializers; a focused package font helper if necessary; `backend/app/api/exports.py` for authenticated attachment responses; `backend/tests/test_exports.py` for historical evidence, privacy, CSV/ZIP safety and PDF Unicode. Coordinator owns dependency locks, asset packaging, router/CLI registration, shared audit hooks and migrations. Matching owner supplies immutable run-party evidence hooks. No concurrent edits to current code writers' shared files.

Current party/source fields belong in a visibly labeled **Current record as of export** header. Historical sections use each hit's immutable `party_snapshot` and `entry_snapshot`, original disposition/audit actor identity, explanation, exact hashes, frozen settings/model identity, timestamps and full stored scope. Never replace those inputs with present party/listing rows or current usernames/source labels. Include suppression's real original human decision and completed-task/run/fetch/list-version provenance. A projected list API response is not a full manifest: export the actual stored `party_ids`, `party_hashes`, `entry_ids`, `entry_hashes` without changing database evidence.

New zero-hit membership should use dedicated immutable run-party evidence. Legacy zero-hit events can show real `parties.create/update` audit snapshots only when their timestamp and recorded party hash establish the exact run input. A matching present record is insufficient; a later update's `before` snapshot may help only if genuine timeline evidence proves it. Otherwise explicitly label historical fields unavailable, retaining known membership/hash/scope/timestamps. Hash-policy upgrades require the run's actual manifest authority rather than assuming old entry revisions reflect the new hash rule.

Preserve application privacy: build both visible PDF and attachment from the same redacted evidence document. Do not embed secrets, credential envelopes, invitation tokens, transport bodies or arbitrary database dumps. Record export requester, selection, format, timestamp and checksum through append-only audit, using a fixed evidence cutoff so the export's own request event does not recursively change its checksum.

The full ZIP should prefer UTF-8 party CSV/evidence files for every party, including soft-deleted records, plus complete run/audit indexes and checksum manifest. This is the proposed interpretation of “every party file,” and requires coordinator confirmation if PDFs are expected inside the ZIP. Avoid generating 20,000 CJK-font-loading PDFs as a side effect of one export. If PDFs are required, measure actual CPU/RSS/ZIP size and use a bounded private spool; the 39 KB smoke proves font subsetting, not full-book scalability.

Reuse `filter_party_rows` exactly for current-view CSV, exporting all matching rows across pagination. Neutralize spreadsheet formulas after leading whitespace/control inspection, quote fields through the CSV library, and preserve raw originals in JSON evidence where formula neutralization intentionally changes spreadsheet-facing strings. Use ID-based safe ZIP paths, attachment headers, `Cache-Control: no-store`, safe source URLs and the application's exact ITA disclaimer in each export footer. The prototype footer is explicitly synthetic and does not claim application disclaimer validation.

Remaining implementation checks: API/CLI role/CSRF behavior; exact disclaimer on every real PDF page/CSV artifact; snapshot mutation regressions; zero-hit legacy unavailable labels; before/after actor identity changes; redaction parity between visible and attached evidence; 20,000-row filter/ZIP bounds; safe Unicode unsupported-glyph labeling; wheel/image asset presence offline; Linux container extraction/rendering smoke with the pinned font/shaping stack. M5 is not complete from this research.
