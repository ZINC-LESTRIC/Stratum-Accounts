# Deploy ReconAI — GitHub + Railway + Vercel

## Architecture

| Piece        | Platform  | What runs                          |
|--------------|-----------|------------------------------------|
| Backend API  | Railway   | FastAPI + Postgres                 |
| Frontend UI  | Vercel    | Static `index.html` (SPA)          |
| Source code  | GitHub    | This monorepo                      |

```
Browser (Vercel)
    │  HTTPS + CORS
    ▼
FastAPI (Railway) ──► Postgres (Railway)
```

---

## 1. Push to GitHub

```bash
cd reconai
git init
git add .
git commit -m "ReconAI V4 — ready for Railway + Vercel"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/reconai.git
git push -u origin main
```

Create the repo first at github.com (public or private).

---

## 2. Backend on Railway

1. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub** → select `reconai`.
2. Add a **Postgres** plugin (Railway → + New → Database → PostgreSQL).
3. In the **service** variables, set:

| Variable | Value |
|----------|--------|
| `DATABASE_URL` | *(auto from Postgres plugin — use the Reference variable)* |
| `RECONAI_JWT_SECRET` | long random string (e.g. `openssl rand -hex 32`) |
| `RECONAI_AUTO_CREATE_SCHEMA` | `true` |
| `CORS_ORIGINS` | `https://YOUR-VERCEL-DOMAIN.vercel.app` (update after Vercel deploy) |
| `LLM_API_KEY` | *(optional)* your OpenAI-compatible key |
| `LLM_BASE_URL` | `https://api.openai.com/v1/chat/completions` |
| `LLM_MODEL` | `gpt-4.1-mini` |

4. Railway will detect `Dockerfile` / `railway.toml` and deploy.
5. Copy the public URL, e.g. `https://reconai-production.up.railway.app`.

Health check: `https://YOUR-RAILWAY-URL/health` → `{"status":"ok",...}`

---

## 3. Frontend on Vercel

### Option A — Static from this repo (recommended)

1. Go to [vercel.com](https://vercel.com) → **Add New Project** → import the same GitHub repo.
2. Configure:
   - **Root Directory**: leave default (repo root)
   - **Framework Preset**: Other
   - **Build Command**: leave empty
   - **Output Directory**: `static`
3. Add Environment Variable (optional, for build-time injection):
   - Not required if you use `config.js` method below.
4. Deploy.

### Point frontend to Railway API

After both are live, open the Vercel site and run once in browser console:

```js
localStorage.setItem('apiBase', 'https://YOUR-RAILWAY-URL.up.railway.app');
location.reload();
```

Or edit `static/index.html` and set near the top of the script:

```js
window.RECONAI_API_BASE = 'https://YOUR-RAILWAY-URL.up.railway.app';
```

Then commit & redeploy Vercel.

### Update CORS on Railway

Set:

```
CORS_ORIGINS=https://your-app.vercel.app,https://your-app-git-main.vercel.app
```

Redeploy Railway (or just update env — Railway restarts).

---

## 4. Checklist

- [ ] GitHub repo pushed
- [ ] Railway: Postgres + FastAPI service running
- [ ] `/health` returns ok
- [ ] Vercel: static site deployed
- [ ] `apiBase` / `RECONAI_API_BASE` points to Railway
- [ ] `CORS_ORIGINS` includes Vercel domain
- [ ] Register a user, create business, post a sale

---

## Local development (unchanged)

```bash
export RECONAI_JWT_SECRET='dev-secret'
export RECONAI_AUTO_CREATE_SCHEMA='true'
uvicorn app.main:app --reload
# open http://127.0.0.1:8000  (API + UI same origin)
```

For frontend-only local with remote API:

```js
localStorage.setItem('apiBase', 'https://your-railway-url.up.railway.app');
```

---

## Notes

- AI remains **read-only**. Never give the LLM write access.
- Production: set `RECONAI_AUTO_CREATE_SCHEMA=false` after first successful schema create, and manage migrations properly later.
- JWT secret must be strong and private.
- Free tiers: Railway + Vercel are enough for testing; upgrade when you have real traffic.
