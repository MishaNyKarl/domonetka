window.DomonetkaSSOUI={render(me){
 document.getElementById('sso-link-button').hidden=!me.sso_can_link||!!me.sso;
 document.getElementById('logout-all').hidden=!me.sso||!!me.sso_local;
 const target=document.getElementById('sso-link-target-help');target.hidden=!me.sso_link_target;
 target.textContent=me.sso_link_target?'Для этого пользователя выбран аккаунт OpsVault: '+me.sso_link_target+'.':'';
}};
if(state.me)DomonetkaSSOUI.render(state.me);
api('/api/auth/config').then(c=>{$('sso-login').hidden=!c.enabled;$('sso-existing-help').hidden=!c.enabled;$('sso-new-help').hidden=!c.auto_account_creation;}).catch(()=>{});
$('sso-link-button').onclick=()=>{$('sso-link-password').value='';$('sso-link-error').textContent='';$('sso-link-dialog').showModal();};
$('sso-link-form').onsubmit=async e=>{e.preventDefault();$('sso-link-start').disabled=true;try{
 const result=await api('/api/auth/link/start',{password:$('sso-link-password').value,csrf:state.me.csrf,save_credentials:true});
 $('sso-link-password').value='';location.assign(result.redirect);
}catch(e){$('sso-link-error').textContent=e.message;}finally{$('sso-link-start').disabled=false;}};
async function showSSOConfirmation(){try{
 const result=await api('/api/auth/link/status');
 if(!result.pending)return;
 $('sso-confirm-accounts').textContent='Домонетка: '+result.local_name+' → OpsVault: '+result.opsvault_name+' ('+result.issuer+'). Роль в сервисе: '+(result.role==='admin'?'администратор':'пользователь')+'.';
 $('sso-confirm-error').textContent='';$('sso-confirm-dialog').showModal();
}catch(e){toast(e.message);}}
$('sso-link-confirm').onclick=async()=>{const b=$('sso-link-confirm');b.disabled=true;try{
 const result=await api('/api/auth/link/confirm',{csrf:state.me.csrf});location.assign(result.redirect);
}catch(e){$('sso-confirm-error').textContent=e.message;}finally{b.disabled=false;}};
$('sso-link-cancel').onclick=async()=>{try{await api('/api/auth/link/cancel',{csrf:state.me.csrf});$('sso-confirm-dialog').close();}catch(e){$('sso-confirm-error').textContent=e.message;}};
$('logout-all').onclick=async()=>{if(!confirm('Выйти из OpsVault и связанных сервисов в этом браузере?'))return;let message='';try{
 await api('/api/auth/logout-all',{csrf:state.me.csrf});
}catch(e){message=e.message;}finally{
 try{await refresh();}catch(e){if(!message)message=e.message;}
 if(message)toast(message);
}};
if(new URLSearchParams(location.search).get('sso_link')==='1'){
 history.replaceState(null,'','/admin');
 // Initial admin fetch can still be in flight when deferred scripts run.
 refresh().then(showSSOConfirmation).catch(e=>toast(e.message));
}
