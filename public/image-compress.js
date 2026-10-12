(() => {
  let queue=[], source=null, result=null, previewUrl=null, busy=false;
  const kb=bytes=>(bytes/1024).toFixed(1)+' КБ';
  function preview(blob){if(previewUrl)URL.revokeObjectURL(previewUrl);previewUrl=URL.createObjectURL(blob);$('compress-preview').src=previewUrl;}
  function resetResult(){result=null;if(queue[0])preview(queue[0]);$('compress-quality-value').textContent=$('compress-quality').value+'%';$('compress-upload').disabled=true;$('compress-result').textContent='Выберите настройки и нажмите «Сжать».';}
  function settings(){return {target:Number($('compress-target').value)*1024,quality:Number($('compress-quality').value)/100,width:Number($('compress-width').value)};}
  function setBusy(value){busy=value;for(const id of ['compress-run','compress-upload','compress-cancel','compress-preset','compress-target','compress-quality','compress-width'])$(id).disabled=value; if(!value)$('compress-upload').disabled=!result;}
  async function next(){
    if(source)source.close();source=null;result=null;
    if(!queue.length){if(previewUrl)URL.revokeObjectURL(previewUrl);previewUrl=null;$('compress-preview').removeAttribute('src');$('compress-dialog').close();$('upload').value='';count();return;}
    const file=queue[0];$('compress-error').textContent='';$('compress-original').textContent=file.name+' · '+kb(file.size)+' · файлов в очереди: '+queue.length;
    if(file.size>30*1024*1024){throw Error('Исходный файл: максимум 30 МБ');}
    source=await createImageBitmap(file);
    $('compress-original').textContent+=' · '+source.width+'×'+source.height;
    preview(file);resetResult();$('compress-dialog').showModal();
  }
  $('upload').onchange=async()=>{queue=[...$('upload').files];if(!queue.length)return;try{await next();}catch(e){$('form-error').textContent=e.message;queue=[];$('upload').value='';}};
  $('compress-preset').onchange=()=>{
    const values={fast:[40,65,640],balanced:[80,80,960],quality:[160,90,1280]}[$('compress-preset').value];
    if(values){[$('compress-target').value,$('compress-quality').value,$('compress-width').value]=values;}
    resetResult();
  };
  for(const id of ['compress-target','compress-quality','compress-width'])$(id).oninput=()=>{$('compress-preset').value='custom';resetResult();};
  $('compress-run').onclick=async()=>{
    $('compress-error').textContent='';const config=settings();
    if(!source||!Number.isFinite(config.target)||config.target<10*1024||config.target>500*1024){$('compress-error').textContent='Целевой вес: от 10 до 500 КБ';return;}
    setBusy(true);
    try{
      let width=Math.min(source.width,config.width);let best=null;
      // Limit both dimensions; never upscale. Reduce dimensions only after lowering quality.
      width=Math.min(width,Math.floor(config.width*source.width/source.height));
      for(let scale=0;scale<7;scale++){
        const canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.round(width));canvas.height=Math.max(1,Math.round(width*source.height/source.width));
        canvas.getContext('2d').drawImage(source,0,0,canvas.width,canvas.height);
        for(const quality of [config.quality,Math.max(.35,config.quality-.15),.35]){
          const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/webp',quality));
          if(!$('compress-dialog').open)return;
          if(!blob)throw Error('Браузер не смог сжать изображение');
          if(!best||blob.size<best.blob.size)best={blob,width:canvas.width,height:canvas.height,quality};
          if(blob.size<=config.target){best={blob,width:canvas.width,height:canvas.height,quality};break;}
        }
        if(best.blob.size<=config.target)break;
        if(width<=240)break;width=Math.max(240,Math.floor(width*.8));
      }
      if(queue[0].size<=best.blob.size&&source.width<=config.width&&source.height<=config.width){best={blob:queue[0],width:source.width,height:source.height,quality:null};}
      result=best.blob;preview(result);
      const seconds=(result.size*8/1000000).toFixed(2);
      $('compress-result').textContent=kb(queue[0].size)+' → '+kb(result.size)+' · '+best.width+'×'+best.height+' · '+result.type+' · экономия '+Math.max(0,100-result.size/queue[0].size*100).toFixed(0)+'%. Передача при 1 Мбит/с: примерно '+seconds+' с, без задержки сети.'+(result.size>config.target?' Целевой вес не достигнут — уменьшите размер или увеличьте лимит.':'');
    }catch(e){result=null;$('compress-error').textContent=e.message;}finally{setBusy(false);if(!$('compress-dialog').open)cancel();}
  };
  $('compress-upload').onclick=async()=>{
    if(!result||busy)return;setBusy(true);$('compress-error').textContent='';
    try{
      const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=reject;reader.readAsDataURL(result);});
      const uploaded=await api('/api/upload',{data});$('images').value=[$('images').value.trim(),uploaded.url].filter(Boolean).join('\n');queue.shift();toast('Оптимизированная картинка загружена');
      $('compress-dialog').close();setBusy(false);await next();
    }catch(e){$('compress-error').textContent=e.message;setBusy(false);}
  };
  function cancel(){queue=[];if(source)source.close();source=null;result=null;if(previewUrl)URL.revokeObjectURL(previewUrl);previewUrl=null;$('compress-preview').removeAttribute('src');$('compress-dialog').close();$('upload').value='';count();}
  $('compress-cancel').onclick=cancel;
  $('compress-dialog').addEventListener('cancel',e=>{e.preventDefault();$('compress-dialog').close();if(!busy)cancel();else queue=[];});
})();
