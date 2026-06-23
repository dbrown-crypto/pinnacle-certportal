# BUILD_BRIEF.md — Pinnacle Self-Serve Certificate Portal

**For:** Claude Cowork or Claude Code. Read this top to bottom, then execute the
tasks in order. Stop at any **[HUMAN]** checkpoint and ask Derrick — those need a
secret, an account, or a judgment call you can't make autonomously.

**Goal:** Stand up the self-serve certificate portal already built in this
folder against Derrick's live Supabase project, get it deployed, seed one test
policy, and prove a certificate issues end to end — without any real client
touching it yet.

**What already exists in this folder (built and tested — do not rewrite):**
- `db/schema.sql` — Postgres schema + RLS (gate-tested data model)
- `backend/gate.py` — the validation gate (12/12 scenarios pass)
- `backend/cert_generator.py` — PyMuPDF ACORD 25 fill + signature + fallback
- `backend/main.py` — FastAPI: server-side gate, issuance, delivery, audit, admin
- `backend/test_gate.py` — run this first to confirm the gate is intact
- `frontend/index.html` — client portal
- `frontend/admin.html` — Derrick's back office

**Known project facts:**
- Supabase project ref: `dvjnnhwuamwcvqoxmske` (region us-east-2, healthy, free tier)
- Existing GitHub repo on the project: `dbrown-crypto/pinnacleriskad` (the website)
- Backend CANNOT run on Netlify (PyMuPDF needs a real Python runtime) → deploy
  the FastAPI service to Render/Railway/Fly; deploy only the frontend to Netlify.

---

## [HUMAN] Decision 0 — where does the portal live?
Before touching git, Derrick decides:
- **(a)** New repo `dbrown-crypto/pinnacle-certportal` (recommended — keeps the
  portal separate from the marketing website), or
- **(b)** A `/certportal` subfolder inside the existing `pinnacleriskad` repo.

Do not proceed until this is chosen. Default to (a) if Derrick is unsure.

---

## Task 1 — Verify the build is intact (no secrets needed)
```bash
cd backend
pip install -r requirements.txt
python test_gate.py        # MUST print: All gate scenarios behaved as designed.
python -c "import main"    # MUST import with no error (13 routes)
```
If either fails, stop and report — do not "fix" the gate logic without flagging.

## Task 2 — Apply the database schema to the live project
Use the Supabase CLI (preferred) or psql with the connection string.
```bash
# CLI path:
supabase link --project-ref dvjnnhwuamwcvqoxmske   # [HUMAN] provides DB password
supabase db push   # or: psql "$SUPABASE_DB_URL" -f ../db/schema.sql
```
Then confirm the tables exist: `clients, policies, issued_certificates,
special_requests, audit_log`, and that RLS is **enabled** on all five.

## Task 3 — Create the private storage bucket
In Supabase Storage, create a **private** bucket named `certificates`. (CLI or
ask Derrick to click it — either is fine. Do NOT make it public.)

## Task 4 — Scaffold the backend deploy
Create the deploy config for the chosen host (e.g. `render.yaml` or a Railway
config) for the `backend/` service running:
`uvicorn main:app --host 0.0.0.0 --port $PORT`
Commit it. Do not deploy yet — env vars come first.

## [HUMAN] Task 5 — Secrets (Derrick supplies; never invent or commit these)
Set these as environment variables on the backend host:
- `SUPABASE_URL` = https://dvjnnhwuamwcvqoxmske.supabase.co
- `SUPABASE_SERVICE_ROLE_KEY`  [HUMAN — Settings → API]
- `SUPABASE_JWT_SECRET`        [HUMAN — Settings → API]
- `RESEND_API_KEY`             [HUMAN — after verifying pinnacleriskad.com in Resend]
- `MAIL_FROM` = "Pinnacle Risk Advisors <certs@pinnacleriskad.com>"
- `AGENT_NOTIFY_EMAIL` = dbrown@pinnacleriskad.com
- `ADMIN_USER_IDS`             [HUMAN — Derrick's Supabase user UUID, see Task 7]
- `ALLOWED_ORIGINS`            [the Netlify URL, set after Task 6]
- Leave `ACORD25_TEMPLATE_PATH` / `SIGNATURE_PNG_PATH` UNSET for now → branded
  sample. (Real template is a later human step.)

Then deploy the backend and confirm `GET /healthz` returns `{"ok": true}`.

## Task 6 — Deploy the frontend
In BOTH `frontend/index.html` and `frontend/admin.html`, fill the CONFIG block:
`SUPABASE_URL`, `SUPABASE_ANON_KEY` [HUMAN — anon key], `BACKEND_URL` (the
deployed backend URL). Deploy the `frontend/` folder to Netlify. Capture the
Netlify URL and go back and set `ALLOWED_ORIGINS` on the backend to it.

## [HUMAN] Task 7 — Create Derrick's admin login + one test policy
1. Supabase → Authentication → add a user for Derrick. Copy its UUID into
   `ADMIN_USER_IDS` (Task 5) and redeploy the backend.
2. Add a `clients` row: `id` = that UUID, `insured_name` = a test insured.
3. Open `admin.html`, sign in as Derrick, and add ONE real-but-test policy
   (use one of Derrick's own DOTs, status `active`, honest dates + coverages).

## Task 8 — Smoke test the whole flow (no real client involved)
1. In Supabase Auth, create a second test user = the "insured." Add a matching
   `clients` row and point the Task 7 policy's `client_id` at it.
2. Open `index.html`, sign in as that insured, add a certificate holder, issue.
3. Confirm: PDF downloads, an `issued_certificates` row appears, an `audit_log`
   row with `AUTO_ISSUE_OK` appears, and (if Resend is set) the email arrives.
4. Now test the guardrails: try issuing with "Additional Insured" checked →
   expect `routed` + a `special_requests` row + an email to Derrick. Flip the
   policy to `cancelled` in admin → expect the client issue to `block` and log
   `BLOCK_STATUS_CANCELLED`.

Report the results of Task 8 to Derrick. **Done = all four checks in step 3 and
both guardrails in step 4 behave as described.**

---

## [HUMAN] After go-live works with the sample — the real-cert switch
These stay with Derrick; the agent can prep around them but not complete them:
1. Provide the licensed ACORD 25 PDF; run `cert_generator.dump_field_names()` on
   it; fill `_build_field_map()` in `main.py`; set `ACORD25_TEMPLATE_PATH`.
2. Provide the authorized-rep signature PNG; set `SIGNATURE_PNG_PATH`.
3. Make the five-minute E&O carrier call confirming they're fine with gated,
   logged, informational-only insured-initiated issuance.
4. Optional: wire a Jenesis/Zapier trigger on policy cancellation → call
   `POST /admin/policies/{id}/status` to flip `status` automatically.

## Guardrails for the agent
- Never commit secrets, keys, the service-role key, or the JWT secret.
- Never weaken `gate.py` to make a test pass — flag instead.
- Never make the `certificates` storage bucket public.
- Never email a real (non-test) holder during this build.
