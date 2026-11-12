MASTER CONSOLIDATION (RoleGenie slice 1, CR-rg-slice1-assessment)

You are consolidating several versions of ONE person's resume into a single list of facts. Each fact must be
copied VERBATIM from the resumes. You do not write any fact text of your own.

THE GOVERNING RULE: facts are COPIED, never rephrased. For every fact, give one or more `sources`, each a `file` name
exactly as labelled below and a `span`: a contiguous piece of that file's text copied character for character
(same words, same spelling, same capitalisation, same punctuation; line breaks may be written as spaces). Code
checks every span against the file and DISCARDS any fact whose span is not found, so a paraphrase, a summary, a
corrected typo or a merged sentence is lost. Never upgrade or soften: do not turn "designed" into "built", do not add
"senior", "led" or a number the text does not state, and do not drop a limiting qualifier ("contributed to", "part of a
team that", "designed for"). If something is not in the resumes, it is not a fact.

What a fact is: one atomic, checkable item a recruiter would match against a job requirement, as the resume states it:
- `role`: a role heading line (title, employer, dates), e.g. "Data Analyst, Fenwick Dairy (2017 - 2020)";
- `achievement`: one bullet or sentence describing something done, e.g. "Cut report run time from 3 hours to 20 minutes.";
- `skill`: a skills line or a named tool/technology phrase, e.g. "SQL, Tableau, dbt";
- `domain`: a sector or problem domain the text names;
- `certification`, `education`: copied as written, e.g. "BSc Statistics, Northmoor University (2017)";
- `other`: any other factual statement (languages spoken, licences, location or right-to-work statements).
Skip names, contact details, headings on their own and decorative text.

Consolidation: when the same fact appears in several resumes (same meaning, possibly different whitespace), output it
ONCE with one source per file, each span copied from its own file. Do not merge two different statements into one fact.

Contradictions: compare the resumes role by role. When two resumes give the same role (same employer, same job) a
DIFFERENT title, DIFFERENT dates or a different employer name, output both role facts separately and list a
contradiction naming both fact indexes, its `kind` (`dates`, `title` or `employer`; pick the first that applies) and a
short neutral `note`. NEVER resolve a contradiction yourself, never pick the "right" version, never drop one side.
A newer resume adding a newer role is not a contradiction.

Respond with JSON only, no markdown fences, no preamble:
{"facts": [{"category": "<role|skill|achievement|domain|certification|education|other>",
            "sources": [{"file": "<file name>", "span": "<verbatim text>"}]}],
 "contradictions": [{"facts": [<fact index, 0-based>, ...], "kind": "<dates|title|employer>", "note": "<short note>"}]}

Resumes:
