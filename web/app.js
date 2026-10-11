const $ = id => document.getElementById(id);
let result = null, busy = false, connected = false, started = 0, timer = null;
let sessionId = null;
let characterProfiles = [];
async function api(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
function error(message) { $('error').textContent = message; $('error').hidden = !message; }
function setBusy(value) {
  busy = value; $('generate').disabled = value || !connected;
  $('model').disabled = value; $('unload').disabled = value;
  $('shutdown').disabled = value;
  for (const id of ['session', 'session-name', 'background', 'new-session', 'save-context', 'thinking', 'refine', 'add-character']) $(id).disabled = value;
  document.querySelectorAll('#character-profiles input, #character-profiles textarea, #character-profiles select, #character-profiles button').forEach(node => { node.disabled = value; });
  $('progress').hidden = !value;
  for (const id of ['copy-positive', 'copy-all', 'download']) $(id).disabled = value || !result;
}
async function refresh() {
  $('refresh').disabled = true;
  try {
    const status = await api('/api/status');
    const previous = $('model').value || localStorage.getItem('anima-model') || 'qwen3:8b';
    $('model').replaceChildren();
    for (const model of status.models) $('model').add(new Option(model, model));
    if (status.models.includes(previous)) $('model').value = previous;
    if (!status.models.length) $('model').add(new Option('Qwen 모델을 설치해 주세요', ''));
    connected = !status.connection_error && !!status.catalog && status.models.length > 0;
    $('connection').textContent = status.catalog_error ? `CSV 로딩 실패: ${status.catalog_error}` : status.connection_error || (status.models.length ? 'Ollama 연결됨 · 모든 생성 요청은 이 PC에서 처리됩니다.' : 'Ollama 연결됨 · 아직 설치된 Qwen 모델이 없습니다.');
    $('setup').open = !connected;
    if (status.catalog) {
      $('catalog-count').textContent = `${status.catalog.count.toLocaleString()}개 태그 · 한국어 설명 ${status.catalog.korean_descriptions.toLocaleString()}개`;
      $('catalog-source').textContent = `${status.catalog.path}\nSHA256: ${status.catalog.sha256}`;
    }
    $('generate').disabled = busy || !connected;
    if (status.active_job && !busy) { setBusy(true); started = Date.now(); await poll(status.active_job); }
  } catch (exc) { connected = false; $('connection').textContent = exc.message; $('generate').disabled = true; }
  finally { $('refresh').disabled = false; }
}
function tagRow(tag, section = '') {
  const row = document.createElement('div'); row.className = 'tag-row';
  const name = document.createElement('span'); name.className = 'tag-name'; name.textContent = tag.rendered || tag.name.replaceAll('_', ' ');
  const category = document.createElement('span'); category.className = 'tag-section'; category.textContent = section || `빈도 ${tag.count.toLocaleString()}`;
  const description = document.createElement('p'); description.className = 'tag-description'; description.textContent = tag.description;
  row.append(name, category, description); return row;
}
function render(data) {
  result = data; $('empty').hidden = true; $('output').hidden = false; $('output').textContent = data.text;
  $('result-meta').textContent = `${data.seconds}초 · ${data.model}`;
  $('audit').replaceChildren(...data.verified.map(tag => tagRow(tag, tag.section)));
  $('audit-summary').textContent = `CSV 확인 ${data.verified.length}개 · 사용자 지정 이름 ${(data.custom_names || []).length}개 · 최종 후보 제외 ${data.excluded.length}개`;
  $('excluded').replaceChildren();
  for (const tag of data.excluded) { const p = document.createElement('p'); p.textContent = `${tag.section} / ${tag.tag} — ${tag.reason}`; $('excluded').append(p); }
  for (const tag of data.custom_names || []) { const p = document.createElement('p'); p.textContent = `${tag.section} / ${tag.name} — ${tag.reason}`; $('excluded').append(p); }
  if (data.draft_unverified.length) { const p = document.createElement('p'); p.textContent = `초안에서 미확인되어 교정에 전달한 후보: ${data.draft_unverified.join(', ')}`; $('excluded').append(p); }
  if (data.unload_error) error(`생성은 완료됐지만 메모리 해제를 확인하지 못했습니다: ${data.unload_error}`);
  else $('copy-feedback').textContent = data.unload_requested ? 'Qwen 메모리 해제 완료' : '연속 생성을 위해 모델을 최대 5분간 유지합니다.';
  if (data.save_error) error(data.save_error);
  if (data.context) $('context-state').textContent = `고정 설정 ${data.context.pinned_context ? '포함' : '없음'} · 최근 ${data.context.included_turns}회 포함 · 이전 ${data.context.omitted_turns}회 생략 · 입력 약 ${data.context.estimated_input_tokens.toLocaleString()} 토큰`;
}
async function poll(jobId) {
  timer = setInterval(() => { $('elapsed').textContent = `${Math.floor((Date.now() - started) / 1000)}초`; }, 1000);
  try {
    while (true) {
      const job = await api(`/api/jobs/${jobId}`);
      if (job.status === 'done') { render(job.result); await loadHistory(); break; }
      if (job.status === 'error') throw new Error(job.error);
      $('stage').textContent = job.stage;
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
  } catch (exc) { error(exc.message); }
  finally { clearInterval(timer); setBusy(false); }
}
$('generate-form').addEventListener('submit', async event => {
  event.preventDefault(); if (busy) return;
  error(''); setBusy(true); started = Date.now(); $('stage').textContent = '생성을 준비하고 있습니다.'; $('elapsed').textContent = '0초';
  $('copy-feedback').textContent = ''; localStorage.setItem('anima-model', $('model').value);
  try {
    await saveSession();
    const data = await api('/api/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt: $('prompt').value, model: $('model').value, unload: $('unload').checked, session_id:sessionId, thinking:$('thinking').checked, refine:$('refine').checked }) });
    await poll(data.job_id);
  } catch (exc) { error(exc.message); setBusy(false); }
});
$('refresh').addEventListener('click', refresh);
$('shutdown').addEventListener('click', async () => {
  try {
    await saveSession();
    await api('/api/shutdown', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
    connected = false; $('generate').disabled = true; $('refresh').disabled = true; $('shutdown').disabled = true;
    $('connection').textContent = '앱이 종료되었습니다. 다시 실행하려면 start.cmd를 열어 주세요.';
  } catch (exc) { error(exc.message); }
});
$('prompt').addEventListener('input', () => { $('char-count').textContent = `${$('prompt').value.length.toLocaleString()} / 6,000`; });
document.querySelectorAll('[data-example]').forEach(button => button.addEventListener('click', () => { $('prompt').value = button.dataset.example; $('prompt').dispatchEvent(new Event('input')); $('prompt').focus(); }));
async function copy(kind) {
  if (!result) return;
  try { await navigator.clipboard.writeText(result[kind]); $('copy-feedback').textContent = '클립보드에 복사했습니다.'; }
  catch { $('copy-feedback').textContent = '자동 복사에 실패했습니다. 결과 영역을 선택해 복사해 주세요.'; }
}
$('copy-positive').addEventListener('click', () => copy('positive'));
$('copy-all').addEventListener('click', () => copy('markdown'));
$('download').addEventListener('click', () => {
  if (!result) return;
  const url = URL.createObjectURL(new Blob([result.text], { type: 'text/plain;charset=utf-8' }));
  const a = document.createElement('a'); a.href = url; a.download = 'animepromptloom-prompt.txt'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
let searchTimer, searchVersion = 0;
$('search').addEventListener('input', () => {
  clearTimeout(searchTimer); const version = ++searchVersion, query = $('search').value.trim();
  if (!query) { $('search-results').replaceChildren(); return; }
  searchTimer = setTimeout(async () => {
    try { const rows = await api(`/api/search?q=${encodeURIComponent(query)}`); if (version !== searchVersion) return; $('search-results').replaceChildren(...rows.map(tag => tagRow(tag))); if (!rows.length) $('search-results').textContent = 'CSV에서 찾지 못했습니다.'; }
    catch (exc) { if (version === searchVersion) $('search-results').textContent = exc.message; }
  }, 250);
});
async function listSessions() {
  const rows = await api('/api/sessions');
  $('session').replaceChildren(...rows.map(row => new Option(`${row.name} · ${row.turns}회`, row.id)));
  if (sessionId) $('session').value = sessionId;
  return rows;
}
async function saveSession() {
  const data = await api('/api/sessions', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id:sessionId, name:$('session-name').value, character_profiles:characterProfiles, background:$('background').value})});
  sessionId = data.id; localStorage.setItem('anima-session', sessionId);
  $('context-state').textContent = '고정 설정 저장됨';
  await listSessions(); return data;
}
function renderHistory(session) {
  $('history-count').textContent = `저장 ${session.total_turns}회 · 최근 20회 표시`;
  $('history').replaceChildren();
  for (const turn of [...session.turns].reverse()) {
    const row = document.createElement('div'); row.className='history-turn';
    const date = document.createElement('time'); date.textContent = new Date(turn.created_at).toLocaleString('ko-KR');
    const button = document.createElement('button'); button.textContent='이 결과 보기';
    button.addEventListener('click', () => { if(busy) return; error(''); render(turn.result); setBusy(false); });
    const prompt = document.createElement('p'); prompt.textContent=turn.prompt;
    row.append(date,button,prompt); $('history').append(row);
  }
  if (!session.turns.length) $('history').textContent='아직 대화 기록이 없습니다.';
}
async function loadHistory() {
  if (!sessionId) return;
  renderHistory(await api(`/api/sessions/${sessionId}`)); await listSessions();
}
async function loadSession(id) {
  const session = await api(`/api/sessions/${id}`); sessionId=id;
  $('session').value=id; $('session-name').value=session.name; characterProfiles=session.character_profiles || []; renderCharacters(); $('background').value=session.background;
  localStorage.setItem('anima-session',id); $('context-state').textContent='저장된 설정 불러옴';
  result=null; $('output').hidden=true; $('empty').hidden=false; $('result-meta').textContent='출력을 기다리는 중';
  $('audit').replaceChildren(); $('excluded').replaceChildren(); $('copy-feedback').textContent=''; error('');
  $('audit-summary').textContent='생성하면 태그의 한국어 설명과 제외 사유가 표시됩니다.';
  renderHistory(session); setBusy(false);
}
$('save-context').addEventListener('click', () => saveSession().catch(exc=>error(exc.message)));
$('session').addEventListener('change', async () => {
  const next=$('session').value;
  try { await saveSession(); await loadSession(next); $('prompt').value=''; $('prompt').dispatchEvent(new Event('input')); }
  catch(exc){error(exc.message);}
});
$('new-session').addEventListener('click', async () => {
  try {
    if(sessionId) await saveSession();
    sessionId=null; $('session-name').value='새 대화'; characterProfiles=[]; renderCharacters(); $('background').value='';
    const created=await saveSession(); await loadSession(created.id); $('prompt').value=''; $('prompt').dispatchEvent(new Event('input'));
  } catch(exc){error(exc.message);}
});
async function initialize() {
  try {
    const sessions=await listSessions();
    const previous=localStorage.getItem('anima-session');
    if (sessions.length) await loadSession(sessions.some(s=>s.id===previous)?previous:sessions[0].id);
    else { $('session-name').value='첫 대화'; const created=await saveSession(); await loadSession(created.id); }
    await refresh();
  } catch(exc){error(exc.message); $('generate').disabled=true;}
}
initialize();

function renderCharacters() {
  $('character-profiles').replaceChildren();
  const ordinals=['첫 번째','두 번째','세 번째','네 번째','다섯 번째','여섯 번째','일곱 번째','여덟 번째','아홉 번째','열 번째','열한 번째','열두 번째'];
  characterProfiles.forEach((profile,index)=>{
    const card=document.createElement('fieldset'); card.className='character-card';
    const legend=document.createElement('legend'); legend.textContent=`캐릭터 ${index+1}`; card.append(legend);
    const grid=document.createElement('div'); grid.className='character-grid';
    const field=(key,title,kind='input',placeholder='')=>{
      const wrap=document.createElement('div'); wrap.className=`character-${key}`;
      const label=document.createElement('label'); label.htmlFor=`character-${index}-${key}`; label.textContent=title;
      const control=document.createElement(kind); control.id=label.htmlFor;
      if(kind==='select') { control.add(new Option('이름 사용하지 않기','false')); control.add(new Option('이름 그대로 사용','true')); control.value=String(profile.use_name); }
      else { if(kind==='input') control.type='text'; control.value=profile[key] || ''; control.placeholder=placeholder; control.maxLength=key==='description'?6000:key==='label'?100:200; }
      control.addEventListener(kind==='select'?'change':'input',()=>{
        profile[key]=key==='use_name'?control.value==='true':control.value;
        if(key==='use_name') updateMode();
      });
      wrap.append(label,control); grid.append(wrap); return wrap;
    };
    field('label','구분용 이름 (선택)','input','예: 유나 — 출력하지 않는 관리용 이름');
    field('use_name','이름 사용 방식','select');
    const nameField=field('name','출력할 이름·태그·LoRA 트리거','input','예: hatsune_miku 또는 MyCharacter_v1');
    const designationField=field('designation','이름 대신 사용할 지칭 (선택)','input',`미입력: ${ordinals[index]} 캐릭터 · 예: ${ordinals[index]} 여성, 왼쪽의 사람`);
    field('description','이 캐릭터의 특징','textarea','외형, 의상, 성별 등 이 인물의 설정만 입력하세요.');
    function updateMode(){ nameField.hidden=!profile.use_name; designationField.hidden=profile.use_name; }
    updateMode(); card.append(grid);
    const hint=document.createElement('p'); hint.className='muted'; hint.textContent='대체 지칭은 한국어로 입력하세요. LLM이 영문 묘사에 맞게 해석합니다. 순번만으로 좌우 위치를 정하지 않습니다.';
    const remove=document.createElement('button'); remove.type='button'; remove.className='remove-character'; remove.textContent=`캐릭터 ${index+1} 제거`;
    remove.addEventListener('click',()=>{characterProfiles.splice(index,1); renderCharacters();});
    card.append(hint,remove); $('character-profiles').append(card);
  });
  if(!characterProfiles.length) $('character-profiles').textContent='캐릭터를 추가하면 인물별 설정과 이름 사용 방식을 저장할 수 있습니다.';
  $('add-character').disabled=busy || characterProfiles.length>=12;
}
$('add-character').addEventListener('click',()=>{
  if(characterProfiles.length>=12 || busy) return;
  characterProfiles.push({id:crypto.randomUUID(),label:'',name:'',use_name:false,designation:'',description:''}); renderCharacters();
});
