Validation severity audit
=========================

Reviewed on 15 November 2026: all 160 registered check types, their issue
producers, AI verdict handling, publisher fallback, and report overrides.
The table records the classification after this audit. It reviews code
behavior; it does not independently re-verify every venue's recorded rules.

Severity policy
---------------

* **Error**: a directly established violation of an explicit schema or venue
  constraint, an unusable required input, or hidden reviewer-directed prompts.
  Blocks submission under normal validation.
* **Warning**: a likely substantive problem or venue-rule violation requiring
  author review, including uncertain text/image measurements and external
  metadata. Serious findings remain warnings when the evidence is heuristic.
* **Similarity**: reserved for the opt-in outside scans (``integrity.plagiarism``
  and ``integrity.ai_text``) at or over their threshold, shown in pink. A score
  from a third-party service is a reason to look, not a verdict, so it never
  blocks and is not counted with the warnings.
* **Suggestion**: a defensible stylistic choice or optional improvement.
  Detection confidence alone does not make a preference a warning.
* **Incomplete**: the check ran, but part of it was not done because a
  registry or web service did not answer (or a lookup limit was reached):
  references not looked up, links with no answer, a linked repository's files
  not fetched. Grey, like the coverage states: not a pass, and not a finding.
  A check that also found something keeps that finding's status and is listed
  under Incomplete as well.
* **Unchecked / needs AI / not applicable**: coverage states, not violations.
  Missing tools or inconclusive network responses must not imply success.

A check can produce more than one severity. Its report status follows its
most severe finding: error, likely error, similarity, warning, then (when part
of it was not done) incomplete, suggestion, then unchecked. Mixed checks
retain each issue's own level; a table row is not a fixed level for every
possible outcome.

Corrections made
----------------

* Warning to error: unacceptable upload extensions, unsupported guideline
  formats, abstracts below a stated word minimum, and missing explicitly
  required cover-letter or separate title-page uploads.
* Warning to suggestion: accepted custom options outside recommended lists,
  optional keywords, stale cover-letter dates, older references, availability
  of a newer reference version, default figure palettes, figure font variety,
  optional acronym usage, and English-variant preferences with no venue rule.
* Kept warnings for future cover-letter dates, potential duplicate references,
  required keywords detected as missing, undefined/conflicting acronyms,
  explicit font/language rules, readability problems, and scientific concerns.
* Warning to suggestion (follow-up review, 15 November 2026): personal author
  e-mail addresses, self-citation share, inline affiliation numbers, author-year
  references in neither order with no rule, and citing an older arXiv version.
* Warning only where the venue asks (its notes, or a required reporting-guideline
  statement), otherwise suggestion: confusable figure colours, missing RRIDs,
  missing cell-line authentication or mycoplasma statements, missing study-design
  items, and code-repository licence, README, environment, and archived release.
* Publisher fallback now caps errors at warning without promoting suggestions.
* AI figure-design, raster-lettering, and trademark instructions now distinguish
  optional styling advice from a failed requirement.

Overrides and limits
--------------------

``--strict-style`` explicitly promotes suggestions to warnings.
Publisher-only rules cap violations at warning and label their source;
optional advice remains a suggestion. AI ``fail`` always becomes warning,
``suggest`` becomes suggestion, and ``unclear`` remains unchecked.

``--initial-submission`` currently downgrades **every** error to warning,
including missing files, invalid form data, and integrity errors. This is an
existing, documented global override, preserved by this audit; it is broader
than relaxing formatting rules and merits a separate policy decision.

``ValidationReport.warnings`` includes unchecked issue messages for backwards
compatibility, while report statuses distinguish unchecked from warnings.
Consumers using the flat warning list should inspect ``issue.unchecked``.
The table below describes actual findings, not those coverage messages.

All registered checks
---------------------

AI numbered families (``.*``) apply the same policy to each generated task.
Rows describing errors from venue rules are subject to publisher fallback
and the explicit initial-submission override above.

