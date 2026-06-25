#!/usr/bin/env node
// enroll_customer.mjs — Pinnacle Risk Advisors
// Enrolls a customer into Supabase Auth + clients + policies.
//
// Usage:
//   node enroll_customer.mjs honkhorn --dry-run   (prints rows, no writes)
//   node enroll_customer.mjs honkhorn             (real run — prints temp password)
//
// Reuse for the next customer: copy the honkhorn block, give it a new key,
// fill in the data, and add the key to CUSTOMERS below.
//
// Requirements: npm install @supabase/supabase-js dotenv
// Reads SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY from .env (never commit .env).

import { createClient } from '@supabase/supabase-js';
import { randomBytes, randomUUID } from 'crypto';
import { config } from 'dotenv';

config();   // load .env

// ---------------------------------------------------------------------------
// CUSTOMER DATA BLOCKS
// ---------------------------------------------------------------------------

const TODAY = new Date().toISOString().slice(0, 10);   // YYYY-MM-DD

const CUSTOMERS = {

  honkhorn: {
    login_email: 'Braxton.Frank@yahoo.com',
    client: {
      insured_name: 'Honk Horn Trucking LLC',
      email:        'Braxton.Frank@yahoo.com',
    },
    policy: {
      named_insured:      'Honk Horn Trucking LLC',
      insured_address:    '5 Swint St, Newnan, GA 30263',
      usdot:              '4418897',
      mc_number:          '1737009',
      status:             'active',
      self_serve_enabled: true,
      effective_date:     '2026-06-13',
      expiration_date:    '2027-06-13',
      data_current_as_of: TODAY,
      producer_block:     'Pinnacle Risk Advisors LLC\n2700 Cumberland Pkwy SE, Ste 410, Atlanta, GA 30339\n(943) 239-3439  certs@pinnacleriskad.com',
      coverages: {
        auto_liability:    1000000,
        general_liability: 1000000,
        cargo:             100000,
      },
      carriers: [
        {
          eff: '06/13/26', exp: '06/13/27',
          line: 'Auto Liability',
          naic: '10464',
          autos: ['SCHEDULED'],
          limits: { CSL: 1000000 },
          carrier: 'Canal Insurance',
          policy_number: 'CT7212838033-1',
        },
        {
          eff: '06/13/26', exp: '06/13/27',
          line: 'Commercial General Liability',
          naic: '10464',
          limits: {
            'EACH OCCURRENCE':           1000000,
            'GENERAL AGGREGATE':         2000000,
            'DAMAGE TO RENTED PREMISES': 100000,
            'MED EXP':                   5000,
            'PERSONAL & ADV INJURY':     1000000,
            'PRODUCTS COMP/OP AGG':      2000000,
          },
          carrier: 'Canal Insurance',
          policy_number: 'CT7212838033-1',
        },
        {
          eff: '06/13/26', exp: '06/13/27',
          line: 'Motor Truck Cargo',
          naic: '10464',
          limits: { Limit: 100000 },
          deductible: 2500,
          carrier: 'Canal Insurance',
          policy_number: 'CT7212838033-1',
        },
      ],
      drivers:  [{ last: 'BRAXTON', first: 'FRANK', lic_state: 'GA' }],
      trailers: [{ vin: '1XKYDP8X3FJ422580', value: 35000, description: '2015 Kenworth T680' }],
    },
  },

  // ---------------------------------------------------------------------------
  // 40GRAND STUB — fill in when policy is bound, then run:
  //   node enroll_customer.mjs 40grand --dry-run
  // ---------------------------------------------------------------------------
  // '40grand': {
  //   login_email: 'CONTACT@EMAIL.com',
  //   client: {
  //     insured_name: '40Grand Logistics LLC',
  //     email:        'CONTACT@EMAIL.com',
  //   },
  //   policy: {
  //     named_insured:      '40Grand Logistics LLC',
  //     insured_address:    'ADDRESS',
  //     usdot:              'USDOT',
  //     mc_number:          'MC',
  //     status:             'active',
  //     self_serve_enabled: true,
  //     effective_date:     'YYYY-MM-DD',
  //     expiration_date:    'YYYY-MM-DD',
  //     data_current_as_of: TODAY,
  //     producer_block:     'Pinnacle Risk Advisors LLC\n2700 Cumberland Pkwy SE, Ste 410, Atlanta, GA 30339\n(943) 239-3439  certs@pinnacleriskad.com',
  //     coverages: { auto_liability: 0, general_liability: 0, cargo: 0 },
  //     carriers:  [],
  //     drivers:   [],
  //     trailers:  [],
  //   },
  // },

};

// ---------------------------------------------------------------------------
// HELPERS
// ---------------------------------------------------------------------------

function genTempPassword() {
  // 16 bytes -> ~22 base64url chars; suffix guarantees mixed-case + digit
  const raw = randomBytes(16).toString('base64url');
  return raw.slice(0, 20) + 'Pk3';
}

function bail(msg) {
  console.error('\nERROR:', msg);
  process.exit(1);
}

// ---------------------------------------------------------------------------
// MAIN
// ---------------------------------------------------------------------------

