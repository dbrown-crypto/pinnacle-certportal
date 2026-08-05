/* ============================================================================
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
 *
 * Markup, class names and button labels reproduce the original exactly, so
 * existing CSS (.pill, .act, .b-ghost, .b-gold, .b-stop, .muted) still applies.
 * ==========================================================================*/

'use strict';

/* --- helpers -------------------------------------------------------------- */

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

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

/** span.pill with a state class, matching the original markup. */
function pill(text, stateClass) {
  return el('span', text ?? '', 'pill ' + stateClass);
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

function headerRow(labels) {
  const tr = document.createElement('tr');
  labels.forEach(l => tr.appendChild(el('th', l)));
  return tr;
}

function tbodyOf(table) {
  return table.tBodies[0] || table.appendChild(document.createElement('tbody'));
}

/* --- policies table ------------------------------------------------------- */
/* Original: Insured | DOT | Term | Status | Self-serve | As of | actions
 * Actions: Edit, Kill switch/Re-enable, Mark cancelled/Mark active
 *
 * renderPolicies(rows, { onEdit, onToggleServe, onSetStatus })
 *   onEdit(policy)              — receives the object, not a JSON string
 *   onToggleServe(id, nextBool)
 *   onSetStatus(id, status)
 */
function renderPolicies(rows, handlers) {
  const table = document.getElementById('polTable');
  if (!table) return;
  const body = tbodyOf(table);
  clear(body);

  body.appendChild(headerRow(
    ['Insured', 'DOT', 'Term', 'Status', 'Self-serve', 'As of', '']
  ));

  rows.forEach(p => {
    const tr = document.createElement('tr');

    const nameCell = document.createElement('td');
    nameCell.appendChild(el('strong', p.named_insured));
    tr.appendChild(nameCell);

    tr.appendChild(td(p.usdot));
    tr.appendChild(td(`${p.effective_date ?? ''} – ${p.expiration_date ?? ''}`));

    const statusCell = document.createElement('td');
    statusCell.appendChild(pill(p.status, p.status === 'active' ? 'active' : 'off'));
    tr.appendChild(statusCell);

    const serveCell = document.createElement('td');
    serveCell.appendChild(pill(
      p.self_serve_enabled ? 'On' : 'Off',
      p.self_serve_enabled ? 'active' : 'off'
    ));
    tr.appendChild(serveCell);

    tr.appendChild(td(p.data_current_as_of));

    const actions = document.createElement('td');
    actions.appendChild(button('Edit', 'act b-ghost', () => handlers.onEdit(p)));
    actions.appendChild(button(
      p.self_serve_enabled ? 'Kill switch' : 'Re-enable',
      'act ' + (p.self_serve_enabled ? 'b-stop' : 'b-gold'),
      () => handlers.onToggleServe(p.id, !p.self_serve_enabled)
    ));
    if (p.status === 'active') {
      actions.appendChild(button('Mark cancelled', 'act b-stop',
        () => handlers.onSetStatus(p.id, 'cancelled')));
    } else {
      actions.appendChild(button('Mark active', 'act b-gold',
        () => handlers.onSetStatus(p.id, 'active')));
    }
    tr.appendChild(actions);

    body.appendChild(tr);
  });
}

/* --- special-request queue ------------------------------------------------ */
/* Original: When | Holder | Wording | Status | actions
 *
 * This is the table that carried the stored-XSS path: requested_wording is
 * customer-submitted free text and was previously joined into innerHTML
 * WITHOUT escaping, while the holder fields around it were escaped. Here every
 * field goes through textContent, so the distinction no longer matters.
 *
 * renderQueue(rows, { onStatus })  — onStatus(id, 'sent' | 'in_progress')
 */
function renderQueue(rows, handlers) {
  const table = document.getElementById('queueTable');
  if (!table) return;
  const body = tbodyOf(table);
  clear(body);

  const openCount = rows.filter(r => r.status === 'open').length;
  const counter = document.getElementById('queueCount');
  if (counter) counter.textContent = openCount ? `(${openCount})` : '';

  body.appendChild(headerRow(['When', 'Holder', 'Wording', 'Status', '']));

  if (!rows.length) {
    const tr = document.createElement('tr');
    tr.appendChild(td('No special wording requests. Nothing waiting on you.', 'muted'));
    body.appendChild(tr);
    return;
  }

  rows.forEach(r => {
    const tr = document.createElement('tr');

    tr.appendChild(td(r.created_at ? new Date(r.created_at).toLocaleString() : ''));

    const holder = document.createElement('td');
    holder.appendChild(el('strong', r.holder_name));
    holder.appendChild(document.createElement('br'));
    holder.appendChild(el('span', r.holder_address, 'muted'));
    tr.appendChild(holder);

    const wording = Array.isArray(r.requested_wording)
      ? r.requested_wording.join(', ')
      : (r.requested_wording ?? '');
    tr.appendChild(td(wording));

    const statusCell = document.createElement('td');
    statusCell.appendChild(pill(r.status, r.status === 'open' ? 'open' : 'sent'));
    tr.appendChild(statusCell);

    const actions = document.createElement('td');
    if (r.status === 'open' && handlers && handlers.onStatus) {
      actions.appendChild(button('Mark sent', 'act b-gold',
        () => handlers.onStatus(r.id, 'sent')));
      actions.appendChild(button('In progress', 'act b-ghost',
        () => handlers.onStatus(r.id, 'in_progress')));
    }
    tr.appendChild(actions);

    body.appendChild(tr);
  });
}

/* --- exports -------------------------------------------------------------- */
window.PinnacleRender = {
  renderPolicies,
  renderQueue,
  el, td, button, pill, clear
};
