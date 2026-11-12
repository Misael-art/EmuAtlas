// Embedded into the generated static HTML; requests only the local Preview bridge.
function commandText(action) {
  if(action.workflow?.kind==='media_replan')return [
    'romtool media optimize inventory (reutiliza evidência oficial somente se tamanho, data, dispositivo e inode dos selecionados ainda coincidirem; caso contrário, gera novo inventário)',
    'romtool media optimize plan --inventory-operation-id <inventário oficial validado ou recém-gerado>',
    'Perfis: png-lossless-repack, jpeg-lossless-repack, pdf-qpdf-lossless',
    ...action.workflow.relative_paths.map(path=>'--relative-path '+JSON.stringify(path)),
    'O plano será exibido para revisão. Esta ação não converte arquivos.'
  ].join('\n');
  return action.commands.map(command=>command.map(value=>JSON.stringify(value)).join(' ')).join('\n');
}

function showResult(id) {
  const r=(DATA.results||[]).find(row=>row.id===id);
  if(!r)return;
  pendingExecution=[];
  $('review-body').innerHTML=`<div class="review-item"><h2>${esc(r.title)}</h2>
    <p>Operação ${esc(r.operation_id)} · ${esc(r.completed_at)}</p>
    <div class="path-label">Origem registrada</div><div class="path-box mono">${esc(r.source)}</div>
    <div class="path-label">Arquivo final</div><div class="path-box mono">${esc(r.destination)}</div>
    <p>${bytes(r.source_size_bytes)} → ${bytes(r.output_size_bytes)}. Variação de ${r.stored_size_delta_bytes>0?'+':r.stored_size_delta_bytes<0?'−':''}${bytes(Math.abs(r.stored_size_delta_bytes))} no arquivo.</p>
    <p>${r.action==='pack_tree'?'Formato nativo de leitura direta; uma conversão não garante arquivo menor que o contêiner de origem.':r.action==='media_plan_audit'?'Esta análise preservou o arquivo sem conversão; consulte o diagnóstico abaixo.':'O resultado segue o perfil de validação da operação oficial.'}</p>
    <div class="path-label">Original preservado</div><div class="path-box mono">${esc(r.retained_at||'Consultar operação oficial')}</div>
    <p>Originais e quarentena continuam ocupando espaço. Não foi medida liberação física por este relatório.</p>
    <div class="code">SHA-256 final: ${esc(r.sha256)}\nValidação: ${esc(r.proof)}</div></div>`;
  $('confirm-execute').disabled=true;
  $('confirm-execute').textContent='Resultado já registrado';
  if(!$('review-dialog').open)$('review-dialog').showModal();
}

async function apiPost(path,body) {
  const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Preview-Token':token},
    body:JSON.stringify({...body,snapshot_revision:path==='/api/jobs'?(reviewRevision||DATA.revision):DATA.revision}),cache:'no-store'});
  const d=await response.json();
  if(!response.ok)throw Error(d.error||'Falha na sessão local');
  return d;
}

function syncSnapshot(force=false) {
  if(!live)return Promise.resolve(DATA);
  const task=async()=>{
    const response=await fetch('/api/state',{cache:'no-store'});
    if(!response.ok)throw Error('Não foi possível consultar o estado do Preview');
    const state=await response.json();
    if(force||state.revision!==DATA.revision) {
      const snapshotResponse=await fetch('/api/snapshot',{cache:'no-store'});
      if(!snapshotResponse.ok)throw Error('O relatório atual não está disponível');
      const snapshot=await snapshotResponse.json();
      if(!snapshot.revision||!Array.isArray(snapshot.actions)||!snapshot.metrics)throw Error('Resposta de relatório inválida');
      DATA=snapshot;
      const ready=new Set(DATA.actions.filter(a=>a.ready).map(a=>a.id));
      selected=new Set([...selected].filter(id=>ready.has(id)));
      if(focusAction&&!DATA.actions.some(a=>a.id===focusAction))focusAction=null;
      render();
    }
    if(state.active_job&&!jobRunning&&state.active_job!==jobId) {
      jobId=state.active_job;
      jobRunning=true;
      $('job-id').textContent=jobId;
      $('job-button').hidden=false;
      render();
      pollJob();
    }
    return DATA;
  };
  syncChain=syncChain.catch(()=>{}).then(task);
  return syncChain;
}

