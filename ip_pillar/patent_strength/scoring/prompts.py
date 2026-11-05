"""
prompts.py
Gemini prompt templates for the four sub-factors and the final summary.
"""

SF1_PROMPT = """
You are a pharmaceutical patent attorney evaluating the novelty and non-obviousness
of a patent's core inventive step.

Below are the most relevant excerpts from the patent document (independent claims,
background, prior art discussion, summary, and examples):

--- PATENT EXCERPTS ---
{chunks}
--- END EXCERPTS ---

Patent details:
- Drug: {drug}
- Patent Number: {patent_number}
- Filing Date: {filing_date}
- Grant Date: {grant_date}

Evaluate Sub-Factor 1: Novelty & Non-Obviousness of the Core Inventive Step (Weight: 40%).

Objective: Determine whether the claimed invention is structurally or conceptually close to prior art.

Evaluation Steps:
1. Parse the independent claim and identify the inventive element.
2. Summarize the core inventive step.
3. Extract cited prior art from the Background section.
4. Compare the invention with known approaches using the patent text and literature references.
5. Determine whether the modification appears incremental or novel.

Scoring rubric (assign exactly one integer score 1-5):

  Score 5 — Minor Predictable Variation of Known Prior Art:
    The claimed invention differs from prior art only by a routine modification that
    would normally be tried by a skilled person.
    Typical indicators:
    - Simple substitution (e.g., replacing one known excipient with another used for the same purpose)
    - Small parameter adjustment (e.g., pH range, dosage range, concentration)
    - Minor mechanical tweak to an existing device design
    - Same function achieved using the same mechanism
    Key signal: The modification is standard practice in the field.

  Score 4 — Derivative of Known Class with Predictable Properties:
    The invention belongs to a known class of technologies but introduces small modifications.
    Typical indicators:
    - New salt or polymorph of a known molecule
    - Use of a known stabilization technique in a closely related system
    - Device design derived from an existing platform
    Key signal: The modification is expected to work based on prior knowledge.

  Score 3 — Derivative but Non-Trivial:
    The invention is based on known technology but involves non-obvious adjustments or combinations.
    Typical indicators:
    - Combining technologies not previously used together
    - Optimization that required experimentation
    - Modification not directly suggested in prior art
    Key signal: The solution was not obvious but still builds on known technology.

  Score 2 — Significant Modification in Known Technical Area:
    The invention introduces a major technical change within an existing field.
    Typical indicators:
    - New mechanism for drug stabilization
    - New device architecture for the same therapy
    - Novel manufacturing approach not previously applied to this class
    Key signal: The approach is technically different but still within the same technological domain.

  Score 1 — Truly Novel Concept:
    The invention introduces a new technical principle or approach not previously seen in the prior art.
    Typical indicators:
    - No close structural or technical analogues
    - Prior art does not describe similar solutions
    - The invention solves a problem previously considered unsolved
    Key signal: The invention represents a new concept rather than an improvement of existing technology.

Respond ONLY with a valid JSON object — no markdown, no explanation outside the JSON:
{{
  "score": <integer 1-5>,
  "score_reason": "<1 sentence stating the primary reason this specific score was chosen over adjacent scores>",
  "reasoning": "<2-4 sentence justification referencing specific claims or background text>",
  "key_findings": ["<finding 1>", "<finding 2>", "<finding 3>"]
}}
"""

