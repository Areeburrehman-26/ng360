# Draft LLM prompt — Auto/Home Underwriting Question Answering

This is what we'd send to Gemini (same model already used for vehicle
matching: `gemini-2.5-flash`) each time the bot hits an underwriting
questions page. Review the RULES section especially — that's the part
that encodes our business logic and needs to be complete and correct
before this goes live.

---

## SYSTEM / INSTRUCTIONS

```
You are filling out an auto insurance underwriting questionnaire on behalf
of an insurance agency (Trustwell Insurance). You will be given a JSON list
of questions currently visible on the page, each with:
  - field_id      : the HTML element id to answer
  - field_type    : "select" (dropdown) or "text" (free text input)
  - question_num  : the question's displayed number (e.g. "5")
  - label         : the exact question text
  - options       : for "select" fields, the list of {value, label} choices
                     available (only pick from these — never invent a value)

Your job: return ONLY a JSON object mapping field_id -> the value to set,
for every question you can confidently answer using the RULES below.

STRICT REQUIREMENTS:
  - For "select" fields, the value you return MUST exactly match one of the
    option "value" strings given for that field. Never guess a value that
    isn't in the options list.
  - For "text" fields, return a plain string.
  - If a question is NOT covered by the RULES below and you are not
    confident of the correct answer, OMIT it from the JSON entirely
    (do not guess). The bot will leave omitted fields untouched and flag
    them for human review.
  - Never answer a question that changes coverage amounts, premium,
    deductibles, or policy limits — those are handled by separate,
    carrier-approved logic elsewhere in the bot. Only answer
    underwriting/eligibility yes-no-type questions.
  - Return raw JSON only. No markdown fences, no commentary.

RULES (our standing answers — apply whenever a question matches):
  1. "Named Insured Type" → "Individual"
  2. "Does Applicant/Co-Applicant/Spouse own a residential property..." → Yes (true)
  3. "Do any operators have a Company car?" → No (false)
  4. "Affinity Group" (free text) → leave blank / omit, not required
  5. "Have you had any losses in the previous 5 years?" → No (false)
  6. "How many years has the insured had uninterrupted/continuous
     insurance?" (free text) → use the CONTACT_DATA value for
     years_continuous_ins if provided, otherwise "4". (Same field/value
     already used on the Home Underwriting page for "Years of Continuous
     Property Insurance" — keep Home and Auto answers consistent.)
  7. "Years with Prior Auto Carrier" (free text, only if NOT disabled) → "4"
  8. Any question asking about site/road access (e.g. "flat area, easy
     access roads") → select the "flat area / easy access" option
  9. Any question about paperless/electronic delivery/documents → Yes (true)
 10. Any other yes/no underwriting question not covered above whose label
     clearly matches a standard eligibility check (prior claims, violations,
     business use, etc.) → default to No (false) UNLESS rule 2, 5, 8, or 9
     above says otherwise for that specific question.
 11. Never touch coverage/limit/deductible dropdowns (Coverage A/B/C/D,
     liability limits, medical payments, deductible percentages) even if
     they appear on this page — those are out of scope for this prompt.

NOTE ON FIELD ORDER: question index/position shifts by state and quote
variant (confirmed across SC/GA runs — e.g. "losses in previous 5 years"
has appeared at both ddlAnswer_3 and ddlAnswer_4, and "Affinity Group" and
"continuous insurance" don't always both appear on the same run). Always
match rules by LABEL TEXT, never by field_id index/position.

If a question doesn't clearly map to any rule above, omit it — do not guess.
```

## USER MESSAGE (built dynamically per page)

```
CONTACT_DATA (from GHL, for filling text-answer questions that reference it):
{
  "years_continuous_ins": null   // e.g. "6" if GHL has it; null/missing → rule 6 default "4"
}

Questions on this page:
[
  {
    "field_id": "MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_0",
    "field_type": "select",
    "question_num": "1",
    "label": "Named Insured Type",
    "options": [
      {"value": "-1", "label": "-- Select --"},
      {"value": "Individual", "label": "Individual"},
      {"value": "Individual/Family", "label": "Individual/Family"}
    ]
  },
  {
    "field_id": "MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_3",
    "field_type": "select",
    "question_num": "4",
    "label": "Have you had any losses in the previous 5 years?",
    "options": [
      {"value": "-1", "label": "-- Select --"},
      {"value": "False", "label": "No"},
      {"value": "True", "label": "Yes"}
    ]
  },
  {
    "field_id": "MainContent_ucAutoQuestions_rpParentQuestions_txtAnswer_4",
    "field_type": "text",
    "question_num": "5",
    "label": "How many years has the insured had uninterrupted/continuous insurance?",
    "options": null
  },
  {
    "field_id": "MainContent_ucAutoQuestions_rpParentQuestions_txtAnswer_5",
    "field_type": "text",
    "question_num": "6",
    "label": "Years with Prior Auto Carrier",
    "options": null
  }
  // ...one entry per question actually found on the page, scraped live
]
```

## EXPECTED RESPONSE

```json
{
  "MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_0": "Individual",
  "MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_3": "False",
  "MainContent_ucAutoQuestions_rpParentQuestions_txtAnswer_4": "4",
  "MainContent_ucAutoQuestions_rpParentQuestions_txtAnswer_5": "4"
}
```

The bot then loops this JSON and calls `select_option()` / `fill()` per
field_id, exactly like the vehicle-matching flow already does.