async function startJob(ids,refresh=false) {
  try {
    const d=await apiPost(refresh?'/api/refresh':'/api/jobs',refresh?{}:{ids});
    jobId=d.operation_id;
    jobRunning=true;
    reconnects=0;
    sessionStorage.setItem('preview-job:'+location.origin,jobId);
    $('job-button').hidden=false;
    $('job-id').textContent=jobId;
    $('job-status').innerHTML=badge(false,'Aguardando execução e atualização');
    $('job-log').textContent='Aguardando a ferramenta oficial…';
    $('review-dialog').close();
    if(!$('job-dialog').open)$('job-dialog').showModal();
    render();
    pollJob();
  } catch(e) {toast(e.message);}
}

async function pollJob() {
  clearTimeout(pollTimer);
  if(!jobId)return;
  try {
    const response=await fetch('/api/jobs/'+encodeURIComponent(jobId),{cache:'no-store'});
    const job=await response.json();
    if(!response.ok)throw Error(job.error||'Operação indisponível');
    reconnects=0;
    const working=['running','queued','updating_report','finalizing'].includes(job.status);
    const problems=job.error||job.refresh_error||job.history_error;
    const success=!problems&&['completed_verified','completed_commands'].includes(job.status);
    const label={running:'Em execução',queued:'Na fila',updating_report:'Atualizando estado e resultados',finalizing:'Registrando resultado'}[job.status]
      ||(success?'Concluído e reconciliado':problems||'Interrompido para revisão');
    $('job-status').innerHTML=badge(success,label);
    $('job-log').textContent=job.log||(working?'Atualizando o relatório…':success?'Relatório atualizado; os resultados estão disponíveis na página.':problems||'Consulte o registro da operação para revisar o resultado.');
    jobRunning=working;
    if(working) {pollTimer=setTimeout(pollJob,1200);return;}
    await syncSnapshot(true);
    render();
    if(!problems)toast('Estado atualizado às '+new Date(DATA.generated_at).toLocaleTimeString('pt-BR')+'. Consulte Resultados para ver o arquivo final e o rollback.');
    else toast('A execução deixou uma pendência: '+problems);
  } catch(e) {
    $('job-status').textContent='Reconectando ao Preview; a operação continua sob verificação.';
    toast(e.message);
    if(++reconnects<=10)pollTimer=setTimeout(pollJob,2000);
  }
}

if(live) {
  syncSnapshot().catch(e=>toast(e.message));
  document.addEventListener('visibilitychange',()=>{
    if(document.visibilityState==='visible'&&!jobRunning)syncSnapshot().catch(e=>toast(e.message));
  });
}

// Jogar: platform → game list with launch and every canonical media type in series.
let gamesSystem=null,gamesData=null,gamesQuery='',gamesPage=0,gamesError='',launchTimer=null,launchState=null;
const fullscreenKey='preview-fullscreen';
function fullscreenPref(){try{return localStorage.getItem(fullscreenKey)==='1'}catch(e){return false}}
function setFullscreenPref(v){try{localStorage.setItem(fullscreenKey,v?'1':'0')}catch(e){}}

