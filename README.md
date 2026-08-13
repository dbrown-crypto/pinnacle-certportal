# Pinnacle Risk Advisors — Self-Serve Certificate Portal

Clients log in, see their own policy, add a certificate holder, and get a
standard ACORD 25 issued and emailed instantly. Anything that needs additional
insured / waiver / special wording routes to your desk instead of auto-issuing.
Built for your stack: Supabase + Netlify + a small Python (PyMuPDF) service.

This is the **Path B** build — you own all of it, it runs through your existing
certificate-generation pipeline, and every E&O guardrail is explicit and logged.

---

## What's here

```
db/schema.sql            Postgres schema + Row-Level Security (run in Supabase)
backend/
  gate.py                The validation gate. The product. 12 tested scenarios.
  cert_generator.py      ACORD 25 fill + signature + branded fallback (PyMuPDF)
  main.py                FastAPI: server-side gate, issuance, delivery, audit, admin
  test_gate.py           Run this to prove the gate logic
  requirements.txt
frontend/
  index.html             Client portal (login → policy → add holder → issue)
  admin.html             Your back office (policies, kill switch, queue, audit)
```

## How it fits together

```
Client browser ──(anon key, RLS read-only)──> Supabase  (their own policy only)
      │
      └──(JWT)──> FastAPI /issue-certificate
                      │  1. verify JWT, confirm policy ownership
                      │  2. evaluate_gate()  ← THE control (server-side)
                      │  3a. auto_issue → PyMuPDF cert → sign → Storage → Resend email → audit
                      │  3b. route_to_agent → special_requests row → email you → audit
                      │  3c. block → audit (blocked) → reason to client
                      └──(service-role key, server-only)──> Supabase
```

The browser is never trusted. Hiding the "Additional Insured" checkbox is UX;
`gate.py` running server-side is the actual control.

---

## Setup

### 1. Supabase
1. Create a project. SQL editor → paste and run `db/schema.sql`.
2. Storage → create a **private** bucket named `certificates`.
3. Auth → create one login per insured (Authentication → Users → Add user).
   For each, insert a `clients` row with `id` = that user's UUID and the
   matching policy in `policies` (or do it from the admin page once it's live).
4. Add your own user to the admin allowlist (see `ADMIN_USER_IDS` below).
5. Grab from Settings → API: Project URL, `anon` key, `service_role` key,
   and the JWT secret.

### 2. Backend (FastAPI)
Runs anywhere that runs Python (Render, Railway, Fly, a small VPS — not a
serverless edge function; PyMuPDF needs a real runtime).

```bash
cd backend
pip install -r requirements.txt
python test_gate.py          # expect: all 12 scenarios pass
uvicorn main:app --host 0.0.0.0 --port 8000
```

Environment variables:

| Var | What |
|---|---|
| `SUPABASE_URL` | Project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-only. Never ship to the browser. |
| `SUPABASE_JWT_SECRET` | Verifies client sessions |
| `RESEND_API_KEY` | Transactional email (verify pinnacleriskad.com first) |
| `MAIL_FROM` | `Pinnacle Risk Advisors <certs@pinnacleriskad.com>` |
| `AGENT_NOTIFY_EMAIL` | `dbrown@pinnacleriskad.com` |
| `ADMIN_USER_IDS` | Your Supabase user UUID(s), comma-separated |
| `GHL_POLICY_SYNC_SECRET` | Long random bearer secret shared only with the GoHighLevel policy workflow |
| `ACORD25_TEMPLATE_PATH` | Path to your licensed ACORD 25 PDF (omit → branded sample) |
| `SIGNATURE_PNG_PATH` | Your authorized-rep signature image (transparent PNG) |
| `ALLOWED_ORIGINS` | Your Netlify URL(s) for CORS |

## GoHighLevel policy sync

`POST /api/integrations/ghl/policies` accepts a policy snapshot from a GHL
custom-object workflow. It authenticates with
`Authorization: Bearer $GHL_POLICY_SYNC_SECRET`, finds exactly one existing
portal customer by `client_email`, and idempotently creates or updates the
policy using the GHL policy record ID. It never creates a customer and never
issues a certificate.

The workflow JSON must send the GHL policy record ID, the associated portal
customer email, named insured, dates, producer block, insured address, coverage
and exact underwriting-company information. `self_serve_enabled` should be
explicit. Any non-active status automatically forces it off. Driver payloads
accept only first name, last name, and license state; DOB and license number are
rejected.

### 3. Frontend (Netlify)
Edit the CONFIG block at the top of **both** `index.html` and `admin.html`:
`SUPABASE_URL`, `SUPABASE_ANON_KEY`, `BACKEND_URL`. Drag the `frontend/`
folder into Netlify (or wire your existing GitHub→Netlify CI). `index.html` is
the client portal; `admin.html` is yours.

---

## Wiring in your real ACORD 25

The generator ships with a branded **sample** (watermarked) so the pipeline runs
today. For production:

1. Put your licensed ACORD 25 PDF somewhere the backend can read it; set
   `ACORD25_TEMPLATE_PATH`.
2. Discover its form-field names once:
   ```python
   from cert_generator import dump_field_names
   print(dump_field_names("acord25.pdf"))
   ```
3. Fill in `_build_field_map()` in `main.py` mapping stored data → those field
   names.
4. Export your signature as a transparent PNG; set `SIGNATURE_PNG_PATH`.

The signature is applied automatically **only because** the gate guarantees the
cert is a plain informational cert on verified-active, in-date coverage. If you
ever loosen the gate, revisit the auto-signature.

---

## The one Jenesis bridge (recommended)

You can't pull from Jenesis (no open API), but you can push. In JenesisLink /
Zapier, trigger on policy **cancellation or change** → call
`POST /admin/policies/{id}/status` (or write Supabase directly) to flip
`status`/`self_serve_enabled`. That shrinks the manual-staleness window — the
gap between a policy going bad and you remembering to update the row.

---

## Before go-live (E&O)

- The gate restricts self-serve to informational ACORD 25 only. Confirmed in
  `test_gate.py`.
- `data_current_as_of` prints on every cert; keep it honest when you key data.
- The audit log records every issue **and every block** — your defensibility.
- Per-client kill switch is in the admin page.
- Not legal advice: a five-minute call to your E&O carrier to confirm they're
  fine with insured-initiated, gated, logged, informational-only issuance is
  worth making. The architecture is built to make that an easy conversation.