SF2_PROMPT = """
You are a pharmaceutical patent attorney evaluating the obvious-to-combine risk
of a patent — i.e., whether a person skilled in the art would have had a clear
motivation to combine the known elements with a reasonable expectation of success.

Below are the most relevant excerpts from the patent document (background, state of
the art, cited references, summary of invention, and technical problem description):

--- PATENT EXCERPTS ---
{chunks}
--- END EXCERPTS ---

Patent details:
- Drug: {drug}
- Patent Number: {patent_number}
- Filing Date: {filing_date}
- Grant Date: {grant_date}

Evaluate Sub-Factor 2: Obvious-to-Combine Risk (Weight: 30%).

Objective: Determine whether a person skilled in the art would have had a clear
motivation to combine known elements with a reasonable expectation of success.

Evaluation Steps:
1. Parse the independent claim and identify the elements being combined.
2. Determine whether the invention combines elements such as APIs, excipients,
   device components, manufacturing steps, or therapeutic strategies.
3. Identify the technical problem the patent claims to solve.
4. Review the Background section, Summary of the invention, and cited prior art.
5. Extract references mentioned in the Background section and reference list.
6. Assess whether prior patents or scientific literature already proposed similar combinations
   (e.g., [API name + formulation] or [technology + combination therapy]).
7. Assess whether a skilled person would have expected the combination to succeed.

Scoring rubric (assign exactly one integer score 1-5):

  Score 5 — Combination Widely Known and Predictable:
    The combination is explicitly discussed in prior patents or journal literature.
    Typical indicators:
    - Prior patents describing similar combinations
    - Scientific articles proposing the same strategy
    - Combination solves a widely recognized problem
    - Result appears purely additive
    Sources to confirm: Google Patents / Espacenet, PubMed / Google Scholar.
    Key signal: The field already expected this combination to work.

  Score 4 — Clear Motivation to Combine:
    Prior art strongly suggests combining the elements.
    Typical indicators:
    - Prior patents describe similar combinations
    - Scientific literature proposes the same approach
    - Known benefits from combining similar technologies
    Key signal: Combination was a logical next step in the field.

  Score 3 — Motivation Exists but Outcome Uncertain:
    Literature or patents suggest the combination but do not clearly demonstrate success.
    Typical indicators:
    - Limited experimental evidence in prior art
    - Combination discussed but not validated
    - Uncertain compatibility between components
    Key signal: Combination was plausible but not clearly predictable.

  Score 2 — Limited Motivation to Combine:
    Little prior art supports the combination.
    Typical indicators:
    - Few references describing similar approaches
    - Known incompatibilities or technical barriers
    - Limited discussion in scientific literature
    Key signal: Combination would not have been an obvious direction.

  Score 1 — No Clear Motivation or Unexpected Result:
    The combination appears entirely novel.
    Typical indicators:
    - No similar patents found in patent databases
    - No journal literature discussing the combination
    - Patent demonstrates unexpected synergy
    Key signal: The combination was not suggested by prior art.

Respond ONLY with a valid JSON object — no markdown, no explanation outside the JSON:
{{
  "score": <integer 1-5>,
  "score_reason": "<1 sentence stating the primary reason this specific score was chosen over adjacent scores>",
  "reasoning": "<2-4 sentence justification referencing specific background or cited references>",
  "key_findings": ["<finding 1>", "<finding 2>", "<finding 3>"]
}}
"""

SF3_PROMPT = """
You are a pharmaceutical patent attorney evaluating the prosecution history
vulnerability of a patent — i.e., whether the patent was granted only after
significant claim narrowing or limiting arguments that may weaken enforceability.

Below are the most relevant excerpts from the patent document (independent claims,
amendments, prosecution history references, examiner rejections, and applicant responses):

--- PATENT EXCERPTS ---
{chunks}
--- END EXCERPTS ---

Patent details:
- Drug: {drug}
- Patent Number: {patent_number}
- Filing Date: {filing_date}
- Grant Date: {grant_date}

Evaluate Sub-Factor 3: Prosecution History Vulnerability (Weight: 20%).

Objective: Determine whether the patent was granted only after significant claim
narrowing or limiting arguments, which may weaken enforceability.

Evaluation Steps:
1. Retrieve and review any prosecution history (file wrapper) information available in the excerpts.
2. Extract the originally filed independent claim (if discernible).
3. Extract the final granted independent claim.
4. Compare the two versions to identify narrowing amendments.
5. Review examiner rejections and cited prior art references.
6. Review applicant responses and arguments used to distinguish prior art.

Scoring rubric (assign exactly one integer score 1-5):

  Score 5 — Significant Narrowing Amendments:
    Broad claim scope was surrendered to obtain allowance, creating strong estoppel risk.
    Typical indicators:
    - Major structural limitations added
    - Specific excipients, ratios, or device features introduced
    - Narrowed chemical structures
    Key signal: Broad claim scope was surrendered to obtain allowance.

  Score 4 — Meaningful Claim Narrowing:
    Claims were modified to distinguish strong prior art, reducing scope during prosecution.
    Typical indicators:
    - Claims modified to distinguish strong prior art
    - Important technical limitations added
    Key signal: Scope reduced during prosecution.

  Score 3 — Moderate Amendments:
    Several amendments were made but they are not strongly limiting; some vulnerability exists.
    Typical indicators:
    - Several amendments but not strongly limiting
    Key signal: Some vulnerability exists.

  Score 2 — Minor Amendments:
    Only small clarifications or wording changes were made during prosecution.
    Typical indicators:
    - Small clarifications or wording changes
    Key signal: Prosecution history does not significantly weaken the patent.

  Score 1 — Claims Granted Largely as Filed:
    Few or no amendments were required; prosecution history is strong.
    Typical indicators:
    - Few or no amendments
    - Weak prior art cited
    Key signal: Strong prosecution history.

Respond ONLY with a valid JSON object — no markdown, no explanation outside the JSON:
{{
  "score": <integer 1-5>,
  "score_reason": "<1 sentence stating the primary reason this specific score was chosen over adjacent scores>",
  "reasoning": "<2-4 sentence justification referencing specific claim amendments or prosecution arguments>",
  "key_findings": ["<finding 1>", "<finding 2>", "<finding 3>"]
}}
"""

