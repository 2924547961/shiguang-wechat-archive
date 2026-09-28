const archive = JSON.parse(document.getElementById('archive-data').textContent);
let filtered = archive.messages, page = 0, size = 150;
function render() {
  const rows = filtered.slice(page*size,(page+1)*size);
  document.getElementById('messages').innerHTML = rows.length ? ChatRenderer.list(rows) : '<div class="empty">没有找到符合条件的消息</div>';
  document.getElementById('status').textContent = `共 ${filtered.length.toLocaleString()} 条消息 · 第 ${filtered.length? page+1:0} / ${Math.ceil(filtered.length/size)} 页`;
  ['prev','prev-bottom'].forEach(id=>document.getElementById(id).disabled=page===0);
  ['next','next-bottom'].forEach(id=>document.getElementById(id).disabled=(page+1)*size>=filtered.length);
}
function turn(delta){page=Math.max(0,page+delta);render();window.scrollTo({top:0,behavior:'smooth'});}
['prev','prev-bottom'].forEach(id=>document.getElementById(id).onclick=()=>turn(-1));
['next','next-bottom'].forEach(id=>document.getElementById(id).onclick=()=>turn(1));
let timer;
document.getElementById('search').oninput=e=>{clearTimeout(timer);timer=setTimeout(()=>{const q=e.target.value.toLocaleLowerCase();filtered=archive.messages.filter(m=>(m.body||'').toLocaleLowerCase().includes(q));page=0;render();},180);};
document.getElementById('jump').onclick=()=>{const date=document.getElementById('date').value;if(!date)return;filtered=archive.messages;document.getElementById('search').value='';const i=filtered.findIndex(m=>(m.time||'').slice(0,10)>=date);page=Math.floor((i<0?Math.max(0,filtered.length-1):i)/size);render();window.scrollTo(0,0);};
document.addEventListener('click',e=>{const ref=e.target.closest('[data-ref]');if(!ref)return;const id=ref.dataset.ref;const index=archive.messages.findIndex(m=>id&&m.server_id===id);if(index<0){document.getElementById('status').textContent='引用的原消息未包含在本次导出中。';return;}filtered=archive.messages;page=Math.floor(index/size);document.getElementById('search').value='';render();const el=document.getElementById('msg-'+filtered[index].id);el?.scrollIntoView({block:'center',behavior:'smooth'});el?.classList.add('reference-highlight');});
render();