function renderGames() {
  $('title').textContent=gamesSystem?(DATA.platforms.find(p=>p.id===gamesSystem)?.name||gamesSystem):'Escolha uma plataforma.';
  $('subtitle').textContent=gamesSystem?'Jogos em roms/'+gamesSystem+' com as mídias associadas pelo nome exato.':'Entre numa plataforma para ver os jogos, as mídias e iniciar o emulador do perfil.';
  $('crumb').textContent=gamesSystem?'Jogar / '+gamesSystem:'Jogar';
  const view=$('games-view');
  if(!live){view.innerHTML=`<div class="notice">${ico('info')}<span>A lista de jogos e o botão Play precisam da sessão local. Abra <strong>Preview.desktop</strong> ou <strong>preview.sh</strong>.</span></div>`;return;}
  if(!gamesSystem){
    const rows=DATA.platforms.filter(p=>p.roms?.files&&/^[a-z0-9][a-z0-9_-]{0,63}$/.test(p.id)).sort((a,b)=>a.name.localeCompare(b.name,'pt-BR'));
    view.innerHTML=`<div class="play-grid">${rows.map(p=>`<button class="play-platform" data-play-system="${esc(p.id)}">${p.artwork.logo?`<img loading="lazy" src="${esc(imageURL(p.artwork.logo))}" alt="">`:ico('gamepad')}<strong>${esc(p.name)}</strong><span class="muted" style="font-size:11px">${num(p.roms.files)} arquivos · ${esc(p.id)}</span></button>`).join('')}</div>`;
    view.querySelectorAll('[data-play-system]').forEach(b=>b.onclick=()=>openPlatform(b.dataset.playSystem));
    icons();return;
  }
  const head=`<div class="game-head"><button class="small ghost" id="games-back">← Plataformas</button><label class="searchbox"><span data-icon="search"></span><input id="games-search" type="search" placeholder="Buscar jogo…" value="${esc(gamesQuery)}" aria-label="Buscar jogo"></label><label class="fs-toggle"><input type="checkbox" class="check" id="games-fullscreen" ${fullscreenPref()?'checked':''}>Tela cheia</label></div>`;
  let body;
  if(gamesError)body=`<div class="notice">${ico('info')}<span>${esc(gamesError)}</span></div>`;
  else if(!gamesData)body=`<p class="muted">Carregando jogos…</p>`;
  else{
    const rows=gamesData.games.filter(g=>!gamesQuery||(g.title+' '+g.folder).toLocaleLowerCase('pt-BR').includes(gamesQuery)),size=20;
    gamesPage=Math.max(0,Math.min(gamesPage,Math.ceil(rows.length/size)-1));
    const shown=rows.slice(gamesPage*size,gamesPage*size+size),blocked=gamesData.launch_blocked;
    body=(blocked?`<div class="notice">${ico('info')}<span>Play indisponível: ${esc(blocked)}</span></div>`:'')
      +(rows.length?shown.map(g=>gameRow(g,blocked)).join(''):`<div class="empty">${ico('check')}<h3>Nenhum jogo nesta seleção</h3></div>`)
      +`<div class="pagination"><span>${rows.length?num(gamesPage*size+1)+'–'+num(Math.min((gamesPage+1)*size,rows.length)):'0'} de ${num(rows.length)}</span><span><button class="small" id="games-prev" ${gamesPage?'':'disabled'}>Anterior</button> <button class="small" id="games-next" ${(gamesPage+1)*size<rows.length?'':'disabled'}>Próxima</button></span></div>`;
  }
  view.innerHTML=head+body+`<div id="launch-bar"></div>`;
  $('games-back').onclick=()=>{gamesSystem=null;gamesData=null;gamesError='';render()};
  $('games-fullscreen').onchange=e=>setFullscreenPref(e.target.checked);
  const search=$('games-search');search.oninput=e=>{gamesQuery=e.target.value.toLocaleLowerCase('pt-BR');gamesPage=0;renderGames();const el=$('games-search');el.focus();el.setSelectionRange(el.value.length,el.value.length)};
  if($('games-prev'))$('games-prev').onclick=()=>{gamesPage--;renderGames();view.scrollIntoView()};
  if($('games-next'))$('games-next').onclick=()=>{gamesPage++;renderGames();view.scrollIntoView()};
  view.querySelectorAll('[data-launch]').forEach(b=>b.onclick=()=>launchGame(b.dataset.launch));
  renderLaunchBar();icons();
}

