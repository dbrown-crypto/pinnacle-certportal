-- ============================================================================
-- Pinnacle Risk Advisors — Self-Serve Certificate Portal
-- Supabase / Postgres schema + Row-Level Security
--
-- Run this in the Supabase SQL editor. It creates the data model and, more
-- importantly, the RLS policies that make a client able to read ONLY their own
-- policy — enforced in the database, not in the UI. The UI hiding things is
-- convenience; THIS is the control an E&O reviewer will ask about.
--
-- Trust model:
--   * The client (browser) uses the Supabase ANON key. RLS confines it to the
--     logged-in user's own rows, read-only.
--   * The backend (FastAPI) uses the SERVICE_ROLE key. It bypasses RLS and is
--     the ONLY thing allowed to write issued_certificates / audit_log. Clients
--     can never insert a certificate row directly — issuance only happens after
--     the server-side gate passes.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- clients: one row per insured login. id == auth.users.id (Supabase Auth).
-- ---------------------------------------------------------------------------
create table if not exists public.clients (
    id            uuid primary key references auth.users (id) on delete cascade,
    insured_name  text not null,
    email         text not null,
    created_at    timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- policies: the hand-keyed source of truth the portal reads from.
-- status + self_serve_enabled are the control fields the gate checks.
-- coverages is jsonb: { "auto_liability": 1000000, "cargo": 100000, ... }
-- ---------------------------------------------------------------------------
create table if not exists public.policies (
    id                  uuid primary key default gen_random_uuid(),
    client_id           uuid not null references public.clients (id) on delete cascade,
    named_insured       text not null,
    usdot               text,
    status              text not null default 'active'
                        check (status in ('active','cancelled','pending','expired')),
    self_serve_enabled  boolean not null default true,
    effective_date      date not null,
    expiration_date     date not null,
    producer_block      text not null,           -- prints in PRODUCER box
    insured_address     text not null,
    coverages           jsonb not null default '{}'::jsonb,   -- line -> limit
    carriers            jsonb not null default '[]'::jsonb,   -- [{line,carrier,policy_number,eff,exp,limits}]
    data_current_as_of  date not null default current_date,
    updated_at          timestamptz not null default now()
);

create index if not exists policies_client_idx on public.policies (client_id);

-- ---------------------------------------------------------------------------
-- issued_certificates: every cert that actually went out. The audit backbone.
-- coverage_snapshot freezes EXACTLY what printed, so a later dispute can be
-- answered with the data state at issue time.
-- ---------------------------------------------------------------------------
create table if not exists public.issued_certificates (
    id                  uuid primary key default gen_random_uuid(),
    cert_number         text not null unique,
    policy_id           uuid not null references public.policies (id),
    client_id           uuid not null references public.clients (id),
    holder_name         text not null,
    holder_address      text not null,
    holder_email        text,
    description_of_ops   text,
    coverage_snapshot   jsonb not null,          -- frozen copy of what printed
    pdf_path            text,                    -- Supabase Storage path
    issued_by           text not null default 'client_self_serve',
    issued_at           timestamptz not null default now()
);

create index if not exists certs_client_idx on public.issued_certificates (client_id);
create index if not exists certs_policy_idx on public.issued_certificates (policy_id);

-- ---------------------------------------------------------------------------
-- special_requests: holder needs AI / waiver / P&NC / special wording.
-- The gate routes these here; NOTHING is issued. Derrick works the queue.
-- ---------------------------------------------------------------------------
create table if not exists public.special_requests (
    id                  uuid primary key default gen_random_uuid(),
    policy_id           uuid not null references public.policies (id),
    client_id           uuid not null references public.clients (id),
    holder_name         text not null,
    holder_address      text not null,
    holder_email        text,
    requested_wording   jsonb not null default '[]'::jsonb,
    notes               text,
    status              text not null default 'open'
                        check (status in ('open','in_progress','sent','declined')),
    created_at          timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- audit_log: EVERY decision, including blocks. Your defensibility record.
-- ---------------------------------------------------------------------------
create table if not exists public.audit_log (
    id            bigint generated always as identity primary key,
    client_id     uuid references public.clients (id),
    policy_id     uuid references public.policies (id),
    action        text not null,                 -- gate route: auto_issue / route_to_agent / block
    audit_codes   jsonb not null default '[]'::jsonb,
    reasons       jsonb not null default '[]'::jsonb,
    holder_name   text,
    occurred_at   timestamptz not null default now()
);

create index if not exists audit_client_idx on public.audit_log (client_id);

-- ============================================================================
-- Row-Level Security
-- ============================================================================
alter table public.clients              enable row level security;
alter table public.policies             enable row level security;
alter table public.issued_certificates  enable row level security;
alter table public.special_requests     enable row level security;
alter table public.audit_log            enable row level security;

-- clients: a user can read only their own client row.
create policy clients_self_read on public.clients
    for select using (id = auth.uid());

-- policies: a user can read only policies that belong to them. Read-only.
-- No insert/update/delete policy for clients => the anon key cannot modify
-- policy data. Only the service-role backend (which bypasses RLS) writes.
create policy policies_self_read on public.policies
    for select using (client_id = auth.uid());

-- issued_certificates: a user can read their own certificate history.
-- They CANNOT insert — issuance is server-only.
create policy certs_self_read on public.issued_certificates
    for select using (client_id = auth.uid());

-- special_requests: a user can read their own requests (to see status).
create policy special_self_read on public.special_requests
    for select using (client_id = auth.uid());

-- audit_log: clients get NO direct access. (No policy = deny for anon.)
-- Only the service-role backend reads/writes it.

-- ============================================================================
-- Helper view the backend uses to derive an "effective status" that already
-- accounts for the date window, so even a stale 'active' flag on an expired
-- policy resolves correctly. (The gate also checks this; defense in depth.)
-- ============================================================================
create or replace view public.policies_effective as
select
    p.*,
    case
        when p.status <> 'active'              then p.status
        when current_date < p.effective_date   then 'pending'
        when current_date > p.expiration_date  then 'expired'
        else 'active'
    end as effective_status
from public.policies p;
