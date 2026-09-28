/* One message renderer shared by the desktop reader and offline HTML exports. */
window.ChatRenderer = (() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const url = value => {try {const u = new URL(value); return ['http:', 'https:'].includes(u.protocol) && !u.username ? u.href : '';} catch {return '';}};
  const colors = ['sage', 'sand', 'rose', 'blue', 'lavender', 'peach'];
  const color = value => colors[[...String(value || '')].reduce((a,c)=>a+c.codePointAt(0),0)%colors.length];
  const initials = name => [...String(name || '未知').replace(/^wxid_/, '')].slice(0,1).join('').toUpperCase();
  const avatar = (name, photo='', extra='') => `<span class="avatar ${photo?'original-avatar':color(name)} ${extra}">${photo ? `<img src="${esc(photo)}" alt="${esc(name)}" loading="lazy">` : esc(initials(name))}</span>`;
  const names = {text:'文本', image:'图片', video:'视频', audio:'语音', emoji:'表情包', file:'文件', link:'分享链接', system:'系统消息',quote:'引用消息', forward:'聊天记录',transfer:'转账',redpacket:'红包',call:'音视频通话',location:'位置',contact:'名片',miniapp:'小程序',channel:'视频号',unknown:'其他消息'};
  function body(m, asset = p=>p, depth=0) {
    const d = m.detail || {}, kind = m.kind, src = m.media_path ? asset(m.media_path) : '';
    const label = names[kind] || '消息';
    const text = `<div class="message-text">${esc(m.body || '')}</div>`;
    if (kind==='text' || kind==='unknown') return text;
    if (kind==='quote') return `${text}<button class="quote-card" data-ref="${esc(d.quote?.server_id || '')}"><b>${esc(d.quote?.sender || '引用消息')}</b><span>${esc(d.quote?.body || '原消息内容不可用')}</span><small>查看原消息 ↗</small></button>`;
    if (kind==='image' || kind==='emoji') return src ? `<a class="image-link" href="${esc(src)}" target="_blank" rel="noopener"><img class="message-image ${kind==='emoji'?'emoji-image':''}" src="${esc(src)}" alt="${label}" loading="lazy"></a>${m.media_status && m.media_status!=='已恢复'?`<small class="media-caption">${esc(m.media_status)}</small>`:''}` : missing(label,m.media_status);
    if (kind==='video') return src ? `<video controls preload="metadata" class="message-video" src="${esc(src)}"></video><a class="attachment-link" href="${esc(src)}" target="_blank" rel="noopener">打开视频 ↗</a><a class="attachment-link" href="${esc(src)}" download>保存视频 ↓</a>` : missing(label,m.media_status);
    if (kind==='audio') return `<div class="audio-card"><span class="audio-wave">▂ ▅ ▃ ▇ ▅ ▂</span><b>语音消息</b>${d.duration?`<span>${Math.round(Number(d.duration)/1000)}″</span>`:''}</div>${src?`<audio controls preload="none" src="${esc(src)}"></audio><a class="attachment-link" href="${esc(src)}" download>保存语音 ↓</a>`:`<small class="media-caption">${esc(m.media_status || '本机未保存语音')}</small>`}${d.transcript?`<div class="transcript">${esc(d.transcript)}</div>`:''}`;
    if (kind==='file') return `<div class="file-card"><span class="file-symbol">${esc((d.extension || (d.filename||'').split('.').pop() || 'FILE').slice(0,6).toUpperCase())}</span><div><b>${esc(d.title || d.filename || m.body || '文件')}</b><small>${d.size?formatSize(Number(d.size)):esc(m.media_status || '本地附件')}</small></div></div>${src?`<a class="attachment-link" href="${esc(src)}" download="${esc(d.filename || d.title || '')}">保存附件 ↓</a>`:`<small class="media-caption">${esc(m.media_status || '本机未保存原文件')}</small>`}`;
    if (kind==='forward') return `<details class="forward-card"><summary><span class="card-kicker">聊天记录</span><b>${esc(d.title || m.body || '合并转发')}</b><span>${esc(d.description || `展开 ${d.items?.length || 0} 条消息`)}</span></summary><div class="forward-items">${depth<8?(d.items||[]).map(item=>`<div class="forward-item"><small>${esc(item.sender_name || '')} ${esc(item.time || '')}</small>${body({...item, detail:item.detail||{items:item.items}},asset,depth+1)}</div>`).join(''):''}${!d.items?.length?'<p class="media-caption">此记录未包含可展开的消息内容</p>':''}</div></details>`;
    if (kind==='transfer' || kind==='redpacket') return `<div class="transfer-card"><span class="transfer-symbol">${kind==='transfer'?'↔':'礼'}</span><div><b>${esc(d.amount || m.body || label)}</b><small>${esc(d.memo || label)}</small></div></div><div class="card-foot">微信${label}</div>`;
    if (kind==='location') {
      const coords = Number(d.latitude) && Number(d.longitude) ? `https://uri.amap.com/marker?position=${encodeURIComponent(d.longitude+','+d.latitude)}&name=${encodeURIComponent(d.name || d.label || '位置')}` : '';
      return `<div class="location-card"><div class="map-art"><span>⌖</span></div><b>${esc(d.name || m.body || '位置分享')}</b><p>${esc(d.label || '')}</p>${coords?`<a href="${esc(coords)}" target="_blank" rel="noopener noreferrer">在地图中查看 ↗</a>`:''}</div>`;
    }
    if (kind==='contact') return `<div class="contact-message">${avatar(d.nickname || m.body)}<div><b>${esc(d.nickname || m.body || '联系人')}</b><small>${esc(d.alias || d.username || '')}</small></div></div><div class="card-foot">个人名片</div>`;
    if (kind==='call') return `<div class="call-card"><span>◖◗</span>${esc(m.body || '音视频通话')}</div>`;
    const link = url(d.url);
    const content = `<span class="card-kicker">${esc(kind==='miniapp' ? d.appname || '小程序' : kind==='channel' ? '视频号 · '+(d.author||'') : d.appname || '分享链接')}</span><b>${esc(d.title || m.body || label)}</b>${d.description?`<p>${esc(d.description)}</p>`:''}<span class="card-foot">${link?'打开链接 ↗':kind==='miniapp'?'此消息未附带可访问的网页链接':label}</span>`;
    return link ? `<a class="rich-link" href="${esc(link)}" target="_blank" rel="noopener noreferrer">${content}</a>` : `<div class="rich-link">${content}</div>`;
  }
  const missing = (label, reason) => `<div class="missing-media"><span>▧</span><b>${label}</b><small>${esc(reason || '本机未保存原文件')}</small></div>`;
  const formatSize = n => n>1048576?(n/1048576).toFixed(1)+' MB':n>1024?Math.round(n/1024)+' KB':n+' B';
  function message(m, asset = p=>p) {
    if (m.kind==='system') return `<div class="system-message" data-message-id="${esc(m.server_id||'')}" id="msg-${m.id}">${esc(m.body || '系统消息')}</div>`;
    return `<article class="message-row ${m.is_self?'outgoing':'incoming'}" id="msg-${m.id}" data-message-id="${esc(m.server_id||'')}">${avatar(m.is_self?'我':m.sender_name,m.sender_avatar?asset(m.sender_avatar):'')}<div class="message-stack"><div class="message-meta"><span>${esc(m.is_self?'我':m.sender_name||'未知发送者')}</span><time>${esc((m.time||'').slice(11,16))}</time>${m.detail?.recalled?'<span class="recalled-badge">已撤回 · 归档保留</span>':''}</div><div class="message-bubble kind-${esc(m.kind)}">${body(m,asset)}</div></div></article>`;
  }
  function list(messages, asset = p=>p) {
    let last='', html='';
    for (const m of messages) {
      const date = (m.time||'').slice(0,10);
      if (date!==last) {html+=`<div class="date-separator"><span>${esc(date)}</span></div>`; last=date;}
      html+=message(m,asset);
    }
    return html;
  }
  return {esc, url, color, initials, avatar, names, body, message, list};
})();
