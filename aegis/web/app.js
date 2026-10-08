/* All model/tool text is rendered with textContent. No HTML from evidence is executed. */
const $ = (id) => document.getElementById(id);
let config, token = '', active = null, filter = 'all', busy = false;
function el(tag, text, cls) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (cls) node.className = cls; return node; }
function notice(message, error = false) { $('notice').textContent = message; $('notice').className = 'notice' + (error ? ' error' : ''); $('notice').hidden = !message; }
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...(options.headers || {}) } });
  const data = await response.json(); if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`); return data;
}
function setBusy(value) { busy = value; document.body.classList.toggle('busy', value); ['run-button', 'identity', 'token', 'connect', 'approve', 'decline'].forEach(id => $(id).disabled = value); $('graph-state').textContent = value ? 'Executing' : active ? active.status.replaceAll('_', ' ') : 'Ready'; }
function identityRole() { return config.demo_identities[token]?.role || 'operator'; }
async function history() {
  if (!token) return;
  const expectedToken = token; const runs = await api('/api/runs'); if (expectedToken !== token) return; $('history').replaceChildren();
  if (!runs.length) $('history').append(el('p', 'No reviews in this tenant yet.', 'muted'));
  for (const run of runs) {
    const button = el('button', undefined, 'history-item'); const text = el('span', run.scenario.replaceAll('_', ' '));
    text.append(el('small', `${run.id.slice(0, 8)} · ${new Date(run.created * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`));
    button.append(text, el('span', run.status.replaceAll('_', ' ')));
    button.addEventListener('click', async () => { if (busy) return; const expected = token; try { const selected = await api(`/api/runs/${run.id}`); if (expected !== token) return; render(selected); notice(''); } catch (error) { if (expected === token) notice(error.message, true); } });
    $('history').append(button);
  }
}
const groups = { agent: ['plan', 'decision', 'observation', 'proposal', 'evaluation'], gateway: ['model_attempt', 'model_usage', 'cache_hit', 'fallback', 'circuit_open'], policy: ['policy_pass', 'blocked', 'approval', 'execution'] };
const titles = { plan: 'Specialists assigned', decision: 'Next action selected', observation: 'Tool observation recorded', proposal: 'Proposal assembled', evaluation: 'Evaluation recorded', model_attempt: 'Model request', model_usage: 'Provider usage', cache_hit: 'Exact response cache hit', fallback: 'Route failed · fallback considered', circuit_open: 'Open circuit · route skipped', policy_pass: 'Evidence policy passed', blocked: 'Boundary enforced', approval: 'Human decision recorded', execution: 'Local simulation executed', span: 'OpenTelemetry span' };
function eventSummary(event) {
  const p = event.payload;
  if (event.kind === 'span') return `${p.name} · ${p.duration_ms.toFixed(1)} ms · trace ${p.trace_id.slice(0, 12)}`;
  if (event.kind === 'decision') return p.action === 'tool' ? `Request ${p.tool}` : p.summary;
  if (event.kind === 'observation') return p.evidence ? `${p.evidence.source} → ${p.evidence.id}` : 'No evidence returned.';
  if (event.kind === 'model_usage') return `${p.route} · ${p.input_tokens} input / ${p.output_tokens} output tokens · ${p.provider_cached_tokens} provider cached`;
  if (event.kind === 'approval') return p.approved ? 'Operator approved the proposal digest.' : 'Operator declined the proposal.';
  return p.summary || p.reason || p.rationale || p.code || p.outcome || p.next_action || p.route || p.failed_route || 'Recorded in the run ledger.';
}
function timeline() {
  if (!active) return; $('timeline').replaceChildren();
  const events = active.events.filter(e => filter === 'all' || groups[filter].includes(e.kind));
  for (const event of events) {
    const row = el('article', undefined, `event ${event.kind}`); const body = el('div'); const head = el('div', undefined, 'event-head');
    row.append(el('span', `+${Math.max(0, event.at - active.created).toFixed(2)}s`, 'event-time'));
    head.append(el('span', event.agent, 'event-agent'), el('span', titles[event.kind] || event.kind, 'event-title'));
    const details = el('details'); details.append(el('summary', 'Inspect payload'), el('pre', JSON.stringify(event.payload, null, 2)));
    body.append(head, el('p', eventSummary(event)), details); row.append(body); $('timeline').append(row);
  }
  if (!events.length) $('timeline').append(el('p', 'No events in this filter.', 'muted'));
}
function render(run) {
  active = run; $('run-id').textContent = `${run.scenario} / ${run.id.slice(0, 12)}`;
  $('metric-calls').textContent = run.calls; $('metric-budget').textContent = `of ${run.max_calls} permitted attempts`;
  $('metric-units').textContent = `${run.units} / ${run.max_units}`; $('metric-cache').textContent = run.cache_hits;
  $('metric-provider').textContent = run.provider_cached_tokens; $('graph-state').textContent = run.status.replaceAll('_', ' ');
  $('outcome').textContent = run.status.replaceAll('_', ' '); $('outcome').className = 'pill' + (['blocked', 'failed', 'declined'].includes(run.status) ? ' subtle' : '');
  document.querySelectorAll('[data-agent]').forEach(node => {
    const events = run.events.filter(e => e.agent === node.dataset.agent);
    node.classList.toggle('done', events.length > 0); node.classList.toggle('blocked', events.some(e => e.kind === 'blocked'));
  });
  $('proposal').replaceChildren();
  if (run.proposal) {
    $('proposal').append(el('h3', run.proposal.recommendation.replaceAll('_', ' ').toUpperCase()), el('p', run.proposal.summary));
    if (run.proposal.rollback) $('proposal').append(el('p', `Rollback: ${run.proposal.rollback}`));
    run.proposal.citations.forEach(c => $('proposal').append(el('code', c, 'source')));
  } else $('proposal').append(el('p', run.error ? `Stopped: ${run.error}` : 'No proposal available.', 'muted'));
  if (run.error && run.proposal) $('proposal').append(el('p', `Policy result: ${run.error}`));
  $('approval').hidden = run.status !== 'awaiting_approval'; $('digest').textContent = `sha256:${run.proposal_digest || ''}`;
  $('approve').disabled = busy || identityRole() !== 'operator'; $('decline').disabled = busy || identityRole() !== 'operator';
  $('simulation').replaceChildren();
  if (run.result) $('simulation').append(el('strong', 'SIMULATION COMPLETE'), el('p', `Synthetic p95: ${run.result.baseline_p95_ms} → ${run.result.canary_p95_ms} ms. Rollback remains available. Next: review this simulated outcome; no production deployment occurred.`));
  $('evidence').replaceChildren();
  for (const finding of run.findings) {
    const card = el('article', undefined, 'evidence-card'); card.append(el('h3', `${finding.agent} / ${finding.evidence.age_seconds}s snapshot age`), el('p', finding.summary), el('code', finding.evidence.id), el('pre', JSON.stringify(finding.evidence.data, null, 2))); $('evidence').append(card);
  }
  if (!run.findings.length) $('evidence').append(el('div', 'No validated findings in this run.', 'evidence-empty'));
  $('download').disabled = false; timeline();
}
$('run-form').addEventListener('submit', async event => {
  event.preventDefault(); if (busy) return; setBusy(true); notice('Review in progress. Specialist agents are collecting evidence.');
  try {
    const run = await api('/api/runs', { method: 'POST', body: JSON.stringify({ goal: $('goal').value, scenario: $('scenario').value, max_calls: Number($('budget').value), cache: $('cache').checked }) });
    render(run); notice(run.status === 'awaiting_approval' ? 'Review complete. A durable approval checkpoint is waiting for an operator.' : `Review finished: ${run.status.replaceAll('_', ' ')}.`, ['blocked', 'failed'].includes(run.status)); await history();
  } catch (error) { notice(error.message, true); } finally { setBusy(false); if (active) render(active); }
});
async function approve(approved) {
  if (!active || busy) return; setBusy(true);
  try { render(await api(`/api/runs/${active.id}/approval`, { method: 'POST', body: JSON.stringify({ approved, proposal_digest: active.proposal_digest }) })); notice(approved ? 'Local canary simulation completed. Inspect the outcome and trace.' : 'Proposal declined. No action executed.'); await history(); }
  catch (error) { notice(error.message, true); } finally { setBusy(false); if (active) render(active); }
}
$('approve').addEventListener('click', () => approve(true)); $('decline').addEventListener('click', () => approve(false));
document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => { filter = button.dataset.filter; document.querySelectorAll('[data-filter]').forEach(b => b.classList.toggle('selected', b === button)); timeline(); }));
$('download').addEventListener('click', () => { if (!active) return; const url = URL.createObjectURL(new Blob([JSON.stringify(active, null, 2)], { type: 'application/json' })); const link = el('a'); link.href = url; link.download = `aegis-${active.id}.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); });
$('refresh').addEventListener('click', () => history().catch(error => notice(error.message, true)));
async function switchIdentity() {
  active = null; $('approval').hidden = true; $('download').disabled = true;
  $('timeline').replaceChildren(el('p', 'Select a run or launch a review for this identity.', 'muted'));
  $('proposal').replaceChildren(el('p', 'No selected review.', 'muted')); $('simulation').replaceChildren(); $('evidence').replaceChildren();
  ['metric-calls', 'metric-units', 'metric-cache', 'metric-provider'].forEach(id => $(id).textContent = '—');
  $('run-id').textContent = 'No active run'; $('outcome').textContent = 'Waiting'; $('graph-state').textContent = 'Ready';
  document.querySelectorAll('[data-agent]').forEach(n => n.classList.remove('done', 'blocked'));
  $('history').replaceChildren(); try { await history(); notice(''); } catch (error) { notice(error.message, true); }
}
$('identity').addEventListener('change', async () => { token = $('identity').value; $('custom-auth').hidden = token !== 'custom'; if (token === 'custom') token = ''; await switchIdentity(); });
$('connect').addEventListener('click', async () => { token = $('token').value; await switchIdentity(); });
async function init() {
  try {
    config = await (await fetch('/api/config')).json(); $('mode').textContent = `${config.mode} mode · v${config.version}`;
    Object.entries(config.scenarios).forEach(([value, label]) => { const option = el('option', label); option.value = value; $('scenario').append(option); });
    Object.entries(config.demo_identities).forEach(([value, identity]) => { const option = el('option', `${identity.tenant} / ${identity.role}`); option.value = value; $('identity').append(option); });
    const custom = el('option', 'Custom credential'); custom.value = 'custom'; $('identity').append(custom);
    token = Object.keys(config.demo_identities)[0] || ''; $('custom-auth').hidden = !!token;
    if (config.mode !== 'fixture') document.querySelector('.form-note').textContent = 'Proxy mode uses configured model providers. Evidence and actions remain synthetic; provider usage may incur charges.';
    await history();
  } catch (error) { notice(`Unable to connect: ${error.message}`, true); }
}
init();
