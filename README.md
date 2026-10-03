# ReconAI V4 (Improved)

ReconAI is a deterministic accounting and reconciliation application with a read-only AI analyst layered over verified financial data.

### Recent improvements
- Fixed frontend navigation bug and rewritten SPA with cleaner UX
- Proper financial statements rendering (P&L, Balance Sheet, Cash Flow)
- Inventory adjustment UI
- Richer cash reconciliation panel
- Toast notifications instead of alerts
- Stronger AI verified-context builder (cash/inventory/bank/integrity)
- Better form labels, empty states, and responsive polish
- Currency-aware display throughout the UI

## Accounting safety

- The backend is the source of truth.
- Journal entries cannot be posted unless debits equal credits.
- Financial totals are recalculated server-side.
- Credit sales use Accounts Receivable.
- Credit purchases and credit expenses use Accounts Payable.
- Opening inventory creates an Inventory / Owner Capital entry.
- Inventory uses deterministic moving-average costing.
- Corrections use reversal entries rather than destructive deletion.
- AI has no database write tools and receives verified structured context only.

## Included

- JWT authentication and session revocation
- Business-level isolation and role permissions
- Multiple-business selection through the `X-Business-ID` header
- Business setup and Chart of Accounts
- Products, customers, suppliers
- Cash/bank/card/mobile-wallet/credit sales
- Cash/bank/credit purchases
- Expenses
- Inventory movements and physical counts
- Moving-average inventory costing and COGS
- Inventory adjustments with accounting entries
- Cash reconciliation
- Bank transaction entry/import/matching
- P&L, Balance Sheet and Cash Flow
- Accounting-integrity checks
- Audit trail
- Journal reversal workflow
- Idempotency protection
- Deterministic anomaly detection
- Read-only natural-language AI analyst
- Health/readiness endpoints
- Docker + PostgreSQL deployment configuration
- Automated integration tests

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export RECONAI_JWT_SECRET='use-a-long-random-secret'
export RECONAI_AUTO_CREATE_SCHEMA='true'
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.

## Deploy (GitHub + Railway + Vercel)

Full guide: **[DEPLOY.md](./DEPLOY.md)**

1. Push this repo to GitHub  
2. **Railway** → backend (Dockerfile) + Postgres plugin  
3. **Vercel** → deploy the `static/` folder as the frontend  
4. Set `CORS_ORIGINS` on Railway to your Vercel domain  
5. Point the frontend at Railway (`localStorage.apiBase` or `window.RECONAI_API_BASE`)

## PostgreSQL / Docker

```bash
docker compose up --build
```

For production, replace example secrets and set `RECONAI_AUTO_CREATE_SCHEMA=false` after the first successful schema creation.

## AI

Set `LLM_API_KEY`. The optional provider is OpenAI-compatible and configurable with `LLM_BASE_URL` and `LLM_MODEL`.

The AI endpoint is strictly read-only. If the provider fails, ReconAI returns deterministic fact-based output instead of fabricating an answer.

## Tests

```bash
pytest -q
```

Current suite: **8 passing tests**, including the required Rs 100,000 → Rs 105,000 → Rs 103,500 cash discrepancy scenario, accounting integrity, credit liability mapping, opening inventory accounting, JWT revocation, tenant isolation, reversal and idempotency.
