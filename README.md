# Poc-Ideathon-black_sheep_squad
Created for Ideathon

## PipelineIQ — your AI data engineer

PipelineIQ is an interactive assistant (powered by Google Gemini) that you talk to like a
data engineer. It can **build**, **fix**, and **change** SQLite data pipelines, and **answer
analysis questions** about your data.

### Setup
1. `cd PipelineIQ && pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and set a real `GEMINI_API_KEY` (starts with `AQ.`).
3. Seed the demo warehouse: `python src/setup_db.py`

### Talk to it
Run `python src/app.py` to open the interactive prompt, then ask for anything:

| You say | What happens |
| --- | --- |
| `merge users and orders with product details into a customer summary` | **Build** — designs an ETL plan, you confirm, it runs and is saved |
| `which user has spent the most in total?` | **Analyze** — runs a read-only query and shows the answer |
| `change customer_orders to also include product category` | **Change** — reloads the saved pipeline and updates it |
| `fix the customer_orders pipeline` | **Fix** — re-runs it and self-heals SQL errors via the model |
| `list` | Show saved pipelines and the current schema |

Built pipelines are persisted under `data/pipelines/` so they can be listed, fixed, and
changed later. Pipelines self-heal: on a SQL error the plan + error are sent back to the
model for a corrected plan (up to `MAX_REPAIR_ATTEMPTS` times).

A request can still be passed as a one-shot arg — `python src/app.py "build ..."` — after
which it drops into the interactive prompt.
