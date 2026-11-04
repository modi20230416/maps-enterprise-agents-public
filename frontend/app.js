const API = 'http://127.0.0.1:8000';
const $ = id => document.getElementById(id);
const labels = {QUEUED:'排队中',RUNNING:'运行中',WAITING_REVIEW:'待人工复核',READY:'复核通过',REJECTED:'已驳回',FAILED:'执行失败',CANCELLED:'已取消',PENDING:'待复核',APPROVED:'已确认',NOT_REQUESTED:'本次未请求'};
let selected = null, current = null, eventList = [], replaying = false, loading = false, requestGeneration = 0;
const node = (tag, text, cls) => { const el = document.createElement(tag); if(text !== undefined) el.textContent = text; if(cls) el.className = cls; return el; };
function message(text) { $('message').textContent = text; $('message').hidden = !text; }
async function api(path, options={}) {
  const response = await fetch(API + path, options);
  const value = await response.json();
  if(!response.ok) throw new Error(value.detail || `请求失败 (${response.status})`);
  return value;
}
const post = (path, body, headers={}) => api(path,{method:'POST',headers:{'Content-Type':'application/json',...headers},body:JSON.stringify(body || {})});
const stamp = ts => new Date(ts*1000).toLocaleString('zh-CN',{hour12:false});
async function init() {
  try {
    const health = await api('/api/health');
    $('connection').textContent = `API 已连接 · ${health.worker_count} 个任务槽`;
    if(health.protocol_fixture_allowed && !$('mode').querySelector('[value="protocol_fixture"]')) {
      const option=node('option','协议夹具 · 离线链路验证'); option.value='protocol_fixture'; $('mode').append(option);
    }
    $('mode-note').textContent = health.llm_configured ? '后端模型配置已齐；提交会调用服务，连通性仍需实际验证。' : '后端尚未配置密钥；真实 LLM 暂不可提交。可显式选协议夹具验证链路。';
    await refresh();
  } catch(error) { $('connection').textContent = 'API 未连接'; message(`${error.message}。请先启动 API 与前端两个服务。`); }
}
async function refresh() {
  if(loading) return;
  loading = true;
  try {
    const tasks = await api('/api/tasks');
    $('count').textContent = tasks.length;
    $('tasks').replaceChildren();
    for(const task of tasks) {
      const button = node('button',undefined,`task${task.id === selected?' selected':''}`);
      button.append(node('span',labels[task.status] || task.status,'task-state'),node('b',task.id.slice(0,8)),node('small',`${task.mode === 'llm'?'LLM':'协议夹具'} · ${stamp(task.created_at)}`));
      button.onclick = () => select(task.id);
      $('tasks').append(button);
    }
    if(selected && !replaying) await loadDetail();
  } catch(error) { message(error.message); }
  finally { loading = false; }
}
async function select(id) {
  selected = id; replaying = false; requestGeneration++;
  $('projection').hidden = true; message('');
  await loadDetail();
  await refresh();
}
async function loadDetail() {
  const id = selected, generation = requestGeneration;
  const [task, events] = await Promise.all([api(`/api/tasks/${id}`),api(`/api/tasks/${id}/events?limit=1000`)]);
  if(id !== selected || generation !== requestGeneration || replaying) return;
  current = task; eventList = events;
  render(task);
  renderEvents(events);
}
function render(task, artifactsOverride=null, reviewsOverride=null) {
  $('empty').hidden = true; $('detail').hidden = false;
  $('task-title').textContent = `监控任务 ${task.id.slice(0,8)}`;
  $('task-meta').textContent = `${stamp(task.created_at)} · 尝试 ${task.attempt}/${task.max_attempts} · ${task.kind}${task.error ? ' · '+task.error:''}`;
  $('status').textContent = labels[task.status] || task.status;
  const fixture = task.mode === 'protocol_fixture';
  $('source-note').className = `notice${fixture?' fixture':''}`;
  $('source-note').textContent = fixture ? '协议夹具模式：验证协议解析、模块初始化、复核和持久化链路。没有调用真实 LLM，也不代表模型预测效果。' : '真实 LLM 模式：Prediction 根据当前状态和历史观测推断未来扰动。模型输出是风险假设，尚未构成经校准的预测概率。';
  const artifacts = artifactsOverride || Object.fromEntries(task.artifacts.filter(a=>a.attempt===task.attempt).map(a=>[a.name,a.payload]));
  const prediction = artifacts.prediction || artifacts.report?.observation;
  const preparation = artifacts.preparation || artifacts.report?.observation?.preparation || {};
  const observations = artifacts.observation?.event?.payload?.runtime_observations || task.input.event.payload?.runtime_observations || [];
  $('observations').replaceChildren();
  for(const obs of observations) {
    const card = node('div',undefined,'obs');
    card.append(node('small',`${obs.id} · t=${obs.at} min`),node('b',`${Math.round(obs.progress*100)}%`),node('small',`${obs.machine_id} / ${obs.order_id}`));
    $('observations').append(card);
  }
  if(!observations.length) $('observations').append(node('p','观测记录见输入工单。','hint'));
  $('risk-count').textContent = prediction ? prediction.risks.length : '—';
  $('module-count').textContent = Object.keys(preparation.module_initialization || {}).length;
  $('mes-state').textContent = '未写回';
  $('risks').replaceChildren();
  if(!prediction) $('risks').append(node('p','等待 Prediction 返回结构化结果。','hint'));
  else if(!prediction.risks.length) $('risks').append(node('p',prediction.prediction_error || prediction.prediction_call?.abstention_reason || `当前结果：${prediction.status}，没有未来风险假设。`,'hint'));
  for(const risk of prediction?.risks || []) {
    const card = node('div',undefined,'risk');
    card.append(node('h3',`${risk.kind} · ${risk.machine_id}`),node('p',risk.rationale),node('p',`预计影响窗口：${risk.earliest_impact_minute}–${risk.latest_impact_minute} min · 不确定性：${risk.uncertainty}`),node('p',`证据 ${risk.evidence_ids.join(' / ')} · 工单 ${risk.affected_order_ids.join(' / ')}`,'evidence'));
    for(const request of risk.module_requests) card.append(node('p',`→ ${request.module}：${request.purpose}`));
    $('risks').append(card);
  }
  $('modules').replaceChildren();
  for(const module of ['orchestrate','planning','scheduling','warehouse']) {
    const review = reviewsOverride ? reviewsOverride[module] : task.reviews.find(r=>r.module===module);
    const state = preparation.module_initialization?.[module];
    const card = node('div',undefined,'module');
    card.append(node('h3',module),node('span',labels[review?.status || review?.decision] || (state?'上下文已准备':'等待结果'),'review-state'),node('p',module==='orchestrate'?'复核风险假设与模块调用范围':state ? `${state.content} · ${state.status}`:'本轮尚无初始化上下文'));
    if(preparation.dependency_added_modules?.includes(module)) card.append(node('small','由 Scheduling 依赖引入','hint'));
    if(review?.reviewer) card.append(node('p',`${review.reviewer} · ${review.comment || '无附加意见'}`));
    if(!replaying && review?.status==='PENDING' && task.status==='WAITING_REVIEW') {
      const buttons = node('div',undefined,'buttons');
      for(const [decision,label] of [['APPROVED','确认'],['REJECTED','驳回']]) {
        const button = node('button',label,decision==='REJECTED'?'secondary':'');
        button.onclick = async () => {
          const reviewer = $('reviewer').value.trim();
          if(!reviewer) { message('请填写本地复核人姓名。'); $('reviewer').focus(); return; }
          button.disabled = true;
          try { await post(`/api/tasks/${task.id}/reviews/${module}`,{binding:review.binding,decision,reviewer,comment:$('comment').value}); message(''); await loadDetail(); }
          catch(error) { message(error.message); button.disabled = false; }
        };
        buttons.append(button);
      }
      card.append(buttons);
    }
    $('modules').append(card);
  }
  $('cancel').disabled = replaying || !['QUEUED','RUNNING','WAITING_REVIEW'].includes(task.status);
  $('rerun').disabled = replaying;
}
function renderEvents(events) {
  $('event-count').textContent = `${events.length} 条 · 持久化游标`;
  const prior = $('replay-cursor').value;
  $('replay-cursor').replaceChildren(node('option','最新状态')); $('replay-cursor').firstChild.value='';
  $('timeline').replaceChildren();
  for(const event of events) {
    const option = node('option',`#${event.cursor} · ${event.type}`); option.value=event.cursor; $('replay-cursor').append(option);
    const li=node('li',`#${event.cursor} ${event.type}`);
    li.append(node('small',`${event.producer} → ${event.recipient} · 尝试 ${event.attempt} · ${stamp(event.at)}`));
    $('timeline').append(li);
  }
  if([...$('replay-cursor').options].some(o=>o.value===prior)) $('replay-cursor').value=prior;
}
$('create').onsubmit = async event => {
  event.preventDefault(); $('submit').disabled=true;
  try { const result=await post('/api/tasks',{mode:$('mode').value},{'Idempotency-Key':crypto.randomUUID()}); await select(result.task_id); }
  catch(error) { message(error.message); }
  finally { $('submit').disabled=false; }
};
$('refresh').onclick = refresh;
$('cancel').onclick = async () => { try { await post(`/api/tasks/${selected}/cancel`); await loadDetail(); } catch(error) {message(error.message);} };
$('rerun').onclick = async () => { try { const task=await post(`/api/tasks/${selected}/rerun`); await select(task.task_id); } catch(error) {message(error.message);} };
$('replay').onclick = async () => {
  if(!selected) return;
  try {
    const id=selected, cursor=$('replay-cursor').value;
    const state=await api(`/api/tasks/${id}/replay${cursor?'?upto='+cursor:''}`);
    if(id!==selected) return;
    replaying=true; requestGeneration++;
    const task={...current,status:state.status || '尚未入队',attempt:state.attempt};
    render(task,state.artifacts,state.reviews);
    $('projection').hidden=false; $('projection').textContent=JSON.stringify({cursor:state.cursor,status:state.status,attempt:state.attempt,reviews:state.reviews,execution_authorized:false},null,2);
    message(`历史回放：事件 #${state.cursor}。只读；返回实时后可继续复核。`);
  } catch(error) {message(error.message);}
};
$('live').onclick = async () => {replaying=false; requestGeneration++; $('projection').hidden=true; message(''); await loadDetail();};
await init();
setInterval(refresh,1800);