async function main() {
  const [, , customerKey, flag] = process.argv;
  const dryRun = flag === '--dry-run';

  if (!customerKey) bail('Usage: node enroll_customer.mjs <customer_key> [--dry-run]');

  const account = CUSTOMERS[customerKey];
  if (!account) bail(`Unknown customer key "${customerKey}". Known keys: ${Object.keys(CUSTOMERS).join(', ')}`);

  const SUPABASE_URL              = process.env.SUPABASE_URL;
  const SUPABASE_SERVICE_ROLE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY;

  if (!SUPABASE_URL || !SUPABASE_SERVICE_ROLE_KEY) {
    bail('Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in .env');
  }

  const adminClient = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, {
    auth: { autoRefreshToken: false, persistSession: false },
  });

  const tempPassword = genTempPassword();
  const { login_email, client, policy } = account;

  console.log('\n======================================================');
  console.log(`Enrolling  : ${client.insured_name}`);
  console.log(`Email      : ${login_email}`);
  console.log(`Mode       : ${dryRun ? 'DRY RUN (no writes)' : 'LIVE'}`);
  console.log('======================================================\n');

  // -------------------------------------------------------------------------
  // STEP 1 — Auth user
  // -------------------------------------------------------------------------

  console.log('--- Step 1: Auth user ---');

  // Check whether the user already exists (paginate in case of large user list)
  let existingUserId = null;
  let page = 1;
  const PER_PAGE = 1000;
  outer: while (true) {
    const { data: { users }, error } = await adminClient.auth.admin.listUsers({ page, perPage: PER_PAGE });
    if (error) bail(`listUsers failed: ${error.message}`);
    for (const u of users) {
      if (u.email?.toLowerCase() === login_email.toLowerCase()) {
        existingUserId = u.id;
        break outer;
      }
    }
    if (users.length < PER_PAGE) break;
    page++;
  }

  let userId;

  if (dryRun) {
    userId = existingUserId ?? '(new-uuid-will-be-generated)';
    if (existingUserId) {
      console.log(`[DRY RUN] User already exists: ${existingUserId}`);
      console.log(`[DRY RUN] Would reset password via updateUserById("${existingUserId}", { password: "***" })`);
    } else {
      console.log(`[DRY RUN] No existing user found for ${login_email}`);
      console.log(`[DRY RUN] Would call createUser({ email: "${login_email}", email_confirm: true, password: "***" })`);
    }
  } else {
    if (existingUserId) {
      console.log(`Auth user already exists (${existingUserId}) — resetting password.`);
      const { error } = await adminClient.auth.admin.updateUserById(existingUserId, { password: tempPassword });
      if (error) bail(`updateUserById failed: ${error.message}`);
      userId = existingUserId;
      console.log('Password reset OK.');
    } else {
      console.log(`Creating auth user for ${login_email} ...`);
      const { data, error } = await adminClient.auth.admin.createUser({
        email:         login_email,
        email_confirm: true,
        password:      tempPassword,
        user_metadata: { named_insured: client.insured_name },
      });
      if (error) bail(`createUser failed: ${error.message}`);
      userId = data.user.id;
      console.log(`Created. user.id = ${userId}`);
    }
  }

  // -------------------------------------------------------------------------
  // STEP 2 — clients row
  // -------------------------------------------------------------------------

  console.log('\n--- Step 2: clients row ---');

  const clientRow = {
    id:           userId,
    insured_name: client.insured_name,
    email:        client.email,
  };

  console.log('Row to upsert (onConflict: id):');
  console.log(JSON.stringify(clientRow, null, 2));

  if (!dryRun) {
    const { error } = await adminClient
      .from('clients')
      .upsert(clientRow, { onConflict: 'id' });
    if (error) bail(`clients upsert failed: ${error.message}`);
    console.log('clients upsert OK.');
  } else {
    console.log('[DRY RUN] No write.');
  }

  // -------------------------------------------------------------------------
  // STEP 3 — policies row
  // -------------------------------------------------------------------------

  console.log('\n--- Step 3: policies row ---');

  // Look up any existing policy row for this client (avoid duplicates on re-run)
  let existingPolicyId = null;
  if (!dryRun) {
    const { data: existing, error: lookupErr } = await adminClient
      .from('policies')
      .select('id')
      .eq('client_id', userId)
      .maybeSingle();
    if (lookupErr) bail(`policies lookup failed: ${lookupErr.message}`);
    existingPolicyId = existing?.id ?? null;
  }

  const policyRow = {
    id:         existingPolicyId ?? randomUUID(),
    client_id:  userId,
    updated_at: new Date().toISOString(),
    ...policy,
  };

  console.log('Row to upsert (onConflict: id):');
  console.log(JSON.stringify(policyRow, null, 2));

  if (!dryRun) {
    const { error } = await adminClient
      .from('policies')
      .upsert(policyRow, { onConflict: 'id' });
    if (error) bail(`policies upsert failed: ${error.message}`);
    if (existingPolicyId) {
      console.log(`policies upsert OK. (Updated existing row ${existingPolicyId})`);
    } else {
      console.log(`policies upsert OK. (Inserted new row ${policyRow.id})`);
    }
  } else {
    console.log('[DRY RUN] No write.');
  }

  // -------------------------------------------------------------------------
  // STEP 4 — Print credentials (always last — a failed run leaves nothing half-done)
  // -------------------------------------------------------------------------

  console.log('\n======================================================');
  if (dryRun) {
    console.log('DRY RUN COMPLETE — no writes made.');
    console.log('Review the rows above, then re-run without --dry-run to enroll for real.');
  } else {
    console.log('ENROLLMENT COMPLETE');
    console.log('------------------------------------------------------');
    console.log('  Deliver these to the customer directly:');
    console.log(`  Portal   : https://coi.pinnacleriskad.com`);
    console.log(`  Email    : ${login_email}`);
    console.log(`  Password : ${tempPassword}`);
    console.log('------------------------------------------------------');
    console.log('  Ask them to sign in and change their password.');
    console.log(`  Auth UUID: ${userId}`);
  }
  console.log('======================================================\n');
}

main().catch(err => { console.error('Unhandled error:', err); process.exit(1); });
