'use strict';
/* Chat memory and expression practice are separate from automatic replies. */
window.PersonalUI = (() => {
 let cid = '', accountId = '', timer = 0, page = 'memory';
 const contactPicker = people => `<label class="field-label">选择好友<select id="personal-contact"><option value="">请选择</option>${people.map(p => `<option value="${e(p.id)}" ${p.id===cid?'selected':''}>${e(p.title || p.name || p.id)}</option>`).join('')}</select></label>`;
 const taskCard = job => `<div class="panel personal-task"><b>${e(job.action==='personal.memory'?'聊天记忆':'表达练习')}</b><span>${e(job.stage||job.status)} · ${Number(job.percent||0)}%</span><progress max="100" value="${Number(job.percent||0)}"></progress>${job.error?`<p class="muted">${e(job.error)}</p>`:''}${job.status==='running'?btn('取消任务','personal-cancel','','close',`data-id="${e(job.id)}"`):['error','interrupted','cancelled'].includes(job.status)?btn('继续任务','personal-resume','','refresh',`data-id="${e(job.id)}"`):''}</div>`;
 async function render(next) {
   page=next; accountId=state.selected;
   if(!stats){$('#content').innerHTML=needArchive();return;}
   const contacts=await api('/api/conversations?kind=direct&limit=5000');
   const people=contacts.items||[];
   if(!people.some(p=>p.id===cid))cid='';
   $('#content').innerHTML=`<div class="page personal-page">${heading(next==='memory'?'聊天记忆':'表达练习',next==='memory'?'按选定时间范围读取全部文字聊天，分段蒸馏并保存有证据的上下文。':'模型根据聊天记忆出模拟题；你作答后，它会点评并给出改写。')}<section class="panel personal-controls">${contactPicker(people)}<div id="personal-content"></div></section></div>`;
   $('#personal-contact').addEventListener('change',ev=>{cid=ev.target.value;$('#personal-content').innerHTML='';show().catch(err=>toast(err.message));});
   await show();
 }
 async function show() {
   clearTimeout(timer);
   const box=$('#personal-content'); if(!box)return;
   if(!cid){box.innerHTML='<p class="muted">选择一位好友后查看已有记忆和练习。</p>';return;}
   const data=await api('/api/personal/state?'+new URLSearchParams({cid}));
   if(accountId!==state.selected||!box.isConnected)return;
   const active=data.jobs.find(j=>j.status==='running');
   const tasks=data.jobs.slice(0,3).map(taskCard).join('');
   if(page==='memory'){
     const m=data.memory;
     const range=await api('/api/insights/range?'+new URLSearchParams({cid}));
     const previousStart=$('#personal-start')?.value||'',previousEnd=$('#personal-end')?.value||'';
     const bounds=`min="${e(range.first||'')}" max="${e(range.last||'')}"`;
     box.innerHTML=`<div class="date-fields"><label>开始日期<input id="personal-start" type="date" ${bounds} value="${e(previousStart)}"></label><label>结束日期<input id="personal-end" type="date" ${bounds} value="${e(previousEnd)}"></label></div><p class="muted">${num(range.count)} 条文字 · ${e(range.first||'')} 至 ${e(range.last||'')}。不选日期时读取整段聊天；结束日期包含当天。仅把文字发送给当前配置的在线模型。</p>${btn(m?'更新记忆':'生成记忆','personal-start','primary','spark',active?'disabled':'')}<div id="personal-tasks">${tasks}</div>${m?`<section class="personal-result"><h2>已保存的聊天记忆</h2><p>${num(m.count)} 条文字 · ${num(m.chunks)} 段 · 最近复用 ${num(m.reused)} 段 · ${e(m.model||'')}</p><label class="selection-controls"><input id="personal-enable" type="checkbox" ${m.enabled?'checked':''}>允许这位好友的 AI 自动回复参考这份记忆</label><p class="muted">记忆只帮助理解当前消息；不会复制历史回复，也不会把推测当事实。</p><div class="personal-summary">${e(m.summary||'')}</div></section>`:''}`;
     $('#personal-enable')?.addEventListener('change',async event=>{try{await api('/api/personal/memory',{cid,enabled:event.target.checked});toast(event.target.checked?'已为这位好友启用聊天记忆':'已关闭聊天记忆');}catch(err){event.target.checked=!event.target.checked;toast(err.message);}});
   } else {
     const sessions=data.sessions||[];
     const s=sessions[0]; window.PersonalUI.session=s||null;
     box.innerHTML=`<label class="field-label">训练目标<input id="personal-goal" value="${e(s?.goal||'清楚表达感受、需求和边界')}" maxlength="2000"></label>${btn('开始新一轮练习','personal-new','primary','spark',active?'disabled':'')}<div id="personal-tasks">${tasks}</div>${s?`<section class="personal-result"><h2>最近一次练习</h2>${s.turns.map(t=>`<div class="personal-turn ${t.role==='user'?'mine':''}"><b>${t.role==='user'?'我的回答':'表达教练'}</b><div>${e(t.text)}</div></div>`).join('')}<label class="field-label">我的回答<textarea id="personal-answer" rows="5" maxlength="20000" placeholder="根据上面的情境写出你会怎样回应"></textarea></label>${btn('提交回答并获取点评','personal-answer','primary','arrow',active?'disabled':'')}</section>`:''}`;
   }
   if(active)timer=setTimeout(()=>show().catch(err=>toast(err.message)),800);
 }
 async function start(payload){
   if(!cid)throw Error('请先选择好友。');
   await api('/api/personal/start',{cid,...payload});
   await show();
 }
 const actions={
   'personal-start':async()=>{const start=$('#personal-start')?.value,end=$('#personal-end')?.value;const from=start?new Date(start+'T00:00:00').getTime()/1000:null;const to=end?new Date(end+'T00:00:00').getTime()/1000+86400:null;await start({action:'memory',start:from,end:to});},
   'personal-new':()=>start({action:'coach',goal:$('#personal-goal')?.value||''}),
   'personal-answer':()=>{const answer=$('#personal-answer')?.value?.trim();if(!answer)throw Error('请先填写回答。');const s=window.PersonalUI.session;if(!s)throw Error('练习会话已变化，请刷新。');return start({action:'coach',session_id:s.id,answer});},
   'personal-cancel':async node=>{await api('/api/harness/cancel',{id:node.dataset.id});await show();},
   'personal-resume':async node=>{await api('/api/harness/resume',{id:node.dataset.id});await show();}
 };
 return {render,actions,get session(){return this._session},set session(value){this._session=value}};
})();
