const probe = 1;
  // indent test
const two = 2;const probe = 1;
  // indent test
const two = 2;/* ============================================================================
 * admin-render.js — safe DOM builders for the Pinnacle Certificate Admin
 *
 * Replaces the innerHTML-based row builders in admin.html.
 *
 * Design rule: database values NEVER become HTML. They are assigned through
 * textContent, which cannot create elements or execute script. There is no
 * escaping step to forget, because there is no HTML parsing step at all.
 *
 * Handlers are attached with addEventListener and close over the record
 * object directly, so no customer data is ever serialized into an attribute.
 * That is what removes the apostrophe/quote class of bug permanently —
 * O'Neal Trucking, "Smith & Sons", and <script> are all just text.
 * ==========================================================================*/

'use strict';

/* --- small helpers -------------------------------------------------------- */

/** Create an element with text content set safely. */
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

/** Table cell with safe text. */
function td(text, className) {
  return el('td', text ?? '', className);
}

/** Button wired with addEventListener — never an inline onclick. */
function button(label, className, onClick) {
  const b = el('button', label, className);
  b.type = 'button';
  b.addEventListener('click', onClick);
  return b;
}

/** ISO date -> MM/DD/YY for display. Returns '' for empty/invalid input. */
function fmtDate(iso) {
  if (!iso) return '';
  const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? `${m[2]}/${m[3]}/${m[1].slice(2)}` : '';
}

/** Replace all children of a node without touching innerHTML. */
function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/** Build a header row from an array of labels. */
function headerRow(labels) {
  const tr = document.createElement('tr');
  labels.forEach(l => tr.appendChild(el('th', l)));
  return tr;
}

/* --- policies table ------------------------------------------------------- */
/* Columns: Insured | DOT | Term | Status | Self-serve | As of | (actions)
 * Actions: Edit, Kill switch, Mark cancelled
 *
 * Wire-up: renderPolicies(policies, { onEdit, onToggleServe, onSetStatus })
 * The three callbacks receive the policy OBJECT, not an id string, so the
 * existing editPolicy(p) / toggleServe(id, val) / setStatus(id, val) bodies
 * can be reused with minimal change.
 */
function renderPolicies(policies, handlers) {
  const table = document.getElementById('polTable');
  if (!table) return;
  const body = table.tBodies[0] || table.appendChild(document.createElement('tbody'));
  clear(body);

  body.appendChild(headerRow(
    ['Insured', 'DOT', 'Term', 'Status', 'Self-serve', 'As of', '']
  ));

  policies.forEach(p => {
    const tr = document.createElement('tr');

    tr.appendChild(td(p.named_insured));
    tr.appendChild(td(p.usdot));
    tr.appendChild(td(`${fmtDate(p.effective_date)} – ${fmtDate(p.expiration_date)}`));
    tr.appendChild(td(p.status));
    tr.appendChild(td(p.self_serve_enabled ? 'On' : 'Off'));
    tr.appendChild(td(fmtDate(p.data_current_as_of)));

    const actions = document.createElement('td');
    actions.appendChild(button('Edit', 'btn-sm', () => handlers.onEdit(p)));
    actions.appendChild(button(
      'Kill switch', 'btn-sm',
      () => handlers.onToggleServe(p.id, !p.self_serve_enabled)
    ));
    actions.appendChild(button(
      'Mark cancelled', 'btn-sm',
      () => handlers.onSetStatus(p.id, 'cancelled')
    ));
    tr.appendChild(actions);

    body.appendChild(tr);
  });
}

/* --- special-request queue ------------------------------------------------ */
/* Columns: When | Holder | Wording | Status | (actions)
 *
 * This is the highest-risk table: `requested_wording`, `holder_name` and
 * `notes` are all customer-submitted free text. Everything here goes through
 * textContent.
 */
function renderQueue(requests, handlers) {
  const table = document.getElementById('queueTable');
  if (!table) return;
  const body = table.tBodies[0] || table.appendChild(document.createElement('tbody'));
  clear(body);

  body.appendChild(headerRow(['When', 'Holder', 'Wording', 'Status', '']));

  requests.forEach(r => {
    const tr = document.createElement('tr');

    tr.appendChild(td(fmtDate(r.created_at)));
    tr.appendChild(td(r.holder_name));

    // requested_wording may be a JSON array or a free-text string.
    const wording = Array.isArray(r.requested_wording)
      ? r.requested_wording.join(', ')
      : (r.requested_wording ?? '');
    tr.appendChild(td(wording));

    tr.appendChild(td(r.status));

    const actions = document.createElement('td');
    if (handlers && handlers.onSetStatus) {
      actions.appendChild(button(
        'Mark handled', 'btn-sm',
        () => handlers.onSetStatus(r.id, 'handled')
      ));
    }
    tr.appendChild(actions);

    body.appendChild(tr);
  });
}

/* --- issued certificates -------------------------------------------------- */
function renderCerts(certs) {
  const table = document.getElementById('certTable');
  if (!table) return;
  const body = table.tBodies[0] || table.appendChild(document.createElement('tbody'));
  clear(body);

  body.appendChild(headerRow(['Issued', 'Cert #', 'Holder', 'Description', 'By']));

  certs.forEach(c => {
    const tr = document.createElement('tr');
    tr.appendChild(td(fmtDate(c.issued_at)));
    tr.appendChild(td(c.cert_number));
    tr.appendChild(td(c.holder_name));
    tr.appendChild(td(c.description_of_ops));
    tr.appendChild(td(c.issued_by));
    body.appendChild(tr);
  });
}

/* --- audit log ------------------------------------------------------------ */
function renderAudit(entries) {
  const table = document.getElementById('auditTable');
  if (!table) return;
  const body = table.tBodies[0] || table.appendChild(document.createElement('tbody'));
  clear(body);

  body.appendChild(headerRow(['When', 'Action', 'Holder', 'Codes', 'Reasons']));

  entries.forEach(a => {
    const tr = document.createElement('tr');
    tr.appendChild(td(fmtDate(a.occurred_at)));
    tr.appendChild(td(a.action));
    tr.appendChild(td(a.holder_name));
    // audit_codes / reasons are jsonb — stringify, then insert as TEXT.
    tr.appendChild(td(Array.isArray(a.audit_codes) ? a.audit_codes.join(', ') : JSON.stringify(a.audit_codes ?? '')));
    tr.appendChild(td(Array.isArray(a.reasons) ? a.reasons.join('; ') : JSON.stringify(a.reasons ?? '')));
    body.appendChild(tr);
  });
}

/* --- exports -------------------------------------------------------------- */
window.PinnacleRender = {
  renderPolicies,
  renderQueue,
  renderCerts,
  renderAudit,
  el, td, button, fmtDate, clear
};