function gameRow(g,blocked) {
  const cards=[];
  g.media.forEach((m,i)=>{
    if(!m.files.length){cards.push(`<div class="media-card${i?'':' first'}"><div class="frame missing">${ico('image')}<span>Faltante</span><strong>${esc(m.type)}</strong></div><div class="label">${esc(m.type)}</div></div>`);return;}
    m.files.forEach((f,j)=>{
      const name=f.path.split('/').pop(),label=m.type+(m.files.length>1?' · '+(j+1):'');
      const inner=f.kind==='image'?`<img loading="lazy" src="${esc(imageURL(f.path))}" alt="${esc(label)}">`:f.kind==='video'?`<video src="${esc(imageURL(f.path))}" preload="none" controls muted></video>`:`<div class="missing" style="border:0">${ico('info')}<span>${esc(name)}</span><span>formato não visualizável</span></div>`;
      cards.push(`<div class="media-card${i||j?'':' first'}"><div class="frame">${inner}</div><div class="label" title="${esc(f.path)}">${esc(label)}</div></div>`);
    });
  });
  const disabled=blocked||!canRun||jobRunning||launchState?.status==='running';
  return `<article class="game-row"><div class="game-top"><button class="primary small" data-launch="${esc(g.path)}" ${disabled?'disabled':''} aria-label="Jogar ${esc(g.title)}">${ico('play')}Play</button><h3>${esc(g.title)}</h3><span class="muted mono" style="font-size:11px">${esc(g.path.split('/').pop())}${g.folder&&g.folder!=='.'?' · '+esc(g.folder):''}${g.size!=null?' · '+bytes(g.size):''}</span>${g.same_basename>1?`<span class="badge warn" title="Mais de um arquivo usa este nome base; as mídias são as mesmas para todos">◌ ${num(g.same_basename)} arquivos com o mesmo nome</span>`:''}</div>${g.media.length?`<div class="media-strip">${cards.join('')}</div>`:'<p class="muted" style="font-size:12px">Esta plataforma ainda não tem categorias de mídia em media/'+esc(gamesSystem)+'.</p>'}</article>`;
}

async function openPlatform(id) {
  gamesSystem=id;gamesData=null;gamesError='';gamesQuery='';gamesPage=0;render();
  try{const r=await fetch('/api/platforms/'+encodeURIComponent(id)+'/games',{cache:'no-store'});const d=await r.json();if(!r.ok)throw Error(d.error||'Falha ao listar jogos');if(gamesSystem===id)gamesData=d;}
  catch(e){gamesError=e.message}
  if(gamesSystem===id&&tab==='jogar')renderGames();
}

async function launchGame(path) {
  try{
    const d=await apiPost('/api/launch',{system_id:gamesSystem,path,fullscreen:$('games-fullscreen').checked});
    launchState={status:'running',game:path,launch_id:d.launch_id};toast('Iniciando o emulador…');renderGames();pollLaunch();
  }catch(e){toast(e.message)}
}

async function pollLaunch() {
  clearTimeout(launchTimer);
  try{const r=await fetch('/api/launch',{cache:'no-store'});launchState=await r.json();}catch(e){return}
  if(tab==='jogar')renderGames();
  if(launchState.status==='running')launchTimer=setTimeout(pollLaunch,2000);
}

function renderLaunchBar() {
  const bar=$('launch-bar');if(!bar)return;const s=launchState;
  if(!s||s.status==='idle'){bar.innerHTML='';return;}
  const ok=s.status==='running'||s.exit_code===0,name=(s.game||'').split('/').pop();
  bar.innerHTML=`<div class="notice">${ico(s.status==='running'?'play':ok?'check':'info')}<div><strong>${s.status==='running'?'Em execução: ':ok?'Encerrado: ':'Falhou (código '+esc(s.exit_code)+'): '}${esc(name)}</strong> <span class="muted">· ${s.display==='fullscreen'?'tela cheia':'janela'}</span>${!ok&&s.log?`<pre class="joblog" style="max-height:140px;margin-top:8px">${esc(s.log.slice(-1500))}</pre>`:''}</div></div>`;
}

if(live)fetch('/api/launch',{cache:'no-store'}).then(r=>r.json()).then(s=>{launchState=s;if(s.status==='running')pollLaunch()}).catch(()=>{});
{const play=new URLSearchParams((location.hash||'').slice(1)).get('play');if(live&&play&&/^[a-z0-9][a-z0-9_-]{0,63}$/.test(play)){setTab('jogar');openPlatform(play);}}
