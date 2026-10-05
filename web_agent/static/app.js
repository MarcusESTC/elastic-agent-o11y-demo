'use strict';
const $ = id => document.getElementById(id);
const state = {config:null,scenario:'normal',session:null,busy:false,runs:[],active:null};
function el(tag,cls,text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;}
function safeLink(url,label,cls){const a=el('a',cls,label);try{const u=new URL(url);if(u.protocol==='https:'||u.protocol==='http:'){a.href=u.href;a.target='_blank';a.rel='noopener noreferrer';}}catch{}return a;}
function toast(message){$('toast').textContent=message;$('toast').style.display='block';setTimeout(()=>$('toast').style.display='none',5000);}
function time(ms){return ms<1000?`${ms} ms`:`${(ms/1000).toFixed(2)} s`;}
function money(value){return value==null?'Unavailable':`$${value.toFixed(6)}`;}
function scroll(){const c=$('conversation');c.scrollTop=c.scrollHeight;}
function choose(id){if(state.busy)return;state.scenario=id;state.session=null;const scenario=state.config.scenarios.find(s=>s.id===id);$('scenario-title').textContent=scenario.title;$('scenario-description').textContent=scenario.description;$('scenario-select').value=id;$('prompt').value=scenario.prompt;$('send').replaceChildren(document.createTextNode(id==='compare'?'Compare models ':'Run scenario '),el('span','', '↗'));document.querySelectorAll('.scenario').forEach(n=>n.classList.toggle('selected',n.dataset.id===id));$('model').disabled=id==='compare';}
function setBusy(value){state.busy=value;$('send').disabled=value;$('reset').disabled=value;$('scenario-select').disabled=value;$('model').disabled=value||state.scenario==='compare';$('prompt').disabled=value;document.querySelectorAll('.scenario').forEach(b=>b.disabled=value);if(value)$('composer-hint').textContent='Following model calls and tools live…';else $('composer-hint').textContent='Enter to run · Shift + Enter for a new line';}
function clear(){if(state.busy)return;state.session=null;state.runs=[];state.active=null;$('conversation').replaceChildren();$('timeline').replaceChildren(el('div','empty-timeline','Run a scenario to see its execution.'));$('run-tabs').replaceChildren();$('trace-actions').replaceChildren();$('run-state').textContent='Ready';$('run-state').className='status-badge';}
function renderTabs(){const container=$('run-tabs');container.replaceChildren();(state.runs.length>1?state.runs.slice(-6):[]).forEach((run,i)=>{const b=el('button',run===state.active?'selected':'',`${run.label} · ${i+1}`);b.onclick=()=>{state.active=run;renderTimeline();};container.append(b);});}
function renderTimeline(){const run=state.active;if(!run)return;renderTabs();$('run-state').textContent=run.result?(run.result.is_error?'Failed':'Complete'):'Running';$('run-state').className='status-badge '+(run.result?(run.result.is_error?'error':'complete'):'running');const timeline=$('timeline');timeline.replaceChildren();const total=run.result?.latency_ms||Math.max(1,...run.steps.map(s=>s.started_ms+(s.duration_ms||0)));run.steps.forEach(step=>{const item=el('div',`timeline-step ${step.kind} ${step.status}`);item.append(el('span','step-dot'));const line=el('div','step-line');line.append(el('span','',step.label.replace('search_knowledge_base','Search documents')),el('span','step-time',step.status==='running'?'Working…':time(step.duration_ms)));item.append(line);if(step.kind==='tool'){item.append(el('div','step-note',step.status==='error'?step.detail:(step.result?.backend|| (step.result?.result!==undefined?`Result: ${step.result.result}`:step.injected?'Demo fault injection enabled':'Tool execution'))));const details=el('details');details.append(el('summary','','Arguments & result'),el('pre','',JSON.stringify({arguments:step.arguments,result:step.result},null,2)));item.append(details);}else item.append(el('div','step-note',step.detail||step.model||'Checking configured patterns'));if(step.duration_ms){const bar=el('div','step-bar'),fill=el('span');fill.style.width=Math.max(2,100*step.duration_ms/total)+'%';bar.append(fill);item.append(bar);}timeline.append(item);});$('trace-actions').replaceChildren();const links=run.result||run;if(links.trace_url)$('trace-actions').append(safeLink(links.trace_url,'Trace waterfall ↗'),safeLink(links.logs_url,'Correlated logs ↗'));}
function makeRun(label){$('welcome')?.remove();const article=el('article','message');article.append(el('div','message-label','You'));const user=el('div','user-message','Preparing your request…');article.append(user);const answerLabel=el('div','message-label','Assistant');answerLabel.style.marginTop='22px';answerLabel.append(el('span','',label));const answer=el('div','assistant-message waiting','Starting the run…');article.append(answerLabel,answer);$('conversation').append(article);const run={label,steps:[],article,user,answer};state.runs.push(run);state.active=run;renderTimeline();scroll();return run;}
function showResult(run,r){
  run.result=r;run.answer.textContent=r.is_error?r.error:r.text;run.answer.className='assistant-message'+(r.is_error?' error':'');
  if(!r.trace_id){run.user.textContent='Request stopped';if(state.active===run)renderTimeline();scroll();return;}
  const proof=el('details','elastic-proof');proof.append(el('summary','','Elastic copy · redacted during ingestion'));
  const proofText=el('div');proofText.append(el('p','','The conversation above is original. Read the stored trace to see what Elasticsearch masked.'));
  const loadProof=el('button','architecture-button','View Elastic copy');
  loadProof.onclick=async()=>{
    loadProof.disabled=true;loadProof.textContent='Reading stored trace…';
    try{
      const response=await fetch(`/api/runs/${encodeURIComponent(r.run_id)}/stored?session_id=${encodeURIComponent(r.session_id)}`);
      const data=await response.json();if(!response.ok)throw Error(data.error||'Unable to read the Elastic copy.');
      if(data.status==='pending'){proofText.replaceChildren(el('p','','The trace is still being indexed. Retry in a few seconds.'));}
      else if(data.status!=='stored'){proofText.replaceChildren(el('p','','Ingest redaction could not be verified for this trace. Check the pipeline.'));}
      else{
        proofText.replaceChildren(el('p','',data.action==='obfuscated'?`Verified in Elastic · ${data.redacted_fields} content ${data.redacted_fields===1?'field':'fields'} changed`:'Verified in Elastic · no matching patterns'));
        proofText.append(el('strong','','Stored question'),el('pre','',data.input),el('strong','','Stored answer'),el('pre','',data.output),safeLink(data.url,'Open stored trace in Elastic ↗'));
      }
    }catch(error){proofText.replaceChildren(el('p','',error.message));}
    finally{loadProof.disabled=false;loadProof.textContent='Refresh Elastic copy';}
  };
  proof.append(proofText,loadProof);run.article.append(proof);
  if(r.sources?.length){const sources=el('div','sources');r.sources.forEach(s=>sources.append(safeLink(s.url,`[${s.source_id}] ${s.title} ↗`,'source')));run.article.append(sources);}
  const footer=el('div','result-footer'),links=el('div','result-links');links.append(safeLink(r.trace_url,'View trace ↗'),safeLink(r.logs_url,'View logs ↗'));footer.append(links);run.article.append(footer);
  const details=el('details','technical');details.append(el('summary','',`Run details · ${time(r.latency_ms)}`));
  const stats=el('div','run-summary');[[time(r.latency_ms),'End-to-end'],[(r.in_tokens+r.out_tokens+r.thinking_tokens).toLocaleString(),'Total tokens'],[money(r.cost_usd),'Est. API cost']].forEach(([value,label])=>{const stat=el('div','stat');stat.append(el('strong','',value),el('small','',label));stats.append(stat);});details.append(stats);
  const dl=el('dl');[['Model',r.model_used],['Trace ID',r.trace_id],['Redaction','At ingestion · '+r.privacy_pipeline],['Input / answer / thinking tokens',`${r.in_tokens} / ${r.out_tokens} / ${r.thinking_tokens}`],['First provider response',r.first_response_ms==null?'Unavailable':time(r.first_response_ms)],['Cost estimate','Standard text API rates, including thinking. Excludes free-tier allowances, caching and tool charges.']].forEach(([k,v])=>dl.append(el('dt','',k),el('dd','',v)));details.append(dl,safeLink(state.config.pricing_source,'Pricing reference ↗'));
  const feedback=el('div','feedback','Helpful? ');[1,-1].forEach(rating=>{const button=el('button','',rating===1?'Yes':'No');button.onclick=async()=>{feedback.querySelectorAll('button').forEach(b=>b.disabled=true);try{const response=await fetch('/api/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({run_id:r.run_id,session_id:r.session_id,rating})});if(!response.ok)throw Error('Feedback could not be saved.');const data=await response.json();feedback.querySelectorAll('button').forEach((b,i)=>b.classList.toggle('chosen',(i===0?1:-1)===data.rating));toast('Feedback saved with this trace.');}catch(error){toast(error.message);feedback.querySelectorAll('button').forEach(b=>b.disabled=false);}};feedback.append(button);});details.append(feedback);run.article.append(details);
  if(state.active===run)renderTimeline();scroll();health();
}
async function execute(message,model,session,scenario,label){const run=makeRun(label);let completed=false;function receive(event){if(event.type==='start'){Object.assign(run,{trace_url:event.trace_url,logs_url:event.logs_url,session_id:event.session_id});run.user.textContent=event.message;run.answer.textContent='Working… inspect the execution timeline.';if(state.active===run)renderTimeline();}else if(event.type==='step'){const index=run.steps.findIndex(s=>s.id===event.step.id);if(index<0)run.steps.push(event.step);else run.steps[index]=event.step;if(state.active===run)renderTimeline();}else if(event.type==='answer'){run.answer.textContent=event.text;run.answer.className='assistant-message';scroll();}else if(event.type==='notice'){run.answer.textContent=event.text;}else if(event.type==='complete'){completed=true;showResult(run,event.result);}}
try{const response=await fetch('/api/chat/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message,model,session_id:session,scenario})});if(!response.ok){const error=await response.json();throw Error(error.error||'Request failed');}const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='';while(true){const {value,done}=await reader.read();buffer+=decoder.decode(value||new Uint8Array(),{stream:!done});const lines=buffer.split('\n');buffer=lines.pop();lines.filter(Boolean).forEach(line=>receive(JSON.parse(line)));if(done)break;}if(buffer.trim())receive(JSON.parse(buffer));if(!completed)throw Error('The connection ended before the run completed.');return run;}catch(error){run.answer.textContent=error.message;run.answer.className='assistant-message error';run.result={is_error:true,latency_ms:0};if(state.active===run)renderTimeline();return run;}}
async function submit(event){event?.preventDefault();if(state.busy||!state.config)return;const message=$('prompt').value.trim();if(!message)return;setBusy(true);$('prompt').value='';try{if(state.scenario==='compare'){state.session=null;const note=el('div','comparison-note','Same prompt · independent conversations · identical tools. One run is illustrative, not a benchmark.');$('welcome')?.remove();$('conversation').append(note);const a=await execute(message,'A',null,'normal','Flash');const b=await execute(message,'B',null,'normal','Flash-Lite');if(!a.result.is_error&&!b.result.is_error){const text=`Flash: ${time(a.result.latency_ms)}, ${money(a.result.cost_usd)} estimated. Flash-Lite: ${time(b.result.latency_ms)}, ${money(b.result.cost_usd)} estimated.`;note.textContent='Same prompt, independent conversations. '+text+' One run is not a benchmark.';}}else{const choice=$('model').value;const run=await execute(message,choice,state.session,state.scenario,choice==='B'?'Flash-Lite':choice==='A'?'Flash':'Automatic');state.session=run.session_id||state.session;}}finally{setBusy(false);$('prompt').focus();}}
async function health(){try{const response=await fetch('/api/health');if(!response.ok)throw Error();const data=await response.json();$('signals').replaceChildren();Object.entries(data.signals).forEach(([name,status])=>{const card=el('div','signal '+status.state),title=el('strong');title.append(el('i','dot'),document.createTextNode(name[0].toUpperCase()+name.slice(1)));card.append(title,el('small','',status.state==='accepted'?`Accepted · ${status.age_seconds}s ago`:status.state==='waiting'?'Waiting for export':'Export failed'));$('signals').append(card);});}catch{$('signals').replaceChildren(el('span','step-note error','Demo server unavailable'));}}
async function init(){try{const response=await fetch('/api/config');if(!response.ok)throw Error('Unable to load demo configuration');state.config=await response.json();$('scenario-select').replaceChildren();$('connection-details').replaceChildren();$('service-link').href=state.config.apm_url;state.config.scenarios.forEach(s=>{const option=el('option','',s.title);option.value=s.id;$('scenario-select').append(option);});[['Service',state.config.service],['Document retrieval',state.config.retrieval_backend+' · curated public references'],['Telemetry',state.config.otlp_endpoint],['Redaction','During indexing · '+state.config.privacy_pipeline],['Trace stream',state.config.trace_stream],['Log stream',state.config.log_stream]].forEach(([k,v])=>$('connection-details').append(el('dt','',k),el('dd','',v)));choose(state.scenario);health();clearInterval(state.healthTimer);state.healthTimer=setInterval(health,10000);$('send').disabled=state.busy;}catch(error){toast(error.message);$('send').disabled=true;}}
const settingsDialog=$('settings-dialog');
let settingsToken='',loadedSettings=null,settingsPending=false;
function settingsStatus(message,kind=''){$('settings-status').textContent=message;$('settings-status').className='settings-status '+kind;}
async function loadSettings(){
  const response=await fetch('/api/settings',{cache:'no-store'});const data=await response.json();
  if(!response.ok)throw Error(data.error||'Unable to load connection settings.');
  loadedSettings=data;settingsToken=data.csrf_token;
  $('elastic-endpoint').value=data.elasticsearch_endpoint;$('elastic-kibana').value=data.kibana_endpoint;$('elastic-otlp').value=data.otlp_endpoint;
  $('elastic-api-key').value='';$('elastic-api-key').placeholder=data.api_key_configured?'Saved key · leave blank to keep it':'Encoded Elastic API key';
  $('key-help').textContent=data.api_key_configured?'Leave blank to keep the saved key. Enter a new key when changing the destination.':'Stored privately on this server. Used for configuration checks, search and telemetry.';
}
$('show-settings').onclick=async()=>{
  settingsDialog.showModal();$('settings-fields').disabled=true;settingsStatus('Loading connection…');
  try{await loadSettings();settingsStatus('');$('settings-fields').disabled=false;$('elastic-endpoint').focus();}
  catch(error){settingsStatus(error.message,'error');}
};
$('close-settings').onclick=()=>settingsDialog.close();
settingsDialog.addEventListener('close',()=>{$('elastic-api-key').value='';$('show-settings').focus();});
settingsDialog.addEventListener('cancel',event=>{if(settingsPending)event.preventDefault();});
$('elastic-endpoint').addEventListener('change',()=>{
  try{
    const url=new URL($('elastic-endpoint').value.trim());
    if(url.hostname.endsWith('.elastic.cloud')){
      if(url.hostname.includes('.kb.'))url.hostname=url.hostname.replace('.kb.','.es.');
      if(url.hostname.includes('.es.')){
        $('elastic-endpoint').value=url.origin;
        const kb=new URL(url.origin),otlp=new URL(url.origin);kb.hostname=kb.hostname.replace('.es.','.kb.');otlp.hostname=otlp.hostname.replace('.es.','.ingest.');
        $('elastic-kibana').value=kb.origin;$('elastic-otlp').value=otlp.origin;
      }
    }else if(url.origin!==loadedSettings?.elasticsearch_endpoint){$('elastic-kibana').value='';$('elastic-otlp').value='';}
  }catch{}
});
async function connectionAction(save){
  if(settingsPending||!$('settings-form').reportValidity())return;
  if(save&&state.busy){settingsStatus('Wait for this run to finish before saving.','error');return;}
  settingsPending=true;$('settings-fields').disabled=true;$('close-settings').disabled=true;
  settingsStatus(save?'Checking the connection before saving…':'Checking authentication, ingest pipeline and telemetry…');
  const body={elasticsearch_endpoint:$('elastic-endpoint').value.trim(),kibana_endpoint:$('elastic-kibana').value.trim(),otlp_endpoint:$('elastic-otlp').value.trim(),api_key:$('elastic-api-key').value};
  try{
    const response=await fetch(save?'/api/settings':'/api/settings/test',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':settingsToken},body:JSON.stringify(body)});
    body.api_key='';const result=await response.json();
    if(!response.ok)throw Error(result.error||'Connection check failed.');
    if(!save){settingsStatus('Connection verified.\n'+result.checks.join(' · '),'success');return;}
    $('elastic-api-key').value='';
    if(result.restarting){
      settingsStatus('Saved. Reconnecting the demo…');
      const previousToken=settingsToken;let ready=false;
      for(let attempt=0;attempt<60;attempt++){
        await new Promise(resolve=>setTimeout(resolve,1000));
        try{await loadSettings();if(settingsToken!==previousToken){ready=true;break;}}catch{}
      }
      if(!ready)throw Error('Settings were saved. Reconnection is taking longer than expected; reopen the demo shortly.');
      state.session=null;await init();
    }else{await loadSettings();}
    settingsStatus('Connection saved and verified. Your next run is ready.','success');
  }catch(error){settingsStatus(error.message,'error');}
  finally{body.api_key='';settingsPending=false;$('settings-fields').disabled=false;$('close-settings').disabled=false;}
}
$('test-connection').onclick=()=>connectionAction(false);
$('settings-form').addEventListener('submit',event=>{event.preventDefault();connectionAction(true);});

$('scenario-select').onchange=()=>choose($('scenario-select').value);
const architectureDialog=$('architecture-dialog');
$('show-architecture').onclick=()=>architectureDialog.showModal();
$('close-architecture').onclick=()=>architectureDialog.close();
architectureDialog.addEventListener('close',()=>$('show-architecture').focus());
architectureDialog.addEventListener('click',event=>{
  if(event.target!==architectureDialog)return;
  const bounds=architectureDialog.getBoundingClientRect();
  if(event.clientX<bounds.left||event.clientX>bounds.right||event.clientY<bounds.top||event.clientY>bounds.bottom)architectureDialog.close();
});
document.querySelectorAll('[data-diagram]').forEach(button=>button.addEventListener('click',()=>{
  document.querySelectorAll('[data-diagram]').forEach(item=>item.setAttribute('aria-pressed',String(item===button)));
  document.querySelectorAll('.diagram-view').forEach(view=>{view.hidden=view.id!=='diagram-'+button.dataset.diagram;});
}));
$('composer').addEventListener('submit',submit);$('prompt').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();submit();}});$('reset').onclick=clear;$('model').onchange=()=>{state.session=null;toast('Model changed. The next run starts a fresh conversation.');};$('refresh-health').onclick=health;init();
