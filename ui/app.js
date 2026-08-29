/* Planner UI. No libraries: the two charts a control office actually reads are
   a time-distance diagram and a machine Gantt, and both are just scaled lines
   and rectangles. Keeping them hand-drawn means no CDN, no build step, and
   nothing to fail on demo day. */

const S = {
  // 'ALL' by default: the corridor alternates between the UP and DN lines day
  // to day, so opening on a single line lands on an empty chart about half the
  // time. Never open a demo on an empty chart.
  data: null, plan: 0, day: 0, line: 'ALL',
  show: { trains: true, blocks: true, ghosts: true },
  disabled: new Set(),
};
const NS = 'http://www.w3.org/2000/svg';
const TYPE_COLOR = { SUPERFAST: 'var(--sf)', EXPRESS: 'var(--exp)',
                     PASSENGER: 'var(--pas)', FREIGHT: 'var(--frt)' };
const ACT_COLOR = { tamping: '#ef5350', deep_screening: '#ab47bc',
                    usfd: '#ffca28', destressing: '#26a69a',
                    ohe_maintenance: '#5c6bc0', rail_grinding: '#8d6e63',
                    points_crossings: '#ec407a' };

const el = (t, a = {}, p) => { const n = document.createElementNS(NS, t);
  for (const k in a) n.setAttribute(k, a[k]); if (p) p.appendChild(n); return n; };
