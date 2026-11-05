# CounterTalk

**Your Field Sales reps hear the market at every shop. AI turns it into action for the right team.**

## Try the demo

- **Live app:** https://countertalk.up.railway.app 
- **To View Demo Sign in:** https://countertalk.up.railway.app/signin
> (Sign in instructions are provided on the page or below in read me)
- **On your own machine:** follow [INSTALL.md](INSTALL.md) and load the demo data.

Field sales reps at FMCG companies visit neighbourhood shops all day, and what shopkeepers tell them gets lost: late credit notes, short deliveries, competitor offers, requests for new products. CounterTalk records the visit and turns it into routed work:

1. **It transcribes the conversation,** in Hindi, English, Hinglish or Marathi, and tells the speakers apart.
2. **It pulls out each finding,** backed by the shopkeeper's exact words. Every quote is checked against the transcript, and tapping ▶ plays that moment of the recording.
3. **It sends each finding to the team that can act on it,** such as finance, distribution or trade marketing. The team works it in its inbox, and when it's fixed, the rep sees the line to pass on at the next visit.

**The AI features, in brief** (Meta models only; details [below](#meta-models-and-where-each-is-used)):
- **Transcription with speaker separation:** Muse Voice Transcribe (`muse-voice-transcribe-1.0`), biased towards the visit's languages and given the company's brand names.
- **Muse Spark (`muse-spark-1.3`):**
  - **speaker roles:** which voice is the rep and which the shopkeeper;
  - **extraction:** each finding with its category, severity, products, suggested team and the shopkeeper's quotes;
  - **quote repair,** when a quote doesn't match the transcript;
  - **issue merge suggestions,** for problems that share a cause;
  - **summaries:** each team's and the whole org's, by week or month;
  - **Ask:** a question in Hindi, Marathi or English, turned into the inbox's filters;
  - **pre-visit briefs:** where a shop stands before the next visit.
- **The AI suggests, code decides:** every answer comes back in a strict schema, and code checks the quotes, product links, citations and filters before anything is shown.

Built and tried for FMCG. The same playbook fits other goods sold through neighbourhood shops (groceries, general merchandise and apparel), because its issue categories and routing are generic to selling through shops: a new business needs only its own **org pack**, the data files with its catalog, teams and routing, and no code changes. Pharma and agri products would need some tuning of the playbook (their issue categories and rules) in future. Creating a company's org, shops and users is still script work today.


Every account's password is `metahackathon`.

| Account | Role | What you'll see |
|---|---|---|
| `rep@example.com` | Field sales rep | Visits in Hindi, Hinglish and English |
| `rep2@example.com` | Field sales rep | Visits in English and Marathi |
| `rep3@example.com` | Field sales rep | Visits in English only |
| `admin@example.com` | Admin | Everything a manager sees, plus the catalog |
| `manager@example.com` | Manager | Every team's issues, summaries, Ask and briefs |
| `finance-member@example.com` | Team member (Finance) | The Finance team's inbox |
| `sales-ops-member@example.com` | Team member (Sales operations) | The Sales operations inbox |
| `distribution-member@example.com` | Team member (Distribution) | The Distribution team's inbox |

## What it costs

Prices are Meta's list prices of 1 Nov 2026: Muse Voice Transcribe at $0.18 an audio hour, and Muse Spark at $1.25 per million input tokens ($0.15 when cached) and $4.25 per million output tokens. The averages below come from the app's own records: every Muse Spark call is stored with its token counts, and every visit with the audio seconds it was billed.

| AI step | When it runs | Average cost |
|---|---|---|
| Transcription (Muse Voice Transcribe) | Every visit | About $0.008 for a 2–3 minute visit |
| Speaker roles (Muse Spark) | Every visit | About $0.003 |
| Extraction (Muse Spark) | Every visit | About $0.013 |
| Quote repair (Muse Spark) | Only when a quote doesn't match the transcript | About $0.004 |
| Issue merge suggestions (Muse Spark) | With a summary | About $0.003 |
| Summary (Muse Spark) | Each team and the org, weekly or monthly | About $0.016 |
| Ask (Muse Spark) | Each question | About $0.004 |
| Pre-visit brief (Muse Spark) | When someone asks for one | About $0.007 |

**A visit, end to end, costs about $0.02–0.03 (about ₹2),** roughly 1 US cent per minute of conversation. A rep who records 30 visits a day, 25 days a month, costs about $20 (about ₹1,700) a month. Summaries, Ask and briefs add cents, not dollars.

**Why it's affordable:**
- **Meta's prices are low.** An hour of audio costs $0.18 to transcribe, and Muse Spark's cached input costs about a tenth of its normal input price.
- **One extraction call per visit.** The checks after it (quotes, catalog links, routing) are code, not more model calls.
- **The model writes words, code does the numbers.** Summary figures come from SQL, Ask returns filters rather than answers, and briefs are written only when asked for.
- **Repairs only on a miss.** Quote repair runs only when a quote doesn't match the transcript.
- **Pay per second of speech.** Transcription is billed by the audio second, and long visits are cut at pauses, so nothing is paid twice.
- **Cached prompts.** The repeated part of each prompt is billed at the cached rate.
- **Cost is visible.** Every call's tokens are recorded against its visit, summary or brief.

## This is demo data

- **PepsiCo India is example data.** Its brand and product names are public product information, as sold in India. PepsiCo is not a customer or partner of CounterTalk, and none of this data comes from it. There are no logos.
- **Everything else is made up:** the distributors, shops, people, schemes and conversations. The recordings are scripted role-plays voiced with generated voices.
- **It's loaded from [`demo/`](demo/)** by `backend/scripts/load_demo_data.py` (see [INSTALL.md](INSTALL.md)).

## Running it for your own company: what you need

- **An org pack** in `backend/org_packs/<id>/v1/`, with no code changes:
  - your brands and products (`skus.csv`) and your competitors (`competitors.csv`);
  - your schemes (`schemes.csv`) and distributors (`distributors.csv`);
  - your teams, what each handles, and the routing from each kind of finding to a team (`org_pack.yaml`), plus the languages your visits are in.

  Check it with `scripts/check_pack.py`, and load it into an org with `scripts/import_org_pack.py`. [`pepsico-india-demo`](backend/org_packs/pepsico-india-demo/v1/) is a complete example.
- **Your org, shops and users,** created by scripts today. `scripts/seed_dev.py` creates the demo org and its shops (to use it for another company, edit its constants), `scripts/add_user.py` adds a user, and `scripts/set_password.py` sets a password. There are no screens for these yet.
- **A Meta API key** (`MODEL_API_KEY`), for Muse Voice Transcribe and Muse Spark.
- **No recordings up front.** They build up as reps record or upload visits, and the inboxes, summaries, briefs and Ask fill in as visits are confirmed.

## How it works

1. **Consent first.** Before recording at a shop, the shopkeeper agrees through a consent link the rep shares, or on a paper form the rep marks. A refusal blocks recording at that shop, and the server enforces it.
2. **Record or upload.** The rep records the visit in the browser, which sends the audio in segments while it records, or uploads a recording made with another app.
3. **Transcribe.** A background worker converts the audio with ffmpeg, splits long visits into chunks, and sends them to Muse Voice Transcribe with speaker separation. Speech-to-text is biased towards the org's languages and given its brand names as keywords.
4. **Speaker roles.** Muse Spark decides which voice is the rep and which is the shopkeeper.
5. **Extract.** Muse Spark returns the findings in a strict JSON schema built from the shared playbook and the org pack. Each finding has a kind, category and severity, the products, competitors or schemes it's about, a suggested team, and the shopkeeper's quotes.
6. **Check in code.** Each quote is matched against the transcript (exactly, or fuzzily within a set score) and replaced with the transcript's own text. A finding with no matching quote is marked unverified: it's never routed, and it's left out when the visit is confirmed. Products and competitors are linked to the org's catalog through its aliases, and schemes to the pack's schemes; a name that matches nothing, or more than one item, goes to review instead of a guess. Routing combines the org pack's routing table with the model's suggestion.
7. **The rep reviews and confirms.** The rep can edit, add or remove findings, and plays each quote with ▶. Nothing reaches a team before the rep confirms.
8. **Teams work issues.** Confirmed findings group into issues by likely cause, across shops and reps. Each issue gets a due date from its severity. A team member takes it on, sets its status and, as its owner, can move the due date; resolving it needs a note for the shop. That note appears on the rep's **Fixed since the last visit** list.
9. **Over time:**
   - **Updates:** what got fixed, told back to the rep. For each shop, the problems fixed since anyone last visited it, each with the team's note to pass on ("Tell them: …"); the rep's own problems from this month that were fixed before that visit follow under a plain "Fixed". The rep's count for the month comes first: how many of the problems they raised are fixed. A shop's pre-visit brief also lists what was fixed since its last visit. No AI: the notes are the teams' own words.
   - **Summaries,** for each team and for the whole org, by week or month. The numbers come from SQL; Muse Spark writes the words, and code drops any sentence that cites nothing it was given or quotes a number that isn't in the summary.
   - **Ask:** a manager, admin or team member types a question in Hindi, Marathi or English. Muse Spark turns it into the inbox's filters, and code checks them and runs the query; the model never writes SQL.
   - **Briefs:** before a visit, a short brief of where a shop stands, once it has two confirmed visits. Code picks the facts, and Muse Spark writes the words when someone asks for it.

## Meta models, and where each is used

One Meta key (`MODEL_API_KEY`) covers both. It never leaves the server.

| Model | ID in the code | Used for |
|---|---|---|
| **Muse Voice Transcribe** | `muse-voice-transcribe-1.0` ([`stt/meta.py`](backend/countertalk/stt/meta.py)) | Transcribing every visit, with speaker separation (`DIARIZATION` mode), a language bias and catalog keywords |
| **Muse Spark** | `muse-spark-1.3` ([`llm/client.py`](backend/countertalk/llm/client.py)) | Speaker roles; extraction; repairing a quote that didn't match; summary narratives; issue merge suggestions; Ask; pre-visit briefs |

Every Muse Spark call, failed ones too, is recorded with its purpose and token counts, so its cost is known, but never with its prompt or answer. Speech-to-text's billed seconds are counted on each visit.

## How we evaluated the AI

Each AI step was checked against scripted role-plays before we relied on it, and the checks are in the repo.

- **Role-plays with answer keys:** scripted shop visits voiced with generated voices, each with a key of the findings a good extraction should return (category, severity, team and quote).
- **Speech-to-text:** [`compare_transcript.py`](backend/scripts/compare_transcript.py) compares a transcript with its script: whether each line lands on the right speaker, and which script each word comes back in. We used it to check the language bias and the brand-name keywords. It also showed that English words in Hindi speech come back in Devanagari, so the catalog has Devanagari aliases.
- **Extraction:** the Hinglish and Marathi role-plays are eval cases in [`backend/evals/`](backend/evals/), with their transcripts and speaker roles saved. [`run_eval.py`](backend/scripts/run_eval.py) re-runs the real extract job on them (the model, then the quote checks, catalog links and routing in code) and scores it against the key. Each result is saved under its prompt's hash, so every prompt change was compared with the one before.
- **A held-back set:** some cases were never used to change a prompt, only scored, to check that the prompt hadn't just learned the tuning cases. It held up: in every held-back case, each expected finding was found, nothing unexpected was added, and every quote matched the transcript.
- **Ask:** questions in English, Hindi and Marathi, each with the filters it should give, including an off-topic one that should be refused (`run_eval.py --ask`).
- **Summaries and merge suggestions:** made-up weeks of visits with planted patterns ([`tests/fixtures/patterns/`](backend/tests/fixtures/patterns/)). Tests check the counts, labels and ranks with no model; a paid run (`run_eval.py --patterns`) checks the merge suggestions and the narrative. A merge it missed led to a change in the merge prompt.

Without `--yes`, `run_eval.py` only prints what it would send and what it would cost.

## Architecture

```mermaid
flowchart LR
  subgraph browser["Browser"]
    web["Web app<br/>React + Vite"]
  end
  subgraph server["Server (one Docker image)"]
    api["API<br/>FastAPI"]
    worker["Worker<br/>background jobs"]
    files[("Audio files<br/>DATA_DIR")]
  end
  db[("Postgres")]
  stt["Meta Muse Voice Transcribe<br/>muse-voice-transcribe-1.0"]
  spark["Meta Muse Spark<br/>muse-spark-1.3"]

  web -- "audio segments, review, inbox" --> api
  api --> db
  api --> files
  worker --> db
  worker --> files
  worker -- "audio chunks" --> stt
  worker -- "roles, extraction, quote repair,<br/>summaries, merge suggestions" --> spark
  api -- "Ask, briefs" --> spark
```

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic and Postgres 17. The worker takes its jobs (transcribe, extract, summary, retention) from a Postgres table, so there's no extra queue to run.
- **Web:** React 19, React Router, Tailwind CSS 4 and Vite, served by the same FastAPI app.
- **Access:** sign-in with argon2-hashed passwords and an HTTP-only session cookie. Every API route on org data checks one access module's role rules first, every lookup is scoped to the user's org (another org's data is a 404), and audio is served only through an endpoint that checks access.
- **The AI suggests, code decides:** the models' answers come back in strict schemas, and code checks them (quotes, links, citations, filters) before anything is shown or stored as fact.
- **Repository:** [`backend/`](backend/) (the API, worker, migrations, org packs, playbook, eval cases, tests), [`web/`](web/) (the app and the landing page), [`demo/`](demo/) (the demo data) and the [`Dockerfile`](Dockerfile).