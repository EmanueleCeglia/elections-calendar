# EU elections calendar

A pipeline that builds and maintains a calendar of the **national and regional
elections in the 27 EU member states**, from the day it runs until 31 December
of the following year. The output is an Excel workbook plus CSV files.

* **First run** creates the calendar.
* **Every later run** adds elections that have appeared, refines dates that
  have become more precise (e.g. "autumn 2027" → "26 September 2027"),
  records reschedules, and marks past elections as held. Nothing is ever
  silently deleted.
* **Nothing is hard-coded**: elections are always fetched. The only static
  data is the list of member states and some constitutional facts in
  `config/countries.yaml`, for example "Germany's president is elected
  indirectly". The window moves with the run date, so the same code works
  for 2028, 2029, and later years.

## Output

| File | What it is |
|---|---|
| `output/elections_calendar.xlsx` | Workbook with sheets **Calendar** (upcoming), **Past**, **Needs review**, **Changes** (history of every run) and **About** (run metadata, how to read the columns). |
| `output/elections_calendar.csv` | The calendar itself. This is also the pipeline's state: the next run reads it back, and its git diff shows what changed. |
| `output/changes.csv` | Append-only log: `added`, `refined`, `revised`, `rescheduled`, `updated`, `held`, `unconfirmed`. |
| `output/review_candidates.csv` | Elections reported only by weak evidence (e.g. a stale Wikidata entry), kept out of the calendar until another source confirms them. |

Main columns:

| Column | Meaning |
|---|---|
| `date_start`, `date_end` | The period the election falls in. Both are equal when the day is known; a multi-day poll spans its voting days. |
| `date_precision` | `day`, `range`, `month`, `season`, `quarter`, `half`, `deadline` ("by 25 July"), `year` |
| `date_text` | The date as the best source wrote it ("By 25 July", "Autumn 2027") |
| `second_round` | Scheduled run-off |
| `level`, `region`, `election_type` | `national` / `regional`; the first-level region (or "All …" for a nationwide regional round); `parliamentary`, `presidential`, `upper_house`, `regional`, `european_parliament` |
| `indirect` | Chosen by parliament or an electoral college, e.g. the German Federal Convention or the Dutch Senate |
| `status` | `confirmed`: exact date from two sources, or one official listing. `expected`: exact date from one source. `estimated`: only a period is known. `held`: in the past. |
| `confidence` | `high`: two or more sources, or one authoritative one. `medium`: one source. Events with only `low` confidence go to *Needs review*. |
| `sources`, `evidence`, `source_urls` | Which sources reported the election, what each one said, and links |
| `first_seen`, `last_updated`, `last_seen`, `seen_in_last_run` | History of the row across runs |

## Scope

* National: parliament (both chambers when elected together), separately
  elected upper houses (including partial renewals such as the Czech
  Senate), presidents, the European Parliament.
* Indirect national elections: head of state or upper house chosen by an
  assembly (German Federal Convention, Latvian president, Dutch Senate,
  Slovenian National Council).
* Regional: legislatures or executives of first-level subdivisions
  (German/Austrian states, Spanish autonomous communities, Italian regions,
  Dutch provinces, Åland, EU outermost regions such as Saint-Martin, …).
* Excluded: municipal/local elections, referendums, by-elections, party
  primaries, and territories outside the EU (Greenland, Faroe Islands, the
  Dutch Caribbean countries, French overseas countries, Northern Cyprus).
  Each of these switches is in `config/settings.yaml` (`scope`,
  `excluded_territories`).

## Sources

| Source | What it contributes |
|---|---|
| **Wikipedia** – "YYYY national electoral calendar", "YYYY local electoral calendar", "YYYY elections in the European Union" (for every year in the window) | Dates as currently known, including "by…" deadlines and "unknown date" sections. Each linked election article is resolved to its Wikidata item to get the country, region and kind of election. All pages are fetched in a single request. |
| **Wikidata** – SPARQL search for election items of the EU-27 dated within the window, plus every item found through Wikipedia | Dates with their precision (day/month/year). Month-end dates such as "30 June 2027" are usually legal deadlines or placeholders, so they are treated as weak evidence. Wikidata also supplies the first-level subdivisions of each country (P150) in many languages, so that "Tirol", "Tyrol" and "Tyrolean" resolve to the same region. |
| **EU Committee of the Regions – "EU-27 Election Calendar" PDF** | A table of national and regional elections, each marked *Confirmed* or *Expected*. The file is updated in place; its URL is in `config/settings.yaml`, and the run warns when the file has not been updated for more than 90 days. |
| **OpenAI research** (optional, `OPENAI_API_KEY`) | One request per member state using the Responses API with web search and a strict JSON schema. It catches what the structured sources miss (elections called last week, regions without a Wikipedia article yet) and confirms dates against official sources. Its findings are capped at *medium* confidence. |
| **Manual** – `data/manual_events.csv` | Your own additions or corrections. They have the highest priority. |

### How sources are combined

1. Candidates that describe the same election are grouped together when they
   share the same country, level, type and region (regions are matched
   through the multilingual gazetteer) and have compatible dates, or when
   they point to the same Wikidata item.
2. The date is the **intersection** of the sources' periods, applied in
   priority order (`source_priority` in the settings): "By 25 July" +
   "spring 2027" + "2027" gives spring 2027. When a lower-priority source
   contradicts a higher-priority one, the conflict is written to `notes`
   and the lower-priority date is not used. A deadline that one page echoes
   as a date ("28 September" next to "By 28 September") stays a deadline
   until a second source confirms that day.
