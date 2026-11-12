# Job Search Agent

An AI agent that finds senior roles for me and ranks them against my resume every morning.

I built this in August 2026, after leaving Flipkart, to solve my own problem. I was looking for
Strategy, Ops and GM roles across 9 companies, and checking each careers page by hand every day was slow.
Most listings also turned out to be the wrong level or the wrong function. I'm an operator, not a
software engineer, so I built this in Python with Claude as my pair programmer.

## What it does

For each company in `companies.json`, the agent:

1. **Searches.** It uses Claude with live web search to find roles that are open right now at the
   target level (AD, Director, GM, Head of, VP, COO), across careers sites, Lever, Greenhouse
   and LinkedIn.
2. **Filters.** It drops anything posted more than 14 days ago and anything below the seniority
   set for that company. For example, at Flipkart and Myntra, where I was already an AD, it shows
   only Director and above.
3. **Reads the JD.** It fetches the full job description where the page can be read.
4. **Scores fit.** It gives each role a 0–100 score against my resume, with a one-line reason.
   That's the same judgement I would make by reading the JD myself, done in seconds.
5. **Remembers.** It caches every role it has scored, so a daily run only spends money on new
   postings.

You get a ranked CSV, with your best-fit roles at the top, every weekday at 8 AM.

## Sample output

| Score | Company | Role | Why |
|---|---|---|---|
| 78 | Meesho | Head – ValMo Operations | Strong P&L, supply-chain and scaling background; gap on franchise-partner logistics metrics |
| 72 | Uber | Senior Country Manager, Uber for Business India | Right seniority and GTM/P&L fit; lighter on enterprise B2B sales |
| 72 | Meesho | AD – Sort Center Design & Ops | Has built fulfilment infrastructure from scratch; less direct sort-centre experience |
| 58 | Meesho | AD – CEO Office | Strategy fit, but the resume is weaker on board and corporate-strategy work |

The full example is in [`sample_output.csv`](sample_output.csv).

## Design decisions

The first version cost about **$2.70 per run** for 5 companies. That's too much to run daily, so I
redesigned it:

- **Two models.** A stronger model handles web search, where quality matters. A cheaper, faster
  model (Haiku) handles scoring.
- **Batched scoring.** All of a company's new roles are scored in a single call instead of one call
  per role.
- **Prompt caching.** The resume is sent once as a cached block instead of being billed again on
  every call.
- **A seen-roles cache.** A role that's still open is never scored twice, so repeat runs cost close
  to nothing.
- **Hard caps.** At most 15 roles per company, so one large employer can't drive up the cost.

These changes bring a run to an estimated **under $1**, and scheduled re-runs cost less still.

## Run it yourself

```bash
pip install anthropic requests beautifulsoup4
cp .env.example .env               # then paste your Anthropic API key into .env
cp resume.example.txt resume.txt   # then paste your resume
# edit companies.json to choose your target companies
set -a; source .env; set +a
python3 job_search_agent.py
```

To run it every weekday at 8 AM, add `run_job_search.sh` to cron:

```
0 8 * * 1-5 /bin/bash "/path/to/job-search-agent/run_job_search.sh" >> run.log 2>&1
```

### companies.json

```json
[
  { "name": "Uber" },
  { "name": "Google",  "level": "manager_plus" },
  { "name": "Myntra",  "level": "director_plus" }
]
```

The `level` setting is optional. Leave it out to search from AD to COO. Use `manager_plus` to include
Manager and Senior Manager, or `director_plus` to search only Director and above.

## Limitations

- The agent only finds roles that are publicly indexed. Always click through and check a role
  before you apply.
- Some careers sites, such as Workday, load their pages with JavaScript that this script can't read.
  Roles from those sites are scored on title, level and location only, and are flagged
  `title_only`.
