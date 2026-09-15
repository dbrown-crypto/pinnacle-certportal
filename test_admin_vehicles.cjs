// Run: npm install --no-save --no-package-lock jsdom && node --test test_admin_vehicles.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');

function editor() {
  const dom = new JSDOM(fs.readFileSync('admin.html', 'utf8'), {runScripts:'outside-only', url:'https://example.test/admin.html'});
  const w = dom.window;
  w.scrollTo = () => {};
  w.supabase = {createClient:() => ({auth:{getSession:async() => ({data:{session:null}})}})};
  w.eval(fs.readFileSync('admin-render.js', 'utf8'));
  w.eval(fs.readFileSync('admin-vehicles.js', 'utf8'));
  for (const script of w.document.querySelectorAll('script:not([src])')) w.eval(script.textContent);
  w.document.dispatchEvent(new w.Event('DOMContentLoaded'));
  return w;
}
const plain = value => JSON.parse(JSON.stringify(value));
const policy = () => ({id:'policy-test', client_id:'client-test', named_insured:'Example Trucking',
  status:'active', effective_date:'2026-01-01', expiration_date:'2027-01-01',
  insured_address:'1 Test Road', data_current_as_of:'2026-09-15', producer_block:'Pinnacle Risk Advisors LLC',
  vehicles:[{vin:'VIN-A',description:'Truck A',value:30000,comprehensive_deductible:1000,collision_deductible:2500,custom:'keep'}],
  trailers:[{vin:'VIN-T',description:'Trailer',value:0,collision_deductible:500}]});

test('edit and save retains both schedules, zero and metadata', async () => {
  const w = editor();
  try {
    const p = policy(); p.vehicles[0].comprehensive_deductible=0;
    w.editPolicy(p);
    let saved;
    w.api=async (url, options) => {saved=JSON.parse(options.body);return {};};
    w.loadPolicies=async () => {};
    w.document.getElementById('savePolicy').click();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(saved.vehicles, p.vehicles);
    assert.deepEqual(saved.trailers, p.trailers);
    w.editPolicy(saved);
    assert.deepEqual(plain(w.assemblePowerUnits()), saved.vehicles);
  } finally {w.close();}
});

test('apply common amounts then retain an individual override', () => {
  const w=editor();
  try {
    w.editPolicy(policy());
    w.document.getElementById('bulk_comp').value='1000';
    w.document.getElementById('bulk_collision').value='2500';
    w.document.getElementById('applyDeductibles').click();
    w.document.querySelector('.veh-collision').value='5000';
    assert.equal(w.assemblePowerUnits()[0].collision_deductible,2500);
    assert.equal(w.assembleVehicles()[0].collision_deductible,5000);
    assert.equal(w.assembleVehicles()[0].comprehensive_deductible,1000);
    w.document.getElementById('bulk_comp').value='';
    w.document.getElementById('bulk_collision').value='0';
    w.document.getElementById('applyDeductibles').click();
    assert.equal(w.assemblePowerUnits()[0].comprehensive_deductible,1000);
    assert.equal(w.assemblePowerUnits()[0].collision_deductible,0);
  } finally {w.close();}
});

test('blank values remain absent and cleared values are removed', () => {
  const w=editor();
  try {
    w.puAddRow({vin:'VIN-A',description:'Truck'});
    assert.equal('collision_deductible' in w.assemblePowerUnits()[0],false);
    w.editPolicy(policy());
    w.document.querySelector('.pu-comp').value='';
    w.document.querySelector('.pu-collision').value='';
    assert.equal('comprehensive_deductible' in w.assemblePowerUnits()[0],false);
    assert.equal('collision_deductible' in w.assemblePowerUnits()[0],false);
    w.document.getElementById('bulk_comp').value='200';
    w.document.getElementById('clearForm').click();
    assert.equal(w.document.getElementById('bulk_comp').value,'');
    assert.equal(w.assemblePowerUnits().length,0);
  } finally {w.close();}
});

test('invalid input blocks saving and bulk application changes no rows', async () => {
  const w=editor();
  try {
    w.editPolicy(policy());
    w.document.getElementById('bulk_comp').value='500';
    w.document.getElementById('bulk_collision').value='-1';
    w.document.getElementById('applyDeductibles').click();
    assert.equal(w.assemblePowerUnits()[0].comprehensive_deductible,1000);
    let calls=0; w.api=async()=>{calls++;};
    w.document.querySelector('.pu-comp').value='-1';
    w.document.getElementById('savePolicy').click();
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(calls,0);
    assert.match(w.document.getElementById('polErr').textContent,/nonnegative/);
    w.document.querySelector('.pu-comp').value='1000';
    w.document.querySelector('.pu-vin').value='';
    assert.throws(()=>w.assemblePowerUnits(),/VIN/);
  } finally {w.close();}
});

test('untrusted description stays text and row removal still works', () => {
  const w=editor();
  try {
    w.puAddRow({vin:'VIN-A',description:'<img src=x onerror=alert(1)>'});
    assert.equal(w.document.querySelectorAll('#puLines img').length,0);
    assert.equal(w.assemblePowerUnits()[0].description,'<img src=x onerror=alert(1)>');
    w.document.querySelector('#puLines .b-stop').click();
    assert.equal(w.assemblePowerUnits().length,0);
  } finally {w.close();}
});
