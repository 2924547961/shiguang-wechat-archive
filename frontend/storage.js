'use strict';
window.StorageUI=(()=>{
 async function render(){
  const location=await api('/api/storage/location');
  if(route!=='settings')return;
  const title=$('#content .settings-section h2');
  if(!title)return;
  title.insertAdjacentHTML('afterend',`<div class="setting-row stack"><label for="settings-archive">聊天归档目录</label><p>当前使用：${e(location.current)}。更改后在下次启动生效，现有归档不会自动移动。</p><div class="setting-path"><input id="settings-archive" value="${e(location.next)}" aria-label="下次启动使用的归档目录">${btn('选择文件夹','storage-browse','','folder')}${btn('保存归档位置','storage-save','','check')}</div><p id="storage-location-note">首次使用可以选择任意已有文件夹；建议放在非项目目录中。</p></div>`);
 }
 const actions={
  'storage-browse':()=>{const input=$('#settings-archive');if(native?.chooseFolder)native.chooseFolder('archive_dir',input?.value||'');else{input?.focus();toast('预览模式可直接填写已有文件夹路径。');}},
  'storage-save':async()=>{const selected=$('#settings-archive')?.value?.trim();if(!selected)throw Error('请选择已有的归档目录。');const location=await api('/api/storage/location',{path:selected});$('#settings-archive').value=location.next;$('#storage-location-note').textContent='已保存。关闭并重新打开软件后，将使用新目录；旧归档仍保留在原位置。';toast('归档位置已保存，重启软件后生效。');}
 };
 return {render,actions};
})();
