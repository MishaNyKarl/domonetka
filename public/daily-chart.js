/* The chart uses the storefront's cached daily counters; no tracker requests. */
window.DomonetkaDailyChart=(()=>{
 const dayMs=86400000,labels={impressions:'Показы',clicks:'Клики',ctr:'CTR'};
 function series(rows,period={}){
  const byDay=new Map(rows.map(r=>[r.day,r]));
  const dates=[...byDay.keys()].sort();
  if(!dates.length)return [];
  const start=Date.parse((period.start||dates[0])+'T00:00:00Z');
  const end=Date.parse((period.end||dates.at(-1))+'T00:00:00Z');
  const result=[];
  for(let time=start;time<=end;time+=dayMs){
   const day=new Date(time).toISOString().slice(0,10),r=byDay.get(day);
   const impressions=Number(r?.impressions)||0,clicks=Number(r?.clicks)||0;
   result.push({day,impressions,clicks,ctr:impressions?100*clicks/impressions:0});
  }
  return result;
 }
 function render(root,rows,metric,period,summary){
  root.replaceChildren();const data=series(rows,period);const label=labels[metric]||labels.impressions;
  if(!data.length){const empty=document.createElement('div');empty.className='daily-chart-empty';empty.textContent='Нет данных за выбранный период';root.append(empty);summary.textContent='';return;}
  const format=v=>metric==='ctr'?v.toLocaleString('ru',{maximumFractionDigits:2})+'%':v.toLocaleString('ru',{maximumFractionDigits:0});
  const values=data.map(d=>d[metric]||0),max=Math.max(...values,1);
  summary.textContent=label+' · максимум за день: '+format(Math.max(...values));
  const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');
  function node(tag,attrs={},text){const n=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,v);if(text!==undefined)n.textContent=text;return n;}
  svg.setAttribute('viewBox','0 0 900 230');svg.setAttribute('role','img');svg.setAttribute('aria-label',label+' по дням, '+data[0].day+' — '+data.at(-1).day);
  const x=i=>data.length===1?480:60+i*810/(data.length-1),y=v=>190-v/max*160;
  for(let i=0;i<=4;i++){const v=max*i/4,yy=y(v);svg.append(node('line',{x1:60,x2:870,y1:yy,y2:yy,class:'chart-grid'}),node('text',{x:50,y:yy+4,'text-anchor':'end'},format(v)));}
  const path=data.map((d,i)=>(i?'L':'M')+x(i)+' '+y(d[metric]||0)).join(' ');
  if(data.length>1)svg.append(node('path',{d:path+' L870 190 L60 190 Z',class:'chart-area'}));
  svg.append(node('path',{d:path,class:'chart-line'}));
  // Large periods keep the SVG small; the exact daily values remain in the table.
  const step=Math.max(1,Math.ceil(data.length/90));
  data.forEach((d,i)=>{if(i%step&&i!==data.length-1)return;const dot=node('circle',{cx:x(i),cy:y(d[metric]||0),r:3,class:'chart-point'});dot.append(node('title',{},d.day+' · '+label+': '+format(d[metric]||0)));svg.append(dot);});
  const ticks=[...new Set([0,Math.floor((data.length-1)/2),data.length-1])];
  for(const i of ticks)svg.append(node('text',{x:x(i),y:219,'text-anchor':i===0&&data.length>1?'start':i===data.length-1&&data.length>1?'end':'middle'},data[i].day.slice(8)+'.'+data[i].day.slice(5,7)+'.'+data[i].day.slice(0,4)));
  root.append(svg);
 }
 return {series,render};
})();