SF4_PROMPT = """
You are a pharmaceutical patent attorney evaluating secondary considerations
(objective evidence of non-obviousness) for a patent.

Below are the most relevant excerpts from the patent document (examples, experimental
data, comparative experiments, data tables, and claimed advantages):

--- PATENT EXCERPTS ---
{chunks}
--- END EXCERPTS ---

Patent details:
- Drug: {drug}
- Patent Number: {patent_number}
- Filing Date: {filing_date}
- Grant Date: {grant_date}

Evaluate Sub-Factor 4: Secondary Considerations — Evidence of Non-Obviousness (Weight: 10%).

Objective: Evaluate whether the patent provides objective evidence supporting
non-obviousness, such as unexpected results.

Evaluation Steps:
1. Review experimental data in the patent, focusing on:
   - Examples section
   - Data tables
   - Comparative experiments
2. Identify claimed advantages, such as:
   - Improved stability
   - Improved bioavailability
   - Enhanced efficacy
   - Reduced toxicity
3. Assess whether similar advantages were already known in scientific literature.
4. Assess whether similar results were already disclosed in patent databases.
5. Evaluate whether the improvement is truly unexpected.

Scoring rubric (assign exactly one integer score 1-5):

  Score 5 — No Evidence of Unexpected Results:
    No objective evidence supporting non-obviousness.
    Typical indicators:
    - No comparative data in the patent
    - Advantages asserted but not demonstrated
    - Similar results already known in literature
    Key signal: No objective evidence supporting non-obviousness.

  Score 4 — Weak or Unsupported Advantages:
    Advantages are weakly supported.
    Typical indicators:
    - Limited data
    - Benefits not clearly demonstrated
    Key signal: Advantages are weakly supported.

  Score 3 — Moderate Improvement:
    Improvement exists but may be predictable.
    Typical indicators:
    - Data shows improvement but similar effects already reported in literature
    Key signal: Improvement exists but may be predictable.

  Score 2 — Strong Technical Improvement:
    Significant improvement supported by experiments strengthens the patent.
    Typical indicators:
    - Significant improvement supported by experiments
    - Prior art does not show similar results
    Key signal: Improvement strengthens the patent.

  Score 1 — Clear Unexpected Results Supported by Data:
    Results strongly support non-obviousness.
    Typical indicators:
    - Dramatic improvement over prior art
    - Strong comparative data
    - Evidence of synergy
    Key signal: Results strongly support non-obviousness.

Respond ONLY with a valid JSON object — no markdown, no explanation outside the JSON:
{{
  "score": <integer 1-5>,
  "score_reason": "<1 sentence stating the primary reason this specific score was chosen over adjacent scores>",
  "reasoning": "<2-4 sentence justification referencing specific experimental data or claimed advantages>",
  "key_findings": ["<finding 1>", "<finding 2>", "<finding 3>"]
}}
"""

FINAL_SUMMARY_PROMPT = """
You are a pharmaceutical patent attorney. Based on the sub-factor scores and reasoning
below, generate a concise summary for this patent.

Patent: {patent_number}
Drug: {drug}
Patent Type: {patent_type}

Sub-Factor Scores and Reasoning:
- SF1 (Novelty & Non-Obviousness, 40%): Score {sf1_score}/5 — {sf1_reasoning}
- SF2 (Obvious-to-Combine Risk, 30%): Score {sf2_score}/5 — {sf2_reasoning}
- SF3 (Prosecution History Vulnerability, 20%): Score {sf3_score}/5 — {sf3_reasoning}
- SF4 (Secondary Considerations, 10%): Score {sf4_score}/5 — {sf4_reasoning}
- Weighted Final Score: {weighted_score}/5

Respond ONLY with a valid JSON object — no markdown, no explanation outside the JSON:
{{
  "core_inventive_step": "<1 sentence summarizing the patent's core inventive step>",
  "key_vulnerabilities": "<1-2 sentences on the main legal weaknesses based on the scores>",
  "key_strengths": "<1-2 sentences on the main legal strengths based on the scores>"
}}
"""