const hhmm = m => { m = ((m % 1440) + 1440) % 1440;
  return String(m / 60 | 0).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0'); };
const fmtDay = m => 'D' + ((m / 1440 | 0) + 1) + ' ' + hhmm(m);

// ---------------------------------------------------------------- bootstrap
init();
async function init() {
  setStatus('building the world and solving — first load takes ~40 s…');
  try {
    const r = await fetch('/api/data');
    S.data = await r.json();
  } catch (e) {
    setStatus('could not reach the API. Start it with:  python -m api.main');
    return;
  }
  setStatus('');
  S.plan = S.data.plans.length - 1;
  S.day = firstDayWithWork();
  buildControls();
  render();
}

function setStatus(t) { const s = document.getElementById('status');
  if (s) s.textContent = t; }

function firstDayWithWork() {
  const plan = S.data.plans[S.plan];
  const days = plan.blocks.map(b => Math.floor(b.start / 1440));
  return days.length ? Math.min(...days) : 0;
}

// ---------------------------------------------------------------- controls
function buildControls() {
  const d = S.data;
  document.getElementById('sectionline').textContent =
    `${d.section.name} · ${d.section.zone} · ${d.stations.length} stations · ` +
    `${d.stations[d.stations.length - 1].km} km double line · ` +
    `${d.reports.traffic.trains_per_day} trains/day ` +
    `(${d.reports.traffic.freight_share_pct}% simulated freight)`;

  seg('planSel', d.plans.map((p, i) => [shortName(p.name), i]),
      () => S.plan, v => { S.plan = v; render(); });
  seg('daySel', Array.from({ length: d.planning.horizon_days },
      (_, i) => ['D' + (i + 1), i]), () => S.day, v => { S.day = v; render(); });
  seg('lineSel', [['DN →Surat', 'DN'], ['UP →Vadodara', 'UP'], ['both', 'ALL']],
      () => S.line, v => { S.line = v; render(); });
  seg('showSel', [['trains', 'trains'], ['blocks', 'blocks'],
      ['pre-shift ghosts', 'ghosts']], null,
      v => { S.show[v] = !S.show[v]; render(); }, v => S.show[v]);

  const mt = document.getElementById('machineToggles');
  mt.innerHTML = '';
  d.machines.forEach(m => {
    const b = document.createElement('button');
    b.textContent = m.id; b.title = m.label;
    b.onclick = () => { S.disabled.has(m.id) ? S.disabled.delete(m.id)
      : S.disabled.add(m.id); b.classList.toggle('off'); };
    mt.appendChild(b);
  });

  document.getElementById('scenarioBtn').onclick = () => {
    const s = document.getElementById('scenario'); s.hidden = !s.hidden; };
  bindRange('traffic', v => (+v).toFixed(2) + '×', 'trafficVal');
  bindRange('hours', v => (+v).toFixed(1) + ' h/day', 'hoursVal');
  bindRange('tl', v => v + ' s', 'tlVal');
  document.getElementById('replanBtn').onclick = replan;
}

function shortName(n) { return n; }

function seg(id, items, get, set, isOn) {
  const host = document.getElementById(id); host.innerHTML = '';
  items.forEach(([label, val]) => {
    const b = document.createElement('button');
    b.textContent = label;
    const on = isOn ? isOn(val) : get() === val;
    if (on) b.classList.add('on');
    b.onclick = () => { set(val);
      [...host.children].forEach(c => c.classList.remove('on'));
      if (isOn) { [...host.children].forEach((c, i) =>
        c.classList.toggle('on', isOn(items[i][1]))); }
      else b.classList.add('on'); };
    host.appendChild(b);
  });
}

function bindRange(id, fmt, out) {
  const i = document.getElementById(id), o = document.getElementById(out);
  const upd = () => o.textContent = fmt(i.value);
  i.oninput = upd; upd();
}

async function replan() {
  const btn = document.getElementById('replanBtn');
  btn.disabled = true; setStatus('re-optimising…');
  const t0 = performance.now();
  const body = {
    traffic: +document.getElementById('traffic').value,
    block_hours_per_day: +document.getElementById('hours').value,
    time_limit: +document.getElementById('tl').value,
    allow_retiming: document.getElementById('retiming').checked,
    corridor_blocks: document.getElementById('corridor').checked,
    disabled_machines: [...S.disabled],
  };
  try {
    const r = await fetch('/api/replan', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    S.data = await r.json();
    S.plan = Math.min(S.plan, S.data.plans.length - 1);
    setStatus(`re-optimised in ${((performance.now() - t0) / 1000).toFixed(1)} s`);
    render();
  } catch (e) { setStatus('re-optimise failed: ' + e); }
  btn.disabled = false;
}

// ---------------------------------------------------------------- render
function render() { drawKPIs(); drawChart(); drawGantt(); drawTable();
  drawRetiming(); drawProvenance(); }

function curPlan() { return S.data.plans[S.plan]; }
function curKPI() { return S.data.kpis[S.plan]; }

function drawKPIs() {
  const k = curKPI(), base = S.data.kpis[0];
  const cards = [
    ['track maintained', k.asset_km_maintained + ' km',
     delta(k.asset_km_maintained, base.asset_km_maintained, 'km')],
    ['blocks completed', k.jobs_completed,
     delta(k.jobs_completed, base.jobs_completed, '')],
    ['asset availability', k.aai_with_plan.toFixed(4),
     'do nothing ' + k.aai_do_nothing.toFixed(4)],
    ['block hours used', k.block_hours_granted + ' h',
     'of ' + (S.data.planning.max_block_hours_per_day *
              S.data.planning.horizon_days).toFixed(0) + ' h tolerance'],
    ['passenger punctuality', k.punctuality_passenger_pct + '%',
     '+' + k.mean_added_delay_passenger_min + ' min mean'],
    ['solve time', k.solve_seconds + ' s',
     k.solver_status + (k.mip_gap_pct ? ` · gap ${k.mip_gap_pct}%` : ' · proven')],
  ];
  const v = (S.data.validation || [])[S.plan];
  if (v) cards.splice(2, 0, ['plan check', v.feasible ? 'feasible' : 'FAILED',
    v.feasible ? 'all constraints verified' : v.n_violations + ' violations']);
  document.getElementById('kpis').innerHTML = cards.map(([l, v, d]) =>
    `<div class="kpi"><div class="v">${v}</div><div class="l">${l}</div>
     <div class="d${String(d).startsWith('−') ? ' neg' : ''}">${d}</div></div>`).join('');
}

function delta(a, b, unit) {
  const d = +(a - b).toFixed(1);
  if (!d) return 'same as greedy';
  return (d > 0 ? '+' : '−') + Math.abs(d) + (unit ? ' ' + unit : '') + ' vs greedy';
}

// ------------------------------------------------- time-distance chart
function drawChart() {
  const d = S.data, sv = document.getElementById('tdchart');
  sv.innerHTML = '';
  const W = 1460, H = 560, L = 74, R = 24, T = 26, B = 34;
  sv.setAttribute('width', W); sv.setAttribute('height', H);
  sv.setAttribute('viewBox', `0 0 ${W} ${H}`);

  const kmMax = d.stations[d.stations.length - 1].km;
  const t0 = S.day * 1440, t1 = t0 + 1440;
  const X = t => L + (t - t0) / 1440 * (W - L - R);
  const Y = km => T + km / kmMax * (H - T - B);

  // hour grid
  for (let h = 0; h <= 24; h++) {
    const x = X(t0 + h * 60);
    el('line', { x1: x, y1: T, x2: x, y2: H - B,
      stroke: h % 6 === 0 ? '#38465a' : '#202a37',
      'stroke-width': h % 6 === 0 ? 1.2 : 1 }, sv);
    if (h % 2 === 0) {
      const tx = el('text', { x, y: H - B + 16, fill: '#8b98a8',
        'font-size': 10.5, 'text-anchor': 'middle' }, sv);
      tx.textContent = String(h).padStart(2, '0') + ':00';
    }
  }
  // station lines
  d.stations.forEach(s => {
    const y = Y(s.km);
    el('line', { x1: L, y1: y, x2: W - R, y2: y,
      stroke: s.loop ? '#3a4a5f' : '#28323f',
      'stroke-dasharray': s.loop ? '' : '3 4' }, sv);
    const tx = el('text', { x: L - 8, y: y + 3.5, fill: '#c3cdd9',
      'font-size': 10.5, 'text-anchor': 'end' }, sv);
    tx.textContent = s.code;
    const t2 = el('text', { x: L - 8, y: y + 14, fill: '#5d6b7d',
      'font-size': 8.5, 'text-anchor': 'end' }, sv);
    t2.textContent = s.km + ' km';
  });

  const plan = curPlan();
  const shifts = plan.retimings || {};

  // reserved corridor windows: the hole the policy made in the timetable
  (d.corridors || []).filter(c => c.day === S.day &&
      (S.line === 'ALL' || c.line === S.line)).forEach(c => {
    const x = X(Math.max(c.start, t0)), x2 = X(Math.min(c.end, t1));
    const r = el('rect', { x, y: Y(c.km_start), width: Math.max(2, x2 - x),
      height: Y(c.km_end) - Y(c.km_start), fill: '#4fc3f7', 'fill-opacity': .13,
      stroke: '#4fc3f7', 'stroke-opacity': .75, 'stroke-width': 1.5,
      'stroke-dasharray': '6 4' }, sv);
    const lab = el('text', { x: x + 6, y: Y(c.km_start) + 13, fill: '#4fc3f7',
      'font-size': 10, 'font-weight': 600 }, sv);
    lab.textContent = `corridor ${c.span} · ${c.line}`;
    tip(r, `<b>Reserved maintenance corridor</b><br>${c.span} · ${c.line} line<br>` +
      `km ${c.km_start}–${c.km_end}<br>${fmtDay(c.start)} → ${fmtDay(c.end)}<br>` +
      `${c.displaced} trains re-timetabled around it`);
  });

  // maintenance blocks next, so trains draw on top
  if (S.show.blocks) {
    plan.blocks.forEach(b => {
      if (S.line !== 'ALL' && b.line !== S.line) return;
      if (b.end <= t0 || b.start >= t1) return;
      const x = X(Math.max(b.start, t0)), x2 = X(Math.min(b.end, t1));
      const y = Y(Math.min(b.km_start, b.km_end));
      const y2 = Y(Math.max(b.km_start, b.km_end));
      const c = ACT_COLOR[b.activity] || '#ef5350';
      const r = el('rect', { x, y: y - 3, width: Math.max(2.5, x2 - x),
        height: Math.max(7, y2 - y + 6), fill: c, 'fill-opacity': .3,
        stroke: c, 'stroke-width': 1.2, rx: 2 }, sv);
      tip(r, `<b>${b.label}</b><br>${b.job_id} · ${b.stretch_id}<br>` +
        `km ${b.km_start}–${b.km_end} · ${b.machine_unit}<br>` +
        `${fmtDay(b.start)} → ${fmtDay(b.end)} (${b.end - b.start} min)<br>` +
        `priority ${b.priority}`);
    });
  }

  // trains
  if (S.show.trains) {
    d.trains.forEach(t => {
      if (S.line !== 'ALL' && t.line !== S.line) return;
      const sh = shifts[t.uid] || 0;
      if (S.show.ghosts && sh) drawPath(sv, t, 0, X, Y, t0, t1, true);
      drawPath(sv, t, sh, X, Y, t0, t1, false);
    });
  }

  const ttl = el('text', { x: L, y: 16, fill: '#8b98a8', 'font-size': 11 }, sv);
  ttl.textContent = `Day ${S.day + 1} · ${S.line === 'ALL' ? 'both lines'
    : S.line + ' line'} · ${plan.blocks.filter(b =>
      (S.line === 'ALL' || b.line === S.line) &&
      b.start < t1 && b.end > t0).length} blocks shown`;

  document.getElementById('legend').innerHTML =
    '<span><i style="background:#4fc3f7;opacity:.5;height:9px;border-radius:2px"></i>' +
    'reserved corridor</span>' +
    Object.entries(TYPE_COLOR).map(([k, v]) =>
      `<span><i style="background:${v}"></i>${k.toLowerCase()}</span>`).join('') +
    Object.entries(ACT_COLOR).map(([k, v]) =>
      `<span><i style="background:${v};height:9px;border-radius:2px;opacity:.55"></i>${
        k.replace(/_/g, ' ')}</span>`).join('') +
    `<span><i style="background:#5d6b7d;border-top:1px dashed"></i>pre-shift path</span>`;
}

function drawPath(sv, t, shift, X, Y, t0, t1, ghost) {
  const pts = [];
  t.pts.forEach(([km, arr, dep]) => {
    pts.push([X(arr + shift), Y(km)]);
    if (dep !== arr) pts.push([X(dep + shift), Y(km)]);
  });
  const lo = t.pts[0][1] + shift, hi = t.pts[t.pts.length - 1][1] + shift;
  if (hi < t0 || lo > t1) return;
  const p = el('polyline', {
    points: pts.map(p => p.join(',')).join(' '),
    fill: 'none',
    stroke: ghost ? '#5d6b7d' : (TYPE_COLOR[t.type] || '#888'),
    'stroke-width': ghost ? 1 : (t.type === 'FREIGHT' ? 1.1 : 1.5),
    'stroke-opacity': ghost ? .5 : (t.type === 'FREIGHT' ? .65 : .9),
    'stroke-dasharray': ghost ? '3 3' : '',
  }, sv);
  if (!ghost) tip(p, `<b>${t.no}</b> ${t.name}<br>${t.type} · ${t.line} line<br>` +
    `enters ${fmtDay(t.pts[0][2] + shift)} · exits ${fmtDay(hi)}` +
    (shift ? `<br><b>regulated ${shift > 0 ? '+' : ''}${shift} min</b>` : ''));
}

// ------------------------------------------------------------- machine gantt
function drawGantt() {
  const d = S.data, sv = document.getElementById('gantt');
  sv.innerHTML = '';
  const units = d.machines;
  const W = 840, rowH = 21, T = 24, L = 96, R = 14, B = 26;
  const H = T + units.length * rowH + B;
  sv.setAttribute('width', W); sv.setAttribute('height', H);
  sv.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const total = d.planning.horizon_days * 1440;
  const X = t => L + t / total * (W - L - R);

  for (let day = 0; day <= d.planning.horizon_days; day++) {
    const x = X(day * 1440);
    el('line', { x1: x, y1: T - 6, x2: x, y2: H - B, stroke: '#2b3644' }, sv);
    if (day < d.planning.horizon_days) {
      const tx = el('text', { x: x + 4, y: T - 10, fill: '#5d6b7d',
        'font-size': 10 }, sv);
      tx.textContent = 'D' + (day + 1);
    }
  }
  const blocks = curPlan().blocks;
  units.forEach((u, i) => {
    const y = T + i * rowH;
    el('rect', { x: L, y, width: W - L - R, height: rowH - 4,
      fill: i % 2 ? '#151b23' : '#19212b' }, sv);
    const lab = el('text', { x: L - 8, y: y + 12, fill: '#c3cdd9',
      'font-size': 10.5, 'text-anchor': 'end' }, sv);
    lab.textContent = u.id;
    blocks.filter(b => b.machine_unit === u.id).forEach(b => {
      const c = ACT_COLOR[b.activity] || '#ef5350';
      const r = el('rect', { x: X(b.start), y: y + 1,
        width: Math.max(2, X(b.end) - X(b.start)), height: rowH - 6,
        fill: c, 'fill-opacity': .78, rx: 2 }, sv);
      tip(r, `<b>${b.label}</b><br>${b.job_id} · ${b.stretch_id}<br>` +
        `${fmtDay(b.start)} → ${fmtDay(b.end)}`);
    });
    const used = blocks.filter(b => b.machine_unit === u.id)
      .reduce((a, b) => a + (b.end - b.start), 0);
    const pc = el('text', { x: W - R - 2, y: y + 12, fill: '#5d6b7d',
      'font-size': 9.5, 'text-anchor': 'end' }, sv);
    pc.textContent = used ? (used / 60).toFixed(1) + ' h' : 'idle';
  });
}

// ------------------------------------------------------------- tables
const CMP_ROWS = [
  ['jobs_completed', 'blocks completed', 1],
  ['asset_km_maintained', 'track maintained (km)', 1],
  ['pct_priority_completed', 'priority-weighted done (%)', 1],
  ['overdue_km_days_cleared', 'overdue km-days cleared', 1],
  ['aai_with_plan', 'asset availability index', 1],
  ['block_hours_granted', 'block hours used', 1],
  ['asset_km_per_block_hour', 'km per block hour', 1],
  ['mean_added_delay_passenger_min', 'passenger delay, mean (min)', -1],
  ['punctuality_passenger_pct', 'passenger punctuality (%)', 1],
  ['mean_added_delay_freight_min', 'goods delay, mean (min)', -1],
  ['block_bursts', 'block bursts in simulation', -1],
  ['trains_retimed', 'goods paths regulated', 0],
  ['solve_seconds', 'solve time (s)', 0],
];

function drawTable() {
  const k = S.data.kpis;
  let h = '<thead><tr><th>metric</th>' +
    k.map(r => `<th>${shortName(r.plan)}</th>`).join('') + '</tr></thead><tbody>';
  CMP_ROWS.forEach(([key, label, dir]) => {
    const vals = k.map(r => r[key]);
    const best = dir === 0 ? null
      : (dir > 0 ? Math.max(...vals) : Math.min(...vals));
    h += `<tr><td>${label}</td>` + vals.map(v =>
      `<td class="${dir && v === best && new Set(vals).size > 1 ? 'best' : ''}">${v}</td>`
    ).join('') + '</tr>';
  });
  document.getElementById('cmp').innerHTML = h + '</tbody>';
}

function drawRetiming() {
  const plan = curPlan(), p = document.getElementById('retimePanel');
  const rt = Object.entries(plan.retimings || {});
  if (!rt.length) { p.hidden = true; return; }
  p.hidden = false;
  const byUid = Object.fromEntries(S.data.trains.map(t => [t.uid, t]));
  let h = '<thead><tr><th>goods path</th><th>day</th><th>shift</th>' +
    '<th>booked entry</th><th>regulated entry</th></tr></thead><tbody>';
  rt.sort((a, b) => Math.abs(b[1]) - Math.abs(a[1])).forEach(([uid, sh]) => {
    const t = byUid[uid]; if (!t) return;
    h += `<tr><td class="tag">${t.no} — ${t.name}</td><td>D${t.day + 1}</td>` +
      `<td>${sh > 0 ? '+' : ''}${sh} min</td><td>${hhmm(t.pts[0][2])}</td>` +
      `<td>${hhmm(t.pts[0][2] + sh)}</td></tr>`;
  });
  const dg = S.data.retiming_diagnostics || {};
  h += `</tbody><tfoot><tr><td class="tag" colspan="5">` +
    `${rt.length} goods paths regulated out of ${dg.candidates_offered || '—'} ` +
    `offered to the solver. ${dg.jobs_unlockable_in_principle || 0} pending jobs ` +
    `sit in windows that are long enough overall but chopped up by goods traffic.` +
    `</td></tr></tfoot>`;
  document.getElementById('retimeTbl').innerHTML = h;
}

function drawProvenance() {
  const rows = [
    ['Passenger timetable', 'REAL schema', 'data.gov.in / Kaggle IR time-table CSV; the synthetic generator writes the same columns, so swapping in the real file changes nothing else'],
    ['Network, stations, chainage', 'REAL', 'public IR time-tables + OpenStreetMap (Overpass query in ingest/network.py)'],
    ['Maintenance periodicities', 'REAL (published)', 'IRPWM, encoded in data/norms.yaml with every assumption named'],
    ['Freight paths', 'SIMULATED', 'threaded into genuine timetable gaps at freight speed, held in loops for overtakes'],
    ['Maintenance demand', 'SIMULATED', 'generated from the published periodicities + last-done dates'],
    ['Asset condition (TGI)', 'SIMULATED', 'degradation model driven by tonnage, age, curvature, monsoon shocks'],
    ['Block plan and KPIs', 'COMPUTED', 'CP-SAT + discrete-event simulation, live, on the data above'],
  ];
  document.getElementById('provenance').innerHTML =
    '<thead><tr><th>layer</th><th>status</th><th>source</th></tr></thead><tbody>' +
    rows.map(([a, b, c]) => `<tr><td>${a}</td><td class="tag">${b}</td>` +
      `<td class="tag" style="text-align:left;white-space:normal">${c}</td></tr>`)
      .join('') + '</tbody>';
}

// ------------------------------------------------------------- tooltip
const tipEl = () => document.getElementById('tip');
function tip(node, html) {
  node.addEventListener('mousemove', e => {
    const t = tipEl(); t.innerHTML = html; t.hidden = false;
    t.style.left = Math.min(e.clientX + 14, innerWidth - 310) + 'px';
    t.style.top = Math.min(e.clientY + 14, innerHeight - 120) + 'px';
  });
  node.addEventListener('mouseleave', () => tipEl().hidden = true);
}
