(() => {
  const app = document.querySelector('#app');
  const token = new URL(location.href).searchParams.get('action') || '';
  const state = {data: null, run: null, revision: null, mode: null, frame: 0,
    referenceCategory: null, referenceFrame: 0, activePanel: 'render', playing: false, page: 'review'};
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const run = () => state.data?.runs.find(item => item.run === state.run);
  const unique = items => [...new Set(items)];
  const label = value => String(value || '').replaceAll('_', ' ');
  const button = (text, handler, className) => {
    const node = el('button', text, className);
    node.type = 'button';
    node.onclick = async () => {try {await handler();} catch (error) {fail(error.message);}};
    return node;
  };
  function fail(message, success = false) {
    let notice = document.querySelector('#notice');
    if (!notice) {notice = el('p', '', 'error'); notice.id = 'notice'; notice.setAttribute('role', 'alert'); app.prepend(notice);}
    notice.className = success ? 'success' : 'error';
    notice.setAttribute('role', success ? 'status' : 'alert');
    notice.textContent = message;
  }
  function select(name, values, selected, handler) {
    const node = el('select');
    node.setAttribute('aria-label', name);
    values.forEach(value => {
      const item = typeof value === 'string' ? {value, text: label(value)} : value;
      const option = el('option', item.text);
      option.value = item.value; option.selected = item.value === selected; node.append(option);
    });
    node.onchange = () => handler(node.value);
    return node;
  }
  function field(name, control) {const node = el('label', name, 'field'); node.append(control); return node;}
  function section(number, title, description, className = '') {
    const node = el('section', undefined, 'section ' + className);
    const heading = el('div', undefined, 'section-heading');
    heading.append(el('div', number, 'section-number'));
    const copy = el('div'); copy.append(el('h2', title), el('p', description, 'muted')); heading.append(copy);
    node.append(heading); return node;
  }
  async function action(body) {
    const response = await fetch('/api/action', {method: 'POST', headers: {
      'Content-Type': 'application/json', 'X-ModelBench-Action': token}, body: JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || 'Action failed');
    await refresh();
  }
  const renderAssets = () => (run()?.assets || []).filter(item => item.revision === state.revision && item.mode === state.mode);
  function referenceAssets() {
    const registered = (run()?.assets || []).filter(item => item.revision === state.revision && item.mode === 'reference');
    const sources = run()?.references || [];
    return [...sources, ...registered.filter(item => !sources.some(source =>
      item.url === source.url || item.alias === 'snapshot/task/inputs/' + source.alias))
      .map(item => ({...item, category: 'reference', title: item.name}))];
  }
  const references = () => referenceAssets().filter(item => (item.category || 'reference') === state.referenceCategory);
  function setActivePanel(name) {
    state.activePanel = name;
    document.querySelectorAll('[data-panel]').forEach(node => {
      const active = node.dataset.panel === name;
      node.classList.toggle('active', active);
      node.querySelector('.panel-hint').textContent = active ? 'Arrow keys control this panel' : 'Hover to control';
    });
  }
  function cycle(name, delta) {
    const items = name === 'reference' ? references() : renderAssets();
    if (!items.length) return;
    const key = name === 'reference' ? 'referenceFrame' : 'frame';
    state[key] = (state[key] + delta + items.length) % items.length;
    if (name === 'render') state.playing = false;
    updateImages();
  }
  function zoomImage(source, title) {
    const dialog = el('dialog', undefined, 'image-dialog');
    const image = el('img'); image.src = source; image.alt = title;
    const close = button('Close image (Esc)', () => dialog.close());
    dialog.append(close, image); document.body.append(dialog);
    dialog.addEventListener('close', () => dialog.remove(), {once: true});
    dialog.addEventListener('click', event => {if (event.target === dialog) dialog.close();});
    dialog.showModal(); close.focus();
  }
  function imagePanel(name, title) {
    const panel = el('div', undefined, 'image-panel'); panel.dataset.panel = name; panel.tabIndex = 0;
    panel.setAttribute('aria-label', title + ' image panel');
    panel.onpointerenter = () => setActivePanel(name);
    panel.addEventListener('focusin', () => setActivePanel(name));
    const head = el('div', undefined, 'panel-heading');
    head.append(el('h3', title), el('small', '', 'panel-hint'));
    const filters = el('div', undefined, 'panel-filters');
    if (name === 'render') {
      const modes = unique((run().assets || []).filter(item => item.revision === state.revision && item.mode !== 'reference').map(item => item.mode));
      filters.append(field('Category', select('Render category', modes, state.mode, value => {
        state.mode = value; state.frame = 0; state.playing = false; render();
      })));
    } else {
      const categories = unique(referenceAssets().map(item => item.category || 'reference'));
      filters.append(field('Category', select('Reference category', categories, state.referenceCategory, value => {
        state.referenceCategory = value; state.referenceFrame = 0; render();
      })));
    }
    const picker = select(title + ' image', [], '', value => {
      state[name === 'render' ? 'frame' : 'referenceFrame'] = Number(value);
      if (name === 'render') state.playing = false;
      updateImages();
    });
    picker.className = 'image-picker'; filters.append(field('Image', picker));
    const stage = el('div', undefined, 'image-stage');
    const image = el('img', undefined, name === 'render' ? 'hero' : 'reference-image');
    image.hidden = true;
    image.onerror = () => {image.hidden = true; empty.hidden = false; empty.textContent = 'Image unavailable. Refresh to check the source.';};
    image.onclick = () => zoomImage(image.src, image.alt);
    const empty = el('p', 'No images available in this category.', 'empty-state'); stage.append(image, empty);
    const navigation = el('div', undefined, 'image-navigation');
    const prev = button('Previous', () => cycle(name, -1)); prev.setAttribute('aria-label', 'Previous ' + name + ' image');
    const next = button('Next', () => cycle(name, 1)); next.setAttribute('aria-label', 'Next ' + name + ' image');
    const count = el('output', '', 'image-count'); count.setAttribute('aria-live', 'polite');
    navigation.append(prev, count, next);
    const caption = el('div', undefined, 'image-caption');
    panel.append(head, filters, stage, navigation, caption);
    if (name === 'render') {
      const timeline = el('div', undefined, 'timeline');
      const play = button('Play', () => {state.playing = !state.playing; updateImages();}); play.id = 'play';
      const slider = el('input'); slider.type = 'range'; slider.min = '0'; slider.setAttribute('aria-label', 'Render frame');
      slider.oninput = () => {state.frame = Number(slider.value); state.playing = false; updateImages();};
      timeline.append(play, slider); panel.append(timeline);
    }
    return panel;
  }
  function updateImages() {
    clearTimeout(state.timer);
    for (const name of ['render', 'reference']) {
      const panel = document.querySelector('[data-panel="' + name + '"]'); if (!panel) continue;
      const items = name === 'render' ? renderAssets() : references();
      const key = name === 'render' ? 'frame' : 'referenceFrame';
      state[key] = Math.max(0, Math.min(state[key], items.length - 1));
      const selected = items[state[key]], image = panel.querySelector('.image-stage img');
      const picker = panel.querySelector('.image-picker');
      picker.replaceChildren(...items.map((item, index) => {
        const option = el('option', item.title || item.name); option.value = String(index); return option;
      }));
      picker.value = String(state[key]); picker.disabled = !items.length;
      panel.querySelectorAll('.image-navigation button').forEach(node => node.disabled = items.length < 2);
      const count = panel.querySelector('.image-count');
      count.textContent = selected ? (name === 'render' ? 'frame ' + (selected.frame ?? state[key]) + ' ' : '') + '(' + (state[key] + 1) + '/' + items.length + ')' : '0 images';
      image.hidden = !selected; panel.querySelector('.empty-state').hidden = Boolean(selected);
      if (selected) {
        if (image.getAttribute('src') !== selected.url) image.src = selected.url;
        image.alt = selected.title || [selected.revision, selected.camera, selected.name].filter(Boolean).join(' ');
      } else image.removeAttribute('src');
      const caption = panel.querySelector('.image-caption'); caption.replaceChildren();
      if (selected) {
        caption.append(el('strong', selected.title || selected.name));
        if (selected.notes) {
          const detail = el('details'); detail.append(el('summary', 'Reference notes'), el('p', selected.notes)); caption.append(detail);
        }
      }
      if (name === 'render') {
        if (items.length < 2) state.playing = false;
        const slider = panel.querySelector('input[type="range"]');
        slider.max = String(Math.max(0, items.length - 1)); slider.value = String(state.frame); slider.disabled = items.length < 2;
        const play = panel.querySelector('#play'); play.textContent = state.playing ? 'Stop' : 'Play'; play.disabled = items.length < 2;
      }
    }
    updatePrevious(); setActivePanel(state.activePanel);
    if (state.playing) state.timer = setTimeout(() => {state.frame = (state.frame + 1) % renderAssets().length; updateImages();}, 700);
  }
  function updatePrevious() {
    const container = document.querySelector('#previous-image'); if (!container) return;
    container.replaceChildren();
    const current = renderAssets()[state.frame];
    const revisions = unique((run()?.assets || []).map(item => item.revision));
    const previous = revisions[revisions.indexOf(state.revision) - 1];
    const old = current && (run().assets || []).find(item => item.revision === previous && item.mode === current.mode && item.kind === current.kind && item.camera === current.camera && item.frame === current.frame);
    if (!old) {container.append(el('p', 'No matching view in the previous revision.', 'muted')); return;}
    const image = el('img', undefined, 'compare'); image.src = old.url; image.alt = 'Previous ' + previous + ' ' + old.name;
    image.onclick = () => zoomImage(old.url, image.alt); container.append(el('p', previous, 'muted'), image);
  }
  function provenance(parent) {
    const data = run().provenance?.[state.revision] || {};
    const strip = el('div', undefined, 'provenance');
    for (const role of ['builder', 'verifier']) {
      const exact = data[role];
      const context = data[role + '_context']?.invocation;
      const invocation = exact || context;
      const card = el('div', undefined, 'model-card');
      card.append(el('div', context && !exact ? 'Run ' + role : role === 'builder' ? 'Produced by' : 'Verified by', 'eyebrow'));
      const model = invocation?.observed_model || invocation?.requested_model;
      const effort = invocation?.observed_effort || invocation?.requested_effort;
      const line = el('div', undefined, 'model-line');
      line.append(el('strong', model || 'Not recorded'), el('b', effort ? effort + ' effort' : 'Effort unknown', 'effort-badge'));
      card.append(line, el('small', context && !exact ? 'Recorded run invocation; revision attribution unavailable' : invocation ? (invocation.observed_model ? 'Runtime confirmed' : 'Recorded invocation request') : 'No invocation linked to this revision', 'muted'));
      strip.append(card);
    }
    parent.append(strip);
  }
  function viewer(parent) {
    const record = run(), all = record.assets || [];
    const revisions = unique([...(record.review?.revisions || []).map(item => item.id), ...all.map(item => item.revision)]);
    if (!revisions.includes(state.revision)) state.revision = revisions.at(-1) || null;
    const modes = unique(all.filter(item => item.revision === state.revision && item.mode !== 'reference').map(item => item.mode));
    if (!modes.includes(state.mode)) state.mode = modes.includes('final') ? 'final' : modes.includes('motion') ? 'motion' : modes[0] || null;
    const categories = unique(referenceAssets().map(item => item.category || 'reference'));
    if (!categories.includes(state.referenceCategory)) {state.referenceCategory = categories[0] || null; state.referenceFrame = 0;}
    const node = section('01', 'Compare the evidence', 'Inspect the render against its reference. Hover either panel, then use left and right arrow keys.', 'viewer');
    node.append(field('Viewing revision', select('Revision', revisions, state.revision, value => {
      state.revision = value; state.frame = 0; state.referenceFrame = 0; state.playing = false; render();
    })));
    provenance(node);
    const panels = el('div', undefined, 'comparison-grid'); panels.append(imagePanel('render', 'Render'), imagePanel('reference', 'Reference')); node.append(panels);
    const previous = el('details', undefined, 'previous-revision');
    previous.append(el('summary', 'Compare with the previous revision'));
    const content = el('div'); content.id = 'previous-image'; previous.append(content); node.append(previous); parent.append(node);
  }
  function evidenceAsset(evidence) {
    const assets = run().assets || [];
    const detail = typeof evidence === 'string' ? {path: evidence} : evidence || {};
    const citation = detail.path || detail.evidence_path;
    const alias = typeof citation === 'string' ? citation.split('#', 1)[0] : citation;
    if (typeof alias === 'string') {
      const exact = assets.find(candidate => candidate.alias === alias);
      if (exact) return exact;
      const parts = alias.split('/').filter(Boolean), name = parts.at(-1);
      const copy = /(?:^|\/)revisions\/(revision_\d+)\/verification\/(?:inputs\/)?([^/]+)\/([^/]+)$/.exec(alias);
      const revision = detail.revision || copy?.[1] || parts.find(part => /^revision_\d+$/.test(part));
      const category = detail.category || detail.kind || copy?.[2] || assets.map(candidate => candidate.mode).find(mode => parts.includes(mode));
      const matches = assets.filter(candidate => candidate.name === (copy?.[3] || name) &&
        (!revision || candidate.revision === revision) && (!category || candidate.mode === category));
      if (matches.length === 1) return matches[0];
    }
    if (detail.frame != null || detail.view != null) {
      const matches = assets.filter(candidate => (detail.frame == null || candidate.frame === detail.frame) &&
        (detail.view == null || candidate.camera === detail.view) &&
        (!detail.revision || candidate.revision === detail.revision) && (!detail.category || candidate.mode === detail.category));
      return matches.length === 1 ? matches[0] : null;
    }
    return null;
  }
  function jumpToEvidence(evidence) {
    const item = evidenceAsset(evidence);
    if (!item) {fail('This finding has no registered image to display. See its details below.'); return;}
    state.revision = item.revision; state.playing = false;
    if (item.mode === 'reference') {
      const match = referenceAssets().find(source => source.alias === item.alias || source.url === item.url || source.name === item.name);
      state.referenceCategory = match?.category || 'reference'; state.referenceFrame = Math.max(0, references().indexOf(match)); state.activePanel = 'reference';
    } else {state.mode = item.mode; state.frame = renderAssets().findIndex(source => source.id === item.id); state.activePanel = 'render';}
    render(); document.querySelector('.viewer').scrollIntoView({behavior: 'smooth', block: 'start'});
  }
  function ledger(parent) {
    const tasks = run().review?.tasks || [], unresolved = tasks.filter(task => task.status !== 'verifier_resolved');
    const node = section('02', 'Findings & corrections', unresolved.length ? unresolved.length + ' items need attention. Open a finding to inspect its evidence and history.' : 'No unresolved corrections recorded.', 'ledger');
    if (unresolved.length) node.querySelector('.section-heading p').classList.add('attention');
    const discoveryLedger = run().discovery;
    const discovery = discoveryLedger?.specification;
    if (discoveryLedger) {
      const target = discovery?.target || {}, claims = discovery?.claims || [], entities = discovery?.entities || [], requirements = discovery?.requirements || [];
      const panel = el('details', undefined, 'finding'); panel.open = true;
      const active = discoveryLedger.active || 'not approved';
      panel.append(el('summary', 'Discovered specification'), el('p', (target.identity || 'Target pending') + ' / ' + (target.status || discoveryLedger.stage || 'unknown')));
      panel.append(el('p', 'Stage: ' + (discoveryLedger.stage || 'unknown') + '; active version: ' + active + '; versions: ' + (discoveryLedger.versions || []).length + '; working seed: ' + (discoveryLedger.working_seed || 'none')));
      if (discoveryLedger.question) panel.append(el('p', 'Needs input: ' + discoveryLedger.question, 'attention'));
      const rows = [
        ['Entities', entities, value => value.name || value.id],
        ['Requirements', requirements, value => (value.critical ? 'Critical: ' : '') + (value.criterion || value.id)],
        ['Claims', claims, value => '[' + (value.kind || 'unknown') + '] ' + (value.statement || value.id)],
        ['References', discovery?.references || [], value => value.applicability || value.id],
        ['Candidate selection', discoveryLedger.candidates || [], value => (value.id || value.artifact || 'candidate') + (value.selected ? ' (selected)' : '')],
      ];
      for (const [name, values, describe] of rows) {
        const detail = el('details'); detail.open = true; detail.append(el('summary', name + ' (' + values.length + ')'));
        values.forEach(value => detail.append(el('p', describe(value)))); panel.append(detail);
      }
      const audit = discoveryLedger.last_audit;
      if (audit) panel.append(el('p', 'Latest audit: ' + (audit.decision || 'recorded') + ' - ' + (audit.assessment || '')));
      node.append(panel);
      const reconstructionPanel = el('details', undefined, 'finding');
      reconstructionPanel.open = true;
      reconstructionPanel.append(el('summary', 'Reconstruction evidence'));
      for (const record of discoveryLedger.reconstructions || []) {
        const solution = record.solution || {}, item = el('details');
        item.append(el('summary', record.id + ': ' + (solution.mathematical_status || record.status || 'unassessed')));
        item.append(el('p', 'Selected hypothesis: ' + (record.selected_hypothesis || 'historical / unavailable') + '; convergence: ' + (solution.convergence_status || 'unavailable')));
        for (const [name, values] of [['Assumptions', solution.assumptions], ['Camera hypotheses', solution.camera_hypotheses], ['Measurements and conditional uncertainty', solution.measurements], ['Residuals and conflicts', solution.constraints], ['Unresolved degrees of freedom', solution.unresolved_degrees_of_freedom]]) {
          const detail = el('details'); detail.append(el('summary', name), el('pre', JSON.stringify(values || [], null, 2))); item.append(detail);
        }
        for (const file of record.files || []) {
          if (!file.path.endsWith('.png')) continue;
          const evidence = {path: record.path + '/' + file.path};
          if (evidenceAsset(evidence)) item.append(button('View ' + file.path.split('/').pop(), () => jumpToEvidence(evidence)));
        }
        reconstructionPanel.append(item);
      }
      if (!(discoveryLedger.reconstructions || []).length) reconstructionPanel.append(el('p', 'No reconstruction evidence required.'));
      node.append(reconstructionPanel);
      const modelPanel = el('details', undefined, 'finding'); modelPanel.open = true; modelPanel.append(el('summary', 'Model-to-reference agreement'));
      for (const revision of run().review?.revisions || []) {
        const fits = revision.evaluation?.reference_fit || {};
        for (const [id, fit] of Object.entries(fits)) {
          const detail = el('details'); detail.append(el('summary', revision.id + ' / ' + id + ': ' + (fit.assessment || 'unassessed')),
            el('p', fit.reason || 'Candidate geometry evaluated under the approved fixed camera.'), el('pre', JSON.stringify(fit.metrics || {}, null, 2)));
          for (const evidence of revision.evidence || []) if (evidence.kind === 'reference' && evidence.path.endsWith('.png') && evidenceAsset(evidence)) detail.append(button('View ' + evidence.path.split('/').pop(), () => jumpToEvidence(evidence)));
          modelPanel.append(detail);
        }
      }
      node.append(modelPanel);
    }
    const appendTask = (target, task) => {
      const detail = el('details', undefined, 'finding');
      if (task.status !== 'verifier_resolved') detail.classList.add('unresolved');
      const summary = el('summary'); summary.append(el('b', label(task.status), 'status-tag'), el('strong', task.requirement));
      detail.append(summary, el('p', task.category === 'reconstruction_assumption' ? 'Reconstruction assumptions / unassessed comparison' : 'Model geometry'), el('p', task.instruction || ''));
      for (const evidence of [...(task.evidence || []), ...(task.history || [])]) {
        const row = el('div', undefined, 'evidence-row');
        if (evidenceAsset(evidence))
          row.append(button('View evidence ' + (evidence.frame ?? ''), () => jumpToEvidence(evidence)));
        row.append(el('p', evidence.response || evidence.reason || (evidence.actual !== undefined ? 'Measured ' + JSON.stringify(evidence.actual) + '; expected ' + JSON.stringify(evidence.expected) + '; tolerance ' + evidence.tolerance : evidence.instruction || label(evidence.type))));
        detail.append(row);
      }
      for (const response of task.responses || []) detail.append(el('p', 'Builder: ' + response.response));
      target.append(detail);
    };
    unresolved.forEach(task => appendTask(node, task));
    const resolved = tasks.filter(task => task.status === 'verifier_resolved');
    if (resolved.length) {const archive = el('details'); archive.append(el('summary', resolved.length + ' resolved findings')); resolved.forEach(task => appendTask(archive, task)); node.append(archive);}
    parent.append(node);
  }
  function controls(parent) {
    const record = run(), node = section('03', 'Review actions', 'Send a correction or manage the selected run.');
    if (record.state === 'awaiting_feedback') {
      const feedback = el('form', undefined, 'feedback-form');
      const message = el('textarea'); message.placeholder = 'Describe input propagation'; message.required = true;
      const send = el('button', 'Submit system feedback', 'primary'); send.type = 'submit';
      feedback.append(el('p', 'Confirm whether inputs passed through ModelBench correctly. This is diagnostic system feedback, not a review of generated artifacts.', 'muted'), field('What input propagation should we investigate?', message), send);
      feedback.onsubmit = async event => {event.preventDefault(); try {await action({action: 'system-feedback', run: record.run, message: message.value}); fail('System feedback recorded.', true); message.value = '';} catch (error) {fail(error.message);}};
      node.append(feedback);
    }
    const commands = el('div', undefined, 'toolbar');
    for (const name of ['open', 'pause', 'resume', 'cancel', 'revise', 'verify']) commands.append(button(name === 'open' ? 'Open in Blender' : label(name), () => action({action: name, run: record.run}), name === 'cancel' ? 'danger' : ''));
    node.append(commands);
    const settings = el('details'); settings.append(el('summary', 'Model settings for subsequent work'));
    for (const role of ['builder', 'verifier']) {
      const pending = record.models?.profiles?.[role];
      settings.append(el('p', 'Next ' + role + ': ' + (pending?.model || 'Not recorded') + ' / ' + (pending?.effort || 'Not recorded') + ' effort', 'muted'));
    }
    const form = el('form', undefined, 'settings-form');
    const role = select('Model role', ['builder', 'verifier', 'both'], 'builder', () => {});
    const model = el('input'); model.placeholder = 'model'; model.required = true;
    const effort = el('input'); effort.placeholder = 'effort'; effort.required = true;
    const save = el('button', 'Set model'); save.type = 'submit';
    form.append(field('Role', role), field('Model', model), field('Effort', effort), save);
    form.onsubmit = async event => {event.preventDefault(); try {await action({action: 'set-model', run: record.run, role: role.value, model: model.value, effort: effort.value});} catch (error) {fail(error.message);}};
    settings.append(form); node.append(settings);
    const logs = el('details'); logs.append(el('summary', 'Technical logs & full ledger'), el('pre', JSON.stringify({models: record.models, review: record.review, workers: state.data.workers}, null, 2))); node.append(logs);
    parent.append(node);
  }
  async function upload(file) {
    const data = await new Promise((resolve, reject) => {const reader = new FileReader(); reader.onerror = () => reject(Error('Could not read reference')); reader.onload = () => resolve(String(reader.result).split(',', 2)[1]); reader.readAsDataURL(file);});
    const response = await fetch('/api/upload', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-ModelBench-Action': token}, body: JSON.stringify({media_type: file.type, data})});
    const result = await response.json(); if (!response.ok) throw Error(result.error || 'Upload failed'); return result.id;
  }
  function start(parent) {
    const node = section('NEW', 'Start a new run', 'Run an existing task or create a sparse brief with optional reference files.', 'new-run');
    const form = el('form', undefined, 'start-form');
    const mode = select('Run type', [{value: 'existing', text: 'Existing task'}, {value: 'brief', text: 'Brief'}], 'existing', () => update());
    const task = select('Task', state.data.tasks || [], '', () => {});
    const brief = document.createElement('textarea'); brief.required = true; brief.maxLength = 16000; brief.placeholder = 'Describe the asset and intended result';
    const notes = document.createElement('textarea'); notes.maxLength = 16000; notes.placeholder = 'Optional context, constraints, or questions';
    const files = document.createElement('input'); files.type = 'file'; files.multiple = true; files.accept = '.png,.jpg,.jpeg,.webp,.pdf,.txt,image/png,image/jpeg,image/webp,application/pdf,text/plain';
    const research = select('Research mode', ['live', 'offline'], 'live', () => {});
    const agent = el('input'); agent.value = 'codex'; agent.required = true;
    const verifier = el('input'); verifier.value = 'codex'; verifier.required = true;
    const efforts = [{value: '', text: 'Agent default'}, 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'];
    const builderEffort = select('Builder effort', efforts, '', () => {}), verifierEffort = select('Verifier effort', efforts, '', () => {});
    const budget = el('input'); budget.type = 'number'; budget.min = '1'; budget.max = '86400'; budget.step = '1'; budget.placeholder = 'Optional seconds';
    const submit = el('button', 'Start run', 'primary'); submit.type = 'submit';
    const existingFields = [field('Task', task)], briefFields = [field('Brief', brief), field('Notes', notes), field('Reference files', files), field('Research mode', research)];
    const shared = [field('Builder agent', agent), field('Builder effort', builderEffort), field('Verifier agent', verifier), field('Verifier effort', verifierEffort), field('Budget seconds', budget), submit];
    form.append(field('Run type', mode), ...existingFields, ...briefFields, ...shared);
    function update() { const isBrief = mode.value === 'brief'; existingFields.forEach(item => item.hidden = isBrief); briefFields.forEach(item => item.hidden = !isBrief); brief.required = isBrief; submit.disabled = !isBrief && !state.data.tasks?.length; submit.textContent = isBrief ? 'Create brief and start run' : 'Start run'; } update();
    form.onsubmit = async event => { event.preventDefault(); submit.disabled = true; try {
      let request;
      if (mode.value === 'brief') { const selected = [...files.files]; if (selected.length > 16) throw Error('Choose at most 16 reference files'); const references = []; for (const file of selected) { if (!['image/png','image/jpeg','image/webp','application/pdf','text/plain'].includes(file.type)) throw Error('References must be PNG, JPEG, WebP, PDF, or text'); references.push(await upload(file)); } request = {action: 'brief-run', brief: brief.value, notes: notes.value, references, research_mode: research.value, agent: agent.value, verifier: verifier.value}; }
      else request = {action: 'run', task: task.value, agent: agent.value, verifier: verifier.value};
      if (builderEffort.value) request.builder_effort = builderEffort.value; if (verifierEffort.value) request.verifier_effort = verifierEffort.value;
      if (budget.value) request.budget_seconds = Number(budget.value);
      await action(request); fail('Run start requested. Switch to Review runs and refresh to see progress.', true);
    } catch (error) {fail(error.message);} finally {update();} };
    node.append(form); parent.append(node);
  }
  function render() {
    clearTimeout(state.timer); app.replaceChildren(); if (!state.data) return;
    const nav = el('nav', undefined, 'page-tabs'); nav.setAttribute('aria-label', 'Workspace');
    for (const [name, title] of [['review', 'Review runs'], ['new', 'New run']]) {
      const tab = button(title, () => {state.page = name; state.playing = false; render();});
      if (state.page === name) tab.setAttribute('aria-current', 'page'); nav.append(tab);
    }
    app.append(nav);
    if (state.page === 'new') {start(app); return;}
    if (!run()) state.run = state.data.runs[0]?.run || null;
    const context = el('div', undefined, 'run-context');
    const heading = el('div'); heading.append(el('div', 'REVIEW WORKSPACE', 'eyebrow'), el('h1', label(run()?.task_id || state.run?.split('/')[0] || 'Evidence review')));
    context.append(heading, field('Selected run', select('Run', state.data.runs.map(item => ({value: item.run, text: item.run})), state.run, value => {
      Object.assign(state, {run: value, revision: null, mode: null, frame: 0, referenceCategory: null, referenceFrame: 0, playing: false}); render();
    })));
    app.append(context);
    if (!run()) {app.append(el('p', 'No runs yet. Open New run to begin.', 'empty-state')); return;}
    const record = run(), summary = el('div', undefined, 'run-summary');
    const unresolved = (record.review?.tasks || []).filter(task => task.status !== 'verifier_resolved').length;
    summary.append(el('b', label(record.state), 'status-tag'), el('strong', unresolved + ' unresolved corrections', unresolved ? 'attention' : 'muted'));
    for (const invocation of (record.models?.invocations || []).filter(item => !item.finished_at)) {
      const activity = invocation.last_output_at || invocation.heartbeat_at || invocation.requested_at;
      summary.append(el('small', 'Running now: ' + invocation.role + ' / ' + invocation.requested_model + ' / ' + invocation.requested_effort + ' effort' + (activity ? ' | activity ' + new Date(activity).toLocaleTimeString() : ''), 'muted'));
    }
    app.append(summary); viewer(app); ledger(app); controls(app); updateImages();
  }
  async function refresh() {
    try {
      const response = await fetch('/api/status', {cache: 'no-store'}), data = await response.json();
      if (!response.ok) throw Error(data.error || 'Status unavailable');
      state.data = {...data, runs: [...data.runs].sort((a, b) => (b.activity_at || b.updated_at || b.run).localeCompare(a.activity_at || a.updated_at || a.run))}; render();
    } catch (error) {fail(error.message);}
  }
  document.querySelector('#refresh').onclick = refresh;
  document.addEventListener('keydown', event => {
    if (state.page !== 'review' || !run() || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || document.querySelector('dialog[open]')) return;
    if (event.target.closest('input,select,textarea,[contenteditable="true"],[role="textbox"]')) return;
    if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {event.preventDefault(); cycle(state.activePanel, event.key === 'ArrowRight' ? 1 : -1);}
    if (event.key === ' ' && !event.target.closest('button,summary,a') && state.activePanel === 'render') {event.preventDefault(); state.playing = !state.playing; updateImages();}
  });
  refresh();
})();