.. csv-table:: Severity by check ID
   :header: "Check ID", "Finding severity", "Reason / conditions"
   :widths: 28, 18, 54
   :class: longtable

   "ai.consistency","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.cover_letter_venue","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.details.abstract_reporting","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.details.author_details","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.details.declarations","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.details.ethics","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.details.heading_content","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.details.misplaced_content","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.details.nih_grant_numbers","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.details.supplementary","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.details.trademarks","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.figure_reproduction","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.figure_reproduction.all","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.figures.content_matches_text","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.figures.design","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.figures.raster_lettering","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.figures.raster_panels_cited","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.notes.abstract","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.notes.cover_letter","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.notes.figures","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.notes.general","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.notes.keywords","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.notes.manuscript","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.notes.references","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.notes.sections","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.notes.statements","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.notes.supplementary","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.notes.tables","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.notes.title_page","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.notes.upload","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.prose","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.reference_format","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.reference_journal","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.reference_primary","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.reference_source","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.rigor.figure_provenance","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.rigor.reporting_guideline","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.rigor.trial_registration","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.tables_editable","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.template","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.title_page_items","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "ai.unverified_references","warning / suggestion","Reader judgment: fail becomes warning; optional advice uses suggest; uncertain results stay unchecked."
   "bibliography.age","suggestion","Reference age alone is not a defect; established or historical sources can be appropriate."
   "bibliography.citation_style","warning","Reference structure, attribution, or citation coverage needs author review; parsing and context can be imperfect."
   "bibliography.density","information","No universal citation-density threshold; records a measurement without an issue."
   "bibliography.entries","warning","Reference structure, attribution, or citation coverage needs author review; parsing and context can be imperfect."
   "bibliography.self_citations","suggestion","The 15-reference and 80% thresholds are paperpush's own, not a venue rule; editors may still judge the share excessive."
   "bibliography.uncited","warning","Reference structure, attribution, or citation coverage needs author review; parsing and context can be imperfect."
   "citations.cross_references","warning","Likely missing, duplicated, broken, or out-of-order content; extraction and citation detection are heuristic."
   "citations.duplicate_captions","warning","Likely missing, duplicated, broken, or out-of-order content; extraction and citation detection are heuristic."
   "citations.figure","warning","Likely missing, duplicated, broken, or out-of-order content; extraction and citation detection are heuristic."
   "citations.missing_captions","warning","Likely missing, duplicated, broken, or out-of-order content; extraction and citation detection are heuristic."
   "citations.panels","warning","Likely missing, duplicated, broken, or out-of-order content; extraction and citation detection are heuristic."
   "citations.supp_figure","warning","Likely missing, duplicated, broken, or out-of-order content; extraction and citation detection are heuristic."
   "citations.supp_table","warning","Likely missing, duplicated, broken, or out-of-order content; extraction and citation detection are heuristic."
   "citations.supplementary_attachments","warning","The manuscript references supplementary material but no supplementary upload is listed; reference detection is heuristic."
   "citations.table","warning","Likely missing, duplicated, broken, or out-of-order content; extraction and citation detection are heuristic."
   "consistency.abstract","warning","A likely mismatch needs reconciliation; manuscript extraction and matching do not prove an error."
   "consistency.authors","warning","A likely mismatch needs reconciliation; manuscript extraction and matching do not prove an error."
   "consistency.display_counts","warning","A likely mismatch needs reconciliation; manuscript extraction and matching do not prove an error."
   "consistency.keywords","warning","A likely mismatch needs reconciliation; manuscript extraction and matching do not prove an error."
   "consistency.statements","warning","A likely mismatch needs reconciliation; manuscript extraction and matching do not prove an error."
   "consistency.title","warning","A likely mismatch needs reconciliation; manuscript extraction and matching do not prove an error."
   "consistency.word_counts","warning","A likely mismatch needs reconciliation; manuscript extraction and matching do not prove an error."
   "cover_letter.date","warning / suggestion","Old date is optional freshness advice; future or contradictory ambiguous dates warrant review."
   "cover_letter.venue","warning","Likely wrong addressee; matching a venue name can still need interpretation."
   "details.affiliation_superscripts","suggestion","An affiliation number set inline rather than superscripted is a formatting detail production corrects."
   "details.latex_compiles","error / warning","Genuine source errors preventing a PDF are errors; a produced PDF with source errors is a warning; missing environment is unchecked."
   "details.placeholders","warning","Potential production or manuscript-completeness problem; context determines whether correction is required."
   "details.reference_order","warning / suggestion","An order that breaks the venue's or option's required order is a warning; author-year references in neither order, with no rule either way, are consistency advice."
   "details.word_cleanliness","warning","Potential production or manuscript-completeness problem; context determines whether correction is required."
   "figures.color_mode","warning","Readability or a guideline concern; inferred print size, image extraction, or placement can require confirmation."
   "figures.colorblind","warning / suggestion","Confusable colours are a warning where the guidelines' notes ask for colour-blind-safe figures; elsewhere they are accessibility advice."
   "figures.default_colors","suggestion","Palette defaults and typeface variety alone do not establish a readability problem or a venue violation."
   "figures.dimensions","warning","Readability or a guideline concern; inferred print size, image extraction, or placement can require confirmation."
   "figures.dpi","warning","Readability or a guideline concern; inferred print size, image extraction, or placement can require confirmation."
   "figures.font","warning","Readability or a guideline concern; inferred print size, image extraction, or placement can require confirmation."
   "figures.font_consistency","suggestion","Palette defaults and typeface variety alone do not establish a readability problem or a venue violation."
   "figures.grayscale","warning","Readability or a guideline concern; inferred print size, image extraction, or placement can require confirmation."
   "figures.legends","warning","Readability or a guideline concern; inferred print size, image extraction, or placement can require confirmation."
   "figures.lettering","warning","Readability or a guideline concern; inferred print size, image extraction, or placement can require confirmation."
   "figures.placement","warning","Readability or a guideline concern; inferred print size, image extraction, or placement can require confirmation."
   "figures.text_size","warning","Readability or a guideline concern; inferred print size, image extraction, or placement can require confirmation."
   "files.biosample_attributes","error / warning","Values NCBI rejects (missing mandatory attributes, values outside a fixed list, unparseable dates or locations, duplicate names, identical samples) are errors; soft formats (number and unit), historical country names, and samples no SRA row uses are warnings; definitions that cannot be downloaded leave the package rules unchecked."
   "files.manuscript_length","error","Measured upload or document limits are explicit constraints; unavailable measurements remain unchecked."
   "files.total_size","error","Measured upload or document limits are explicit constraints; unavailable measurements remain unchecked."
   "files.uploads","error / warning","Missing paths, invalid PDF headers, unacceptable types, and excess sizes are errors; suspiciously small PDFs or undetected pages are warnings."
   "form","not applicable","Scope marker for validation without a .sub; no submission-form verdict is possible."
   "form.authors","error / warning","Missing required author data or invalid corresponding-author count are errors; absent OpenReview IDs are warnings when name lookup is possible."
   "form.counts","error","Deterministic submission-schema requirements or explicit bounds are unsatisfied."
   "form.lengths","error","Deterministic submission-schema requirements or explicit bounds are unsatisfied."
   "form.options","error / suggestion","Invalid closed-list values are errors; accepted free text outside a recommended list is optional advice."
   "form.required","error","Deterministic submission-schema requirements or explicit bounds are unsatisfied."
   "form.schema","error","Deterministic submission-schema requirements or explicit bounds are unsatisfied."
   "form.subfields","error","Deterministic submission-schema requirements or explicit bounds are unsatisfied."
   "form.urls","error","Deterministic submission-schema requirements or explicit bounds are unsatisfied."
   "front_matter.abstract_citations","warning","Potential omission or noncompliant front matter; locating and interpreting extracted text is heuristic."
   "front_matter.contact_email","warning","Potential omission or noncompliant front matter; locating and interpreting extracted text is heuristic."
   "front_matter.institutional_email","suggestion","A personal address is discouraged, not forbidden: an author without an institution may have no other."
   "front_matter.keywords","warning / suggestion","Missing required keywords warrant review; absent optional keywords are discovery advice. Required blank form fields separately remain errors."
   "front_matter.orcid","suggestion","ORCID iDs are optional unless the venue requires them (reported by other checks), so finding none anywhere is advice."
   "front_matter.structured_abstract","warning","Potential omission or noncompliant front matter; locating and interpreting extracted text is heuristic."
   "guidelines","scope marker","Reports whether guideline checks apply or ran; does not itself report a manuscript violation."
   "guidelines.abstract_abbreviations","warning","Guideline concerns detected through section, text, image, or layout heuristics require confirmation."
   "guidelines.ai_policy","error / warning","AI tools in explicit author metadata violate a recorded prohibition; inferred missing or misplaced disclosures are warnings."
   "guidelines.counts","error / warning","Exact listed upload/display counts over a cap are errors; inferred caption counts over a cap are warnings."
   "guidelines.cover_letter","warning","The guidelines ask for a letter the portal does not require (a required portal field is form.required's error); a letter-length estimate is advisory."
   "guidelines.formats","warning","A format list read from the guidelines can miss formats the portal accepts (arXiv's .zip/.tar.gz); the portal's own accept list is files.uploads' error."
   "guidelines.length","error / warning","Measured length over a cap is an error; a count with unresolved exclusions is a warning."
   "guidelines.reference_count","error","Recorded format, count, length, size, or separate-upload requirements are directly violated; unreadable inputs are unchecked."
   "guidelines.reference_list","warning","Guideline concerns detected through section, text, image, or layout heuristics require confirmation."
   "guidelines.reference_style","warning","Guideline concerns detected through section, text, image, or layout heuristics require confirmation."
   "guidelines.section_length","warning","Guideline concerns detected through section, text, image, or layout heuristics require confirmation."
   "guidelines.section_order","warning","Guideline concerns detected through section, text, image, or layout heuristics require confirmation."
   "guidelines.sections","warning","Guideline concerns detected through section, text, image, or layout heuristics require confirmation."
   "guidelines.sizes","error","Recorded format, count, length, size, or separate-upload requirements are directly violated; unreadable inputs are unchecked."
   "guidelines.statements","warning","Guideline concerns detected through section, text, image, or layout heuristics require confirmation."
   "guidelines.supplementary","error / warning","Measured supplementary page-cap violation is an error; inferred bundling or placement concerns remain warnings."
   "guidelines.table_placement","warning","Guideline concerns detected through section, text, image, or layout heuristics require confirmation."
   "guidelines.template","warning","Guideline concerns detected through section, text, image, or layout heuristics require confirmation."
   "guidelines.text_fields","error","Recorded format, count, length, size, or separate-upload requirements are directly violated; unreadable inputs are unchecked."
   "guidelines.title_page","warning","Guideline concerns detected through section, text, image, or layout heuristics require confirmation."
   "guidelines.title_page_file","warning","The guidelines ask for a separate title page the portal does not require; a required portal field is form.required's error."
   "guidelines.title_page_own_page","warning","Where the abstract starts is found by heading and page-break heuristics, so it needs confirmation; a file without fixed pages is not applicable."
   "integrity.ai_text","similarity","Opt-in scan (--ai-detection, Pangram); a detector's estimate is not proof and misjudges some human writing, so reaching the threshold is a similarity finding that says whether the manuscript discloses AI use; a service failure is unchecked."
   "integrity.anonymity","warning","A serious concern worth review, but identity matching, metadata, external services, and content context can be imperfect."
   "integrity.chatbot_residue","likely error","A chatbot's own words ('As an AI language model', 'Regenerate response') would be critical if left in, but the narrow patterns can still match quoted output in a paper about language models, so the author confirms."
   "integrity.dois","warning","A serious concern worth review, but identity matching, metadata, external services, and content context can be imperfect."
   "integrity.hidden_text","error / warning","Hidden reviewer-directed instructions are errors; other hidden text or visible reviewer-directed prose is a warning."
   "integrity.latex_cleaner","warning / information","Author-only source becoming public warrants review; elsewhere cleanup is informational. Missing tool is unchecked."
   "integrity.links","warning","A serious concern worth review, but identity matching, metadata, external services, and content context can be imperfect."
   "integrity.openreview","error / warning","An explicitly supplied nonexistent or inactive profile blocks the portal; absent IDs and profile-quality concerns are warnings."
   "integrity.pdf_reader","unchecked","Missing optional PDF tools mean checks did not run, not that the manuscript violated a rule."
   "integrity.plagiarism","similarity / likely error","Opt-in scan (--plagiarism); similarity needs a reader (methods text, quotations, the author's own preprint), so reaching the threshold is a similarity finding, never an error; characters that hide text from the scan are a likely error; a service failure is unchecked."
   "integrity.reference_versions","warning / suggestion","A preprint listed beside its article is a reference error; citing a preprint since published, or an older arXiv version, is a preference."
   "integrity.retractions","warning","A serious concern worth review, but identity matching, metadata, external services, and content context can be imperfect."
   "integrity.sensitive","warning","A serious concern worth review, but identity matching, metadata, external services, and content context can be imperfect."
   "integrity.unmatched_references","warning / suggestion","Likely bibliographic mismatch is a warning; adding a DOI to a confirmed work is a suggestion. Unverified works remain unchecked."
   "layout.font","warning","Measurements or inference suggest a venue-rule violation; typography and document geometry are not conclusive."
   "layout.density","suggestion","Page fill is a reviewer's impression, not a venue rule; the gap and page-limit measures are heuristics over PDF geometry."
   "layout.font_size","warning","Measurements or inference suggest a venue-rule violation; typography and document geometry are not conclusive."
   "layout.footnotes","warning","Footnotes are read from the Word or LaTeX markup; a venue may accept them moved elsewhere, so the author decides."
   "layout.forbidden_fonts","warning","The fonts a Word file names are reported; the author decides how to replace them."
   "layout.heading_levels","warning","Heading levels are read from Word heading styles or LaTeX sectioning; a run-in heading style can count as a level."
   "layout.language","warning","Measurements or inference suggest a venue-rule violation; typography and document geometry are not conclusive."
   "layout.line_numbers","warning","Measurements or inference suggest a venue-rule violation; typography and document geometry are not conclusive."
   "layout.line_spacing","warning","Measurements or inference suggest a venue-rule violation; typography and document geometry are not conclusive."
   "layout.margins","warning","Measurements or inference suggest a venue-rule violation; typography and document geometry are not conclusive."
   "layout.page_numbers","warning","Measurements or inference suggest a venue-rule violation; typography and document geometry are not conclusive."
   "layout.page_size","warning","Measurements or inference suggest a venue-rule violation; typography and document geometry are not conclusive."
   "layout.section_balance","suggestion","How a paper divides its space is the author's call; the ranges are typical of accepted papers, not rules."
   "layout.template","warning","Measurements or inference suggest a venue-rule violation; typography and document geometry are not conclusive."
   "prose.acronyms","warning / suggestion","Undefined, conflicting, or prematurely used acronyms are warnings; repeated definitions, late definitions after full terms, and unused abbreviations are suggestions."
   "prose.brackets","warning","A likely mechanical mistake, rather than an optional stylistic preference; extraction can produce false positives."
   "prose.conventions","warning / suggestion","Malformed 'et al.' is a warning; a malformed 'i.e.'/'e.g.' (often PDF extraction debris) and consistent choice among defensible forms are optional style."
   "prose.dashes","warning / suggestion","LaTeX opening quotes rendered incorrectly are warnings; dash and range typography is optional style."
   "prose.double_spaces","suggestion","Optional style or consistency advice; an explicitly required English variant is checked separately by layout.language."
   "prose.heading_case","suggestion","Optional style or consistency advice; an explicitly required English variant is checked separately by layout.language."
   "prose.number_format","suggestion","Optional style or consistency advice; an explicitly required English variant is checked separately by layout.language."
   "prose.numbers","warning / suggestion","A recorded venue style rule warrants a warning; otherwise formatting and consistency advice is a suggestion."
   "prose.punctuation_spacing","warning","A likely mechanical mistake, rather than an optional stylistic preference; extraction can produce false positives."
   "prose.repeated_words","warning","A likely mechanical mistake, rather than an optional stylistic preference; extraction can produce false positives."
   "prose.repetition","suggestion","Optional style or consistency advice; an explicitly required English variant is checked separately by layout.language."
   "prose.serial_comma","warning / suggestion","A recorded venue style rule warrants a warning; otherwise formatting and consistency advice is a suggestion."
   "prose.spelling","warning","A likely mechanical mistake, rather than an optional stylistic preference; extraction can produce false positives."
   "prose.spelling_variant","suggestion","Optional style or consistency advice; an explicitly required English variant is checked separately by layout.language."
   "prose.usage","warning / suggestion","Likely agreement mistakes are warnings; contractions, long sentences, and style consistency are suggestions."
   "rigor.cell_lines","warning / suggestion","Missing authentication or mycoplasma statements are warnings where the venue asks for them (or a reporting-guideline statement is required), suggestions elsewhere; a line Cellosaurus lists as problematic is a warning."
   "rigor.code_availability","warning / suggestion","Code or data only on request, missing code, an empty or code-less repository, or a non-public accession are warnings; a missing licence, README, environment file, or archived release is advice unless the venue's notes ask for it."
   "rigor.data_accessions","warning","Scientific or reproducibility concern needing contextual review; detection or external records alone cannot establish noncompliance."
   "rigor.design","warning / suggestion","Missing design items are warnings where the venue cites ARRIVE/MDAR or similar (or requires a reporting-guideline statement), suggestions elsewhere."
   "rigor.rrids","warning / suggestion","Missing RRIDs are warnings where the venue's notes ask for RRIDs (or a reporting-guideline statement is required), suggestions elsewhere; an RRID the resolver does not know is a warning."
   "rigor.statistics","warning","Scientific or reproducibility concern needing contextual review; detection or external records alone cannot establish noncompliance."
   "rigor.trial_registration","warning","Scientific or reproducibility concern needing contextual review; detection or external records alone cannot establish noncompliance."