3. An umbrella row such as "2027 Spanish regional elections" is dropped
   when the individual regions are listed.

### How runs update the calendar

The new results are matched to the stored rows by Wikidata item, or by
country/level/type/region with overlapping dates:

| Situation | Action |
|---|---|
| Not in the calendar yet | `added` (low-confidence rows go to *Needs review* instead) |
| Narrower date inside the stored period | `refined` |
| Date moved outside the stored period | `rescheduled` (old and new dates logged) |
| Sources are now vaguer than the stored date, but one of the sources behind it did not run | stored date kept |
| Every source behind the stored date reported again and they now support only a different or wider period | `revised` (protects against one noisy answer making a date look more precise than it is) |
| No source reports it any more | kept, `seen_in_last_run = no`; after 21 days (`stale_after_days`) `unconfirmed` and moved to *Needs review* |
| Date has passed | `held` (moved to the *Past* sheet) |

## Running it

```bash
pip install ".[llm]"                         # or ".[dev]" for tests
export OPENAI_API_KEY=...                    # optional
python -m elections_calendar run             # window: today .. 31 Dec next year
python -m elections_calendar run --sources wikipedia,wikidata,cor_pdf --dry-run
python -m elections_calendar evaluate        # compare with the ground truth
python -m pytest -q
```

Useful options: `--today 2026-10-12` (run as of a past date), `--end`,
`--cache-dir .cache` (re-use HTTP responses while developing; Wikimedia
rate-limits shared IP addresses heavily), `--output-dir` (write somewhere
other than `output/`), and `ELECTIONS_LLM_MODEL` (override the model).

### Scheduled updates

`.github/workflows/update-calendar.yml` runs the pipeline every Monday and
commits the updated `output/`. You can also start it by hand from the
Actions tab. To enable the LLM source there, add the key as a repository
secret: **Settings → Secrets and variables → Actions → New repository
secret**, named `OPENAI_API_KEY`. Without the secret, that source is
skipped.

## Ground truth and evaluation

`data/ground_truth/eu_elections_2026-10-12_to_2027-12-31.csv` lists the
39 national, regional and indirect elections in the EU-27 between
12 October 2026 and 31 December 2027. I compiled it by hand from official
and news sources (one URL per row) to test the pipeline.
`python -m elections_calendar evaluate` reports recall, precision and date
agreement against it.

Results for the run of 12 October 2026 (all sources, `gpt-6-sol`):

| | First run | Second run (same day, updating the stored calendar) |
|---|---|---|
| Recall (ground-truth elections found) | **39 / 39 (100%)** | 39 / 39 |
| Precision (calendar rows that are in the ground truth) | **39 / 39 (100%)** | 39 / 39 |
| Dates of matched elections | 29 exact days, 10 periods consistent with the truth, 0 wrong | same |
| Changes | 39 added | 2 refined, 1 revised (LLM wording of estimated periods) |

Seven weak candidates went to *Needs review* instead of the calendar. Four are
stale Wikidata entries: a "2027 Maltese general election", although Malta
voted in May 2026, and three 2026 elections that have already been held.
Two are Aragon and Extremadura, which the Committee of the Regions PDF still
lists for 23 May 2027 even though both regions held snap elections that
started new four-year terms. One is a speculative early Romanian election
reported by the LLM.

What each source adds (same window):

| Configuration | Recall | Precision |
|---|---|---|
| Wikipedia + Wikidata + CoR PDF (no API key) | 35 / 39 | 35 / 37 (misses Saint-Martin, Ceuta, Melilla and the Slovenian National Council; includes Aragon and Extremadura) |
| LLM only, `gpt-6-luna` | 31 / 39 | 58% (missed the Spanish snap election, Åland, Ceuta, Melilla…) |
| LLM only, `gpt-6-sol` | 38 / 39 | 79% (most extras were the Slovak regions listed one by one) |
| All sources, `gpt-6-sol` | 39 / 39 | 100% |

LLM cost per run: 27 requests, about 480k input and 20k output tokens,
and about 60 web searches.

### Known limitations

* The LLM's web searches differ from run to run. An estimate it finds once
  (for example, a minister saying "October 2027") may be missing the next
  time. Dates the LLM marks as `estimate` therefore never narrow other
  sources, and changes in its answers are logged as `revised`.
* Wikimedia rate-limits shared IP addresses heavily. The pipeline retries
  with back-off, so a run takes about 5–10 minutes.
* The CoR PDF has no stable landing page. If its URL changes, update
  `sources.cor_pdf.urls`; the run warns when the file has not been updated
  for more than 90 days.

## Extending

* **New source**: subclass `sources.base.Source`, return `Candidate`s, and
  register the class in `pipeline.SOURCES` with a priority in the settings.
* **Local elections**: set `scope.include_local: true`. The classifiers
  already recognise them, but the region model would need a municipal tier.
* **Other countries**: add them to `config/countries.yaml`.

## Layout

```
config/            settings.yaml, countries.yaml (EU-27 reference data)
data/              ground truth, manual additions
output/            the calendar (xlsx + csv), change log, review list
src/elections_calendar/
  pipeline.py      orchestration          merge.py   grouping, dates, run-to-run updates
  sources/         wikipedia, wikidata, cor_pdf, llm_openai, manual
  wikidata.py      Wikidata client, election classifier, region gazetteer
  dates.py         date-text parser        classify.py  keyword rules
  store.py         CSV/Excel I/O           evaluate.py  ground-truth comparison
tests/             unit tests (offline)
```
