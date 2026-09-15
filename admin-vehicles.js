/* Per-VIN schedule editor. Values are assigned through DOM properties only. */
(function () {
  'use strict';
  const keys = ['vin', 'description', 'value', 'comprehensive_deductible', 'collision_deductible'];
  const deductibleFields = [
    ['comprehensive_deductible', 'comp', 'Comprehensive deductible $'],
    ['collision_deductible', 'collision', 'Collision deductible $']
  ];

  function field(row, prefix, name, label, value, numeric) {
    const wrap = document.createElement('label');
    wrap.style.cssText = 'font-size:11px;min-width:0;margin:0';
    wrap.append(document.createTextNode(label));
    const input = document.createElement('input');
    input.className = prefix + '-' + name;
    input.value = value == null ? '' : String(value);
    input.style.minWidth = '0';
    if (numeric) {
      input.type = 'number'; input.min = '0'; input.step = '0.01'; input.max = '999999999.99';
      input.placeholder = 'Not entered';
    }
    wrap.append(input); row.append(wrap);
    return input;
  }

  function build(data, prefix) {
    data = data || {};
    const row = document.createElement('div');
    row.className = prefix + '-row';
    row.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px;padding:10px;border:1px solid var(--line);border-radius:8px;margin-bottom:10px;align-items:end';
    row.dataset.extra = JSON.stringify(Object.fromEntries(Object.entries(data).filter(([k]) => !keys.includes(k))));
    field(row, prefix, 'desc', 'Year / Make / Description', data.description);
    field(row, prefix, 'vin', 'VIN', data.vin);
    field(row, prefix, 'val', 'Stated value $', data.value, true);
    deductibleFields.forEach(([key, cls, label]) => field(row, prefix, cls, label, data[key], true));
    const actions = document.createElement('div');
    if (prefix === 'pu') {
      const decode = document.createElement('button');
      decode.type = 'button'; decode.className = 'act b-ghost pu-decode'; decode.textContent = 'Decode VIN';
      actions.append(decode);
    }
    const remove = document.createElement('button');
    remove.type = 'button'; remove.className = 'act b-stop'; remove.textContent = 'Remove';
    remove.addEventListener('click', () => row.remove());
    actions.append(remove); row.append(actions);
    return row;
  }

  function amount(input, label) {
    const raw = input.value.trim();
    if (input.validity && input.validity.badInput) throw new Error(label + ' must be a dollar amount.');
    if (raw === '') return null;
    const value = Number(raw);
    if (!Number.isFinite(value) || value < 0 || value > 999999999.99 ||
        Math.abs(value * 100 - Math.round(value * 100)) > 0.0001) {
      throw new Error(label + ' must be a nonnegative dollar amount with at most two decimal places.');
    }
    return value;
  }

  function assemble(container, prefix) {
    return Array.from(document.querySelectorAll(container + ' .' + prefix + '-row')).flatMap(row => {
      const get = name => row.querySelector('.' + prefix + '-' + name);
      const description = get('desc').value.trim(), vin = get('vin').value.trim();
      const value = amount(get('val'), 'Stated value');
      const comp = amount(get('comp'), 'Comprehensive deductible');
      const collision = amount(get('collision'), 'Collision deductible');
      if (!description && !vin && value === null && comp === null && collision === null) return [];
      if ((comp !== null || collision !== null) && !vin) throw new Error('Enter a VIN for each vehicle with deductibles.');
      const unit = {...JSON.parse(row.dataset.extra || '{}'), description, vin, value: value === null ? 0 : value};
      if (comp !== null) unit.comprehensive_deductible = comp;
      if (collision !== null) unit.collision_deductible = collision;
      return [unit];
    });
  }

  function applyAll(compInput, collisionInput) {
    // Validate both before changing any row. Blank bulk inputs leave that field alone.
    const comp = amount(compInput, 'Comprehensive deductible');
    const collision = amount(collisionInput, 'Collision deductible');
    if (comp === null && collision === null) throw new Error('Enter at least one deductible to apply.');
    const rows = document.querySelectorAll('#puLines .pu-row, #vehLines .veh-row');
    if (!rows.length) throw new Error('Add a power unit or trailer first.');
    rows.forEach(row => {
      if (comp !== null) row.querySelector('.pu-comp, .veh-comp').value = comp;
      if (collision !== null) row.querySelector('.pu-collision, .veh-collision').value = collision;
    });
    return rows.length;
  }
  window.PinnacleVehicles = {build, assemble, applyAll};
})();
