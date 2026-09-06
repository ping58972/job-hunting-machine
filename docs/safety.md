# Safety boundaries

Application documents are generated locally. `PathGuard` confines master templates, generated
TEX/PDF files, compiler build directories, and metadata to the fixed project root. Inserted plain
text is LaTeX-escaped, and only current VERIFIED facts may become generated claims. Normal workers
cannot modify master templates, produce DOCX/GDOC files, or call Google Docs.

`LatexCompiler` is the only LaTeX process boundary. It passes an argument array with `shell=False`,
uses a timeout, captures bounded diagnostics, and writes builds under `data/latex-build`. It never
installs software or falls back to a cloud service. The local TeX executable and master templates
are trusted operator-controlled inputs.

Form Agent selects the exact application-associated PDF. Submission remains limited to the
dedicated Submission Agent after LIVE mode, authorized approval, matching current review hash,
correct state, and idempotency checks. No document-generation command submits an application or
sends email, Slack, or LinkedIn messages. DRY_RUN remains the default.
