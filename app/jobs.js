const esc=s=>(s??'').toString().replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const LOCAL=__LOCAL__;
const S=(()=>{try{return JSON.parse(localStorage.jobOpen||'{}')}catch(e){return {}}})();
let JOBS={},ORDER=[],timer=null;
function cp(b,t){navigator.clipboard.writeText(t).then(()=>{const o=b.textContent;b.textContent='Skopiowano ✓';setTimeout(()=>b.textContent=o,1500)}).catch(()=>{prompt('Skopiuj:',t)})}
const fmtT=s=>{s=Math.max(0,Math.round(s));return s>=60?Math.floor(s/60)+' min '+(s%60)+' s':s+' s'};
const STEPS=['Pobieranie','Transkrypcja','Wybór momentów','Montaż'];
function stepOf(j){const s=j.stage||'';return j.kind=='rerender'?3:s.startsWith('Montuję')?3:s.startsWith('Szukam')?2:s.startsWith('Transkrypcja')?1:0}
let FIRST=null;function isOpen(id){return id in S?S[id]:FIRST==id}
function sig(j){return JSON.stringify([j.status,j.stage,j.progress,j.total,j.finished,(j.clips||[]).map(c=>[c.ver,c.title,c.caption]),isOpen(j.id),j.status=='running'?Math.floor(Date.now()/5000):0])}
function allCaps(j){return (j.clips||[]).map((c,k)=>`#${k+1} ${c.title}\n${c.caption}`).join('\n\n')}
function html(j){
 const t=new Date(j.created*1000).toLocaleString('pl-PL');
 let h=`<div class=jh><div><b>${esc(j.title||j.url)}</b><div class=st>${t}${j.kind=='rerender'?'':' · '+j.n+' klipów · '+esc(j.lang)}</div></div>
  <button class="s ic" data-act=del title="Usuń z listy">🗑</button></div>`;
 const el=j.started?((j.finished||Date.now()/1000)-j.started):0;
 if(j.status=='queued')h+=`<p>⏳ W kolejce…</p><button class=s data-act=cancel>✖ Anuluj</button>`;
 else if(j.status=='running'){const st=stepOf(j);
  const pct=j.total?Math.round(100*(j.progress||0)/j.total):[5,30,70,80][st];
  let eta='';if(st==3&&j.progress>0&&j.render_started){const per=(Date.now()/1000-j.render_started)/j.progress;eta=' · zostało ok. '+fmtT(per*(j.total-j.progress))}
  h+=`<div class=steps>${STEPS.map((s,k)=>`<span class="${k<st?'ok':k==st?'on':''}">${k<st?'✓ ':''}${s}</span>`).join('')}</div>
   <p>${esc(j.stage)}<br><span class=st>czas: ${fmtT(el)}${eta}</span></p><div class=bar><div style="width:${pct}%"></div></div><p></p><button class=s data-act=cancel>✖ Przerwij</button>`}
 else if(j.status=='error'||j.status=='cancelled'){
  h+=`<div class=err>${j.status=='cancelled'?'✖ Przerwano.':'❌ '+esc(j.stage)}</div><p class=st>${j.status=='error'?'Szczegóły w logu zadania. Jeśli YouTube prosi o zalogowanie – Ustawienia → cookies.txt.':''}</p>
   <button class=s data-act=rerun>🔁 Spróbuj ponownie</button>`}
 else if(j.kind=='rerender'){h+=`<p>✅ Gotowe – <a href="/edit/${encodeURIComponent(j.video_id)}/${j.clip}">otwórz klip</a></p>`}
 else{const open=isOpen(j.id),base='/files/'+encodeURIComponent(j.video_id)+'/',n=(j.clips||[]).length;
  h+=`<p>✅ Gotowe – ${n} klipów${el?' <span class=st>('+fmtT(el)+')</span>':''}</p><div class=acts>
   ${n?`<a class=btn href="/zip/${encodeURIComponent(j.id)}">⬇ ZIP</a> <button class=s data-act=toggle>${open?'▲ Zwiń':'▼ Pokaż klipy'}</button> <button class=s data-act=copyall>📋 Kopiuj wszystkie opisy</button>`:'<span class=st>Brak klipów – film za krótki lub bez mowy?</span>'}
   ${LOCAL?'<button class=s data-act=folder>📂 Otwórz folder</button>':''} <button class=s data-act=rerun>🔁 Zrób ponownie</button></div>`;
  if(open&&n)h+='<div class=clips>'+j.clips.map((c,k)=>`<div><video id="v-${esc(j.id)}-${k}" controls playsinline preload=metadata src="${base+encodeURIComponent(c.file)}?v=${c.ver||0}#t=0.5"></video>
   <b>${esc(c.title)}</b><div class=st>${c.duration} s · score ${c.score}</div>
   <p><a class=btn href="${base+encodeURIComponent(c.file)}?dl=1&v=${c.ver||0}">⬇ Pobierz</a> <a class=btn style="background:#2b2f3a" href="/edit/${encodeURIComponent(j.video_id)}/${parseInt((c.file.match(/[0-9]+/)||['1'])[0],10)}">✏️ Edytuj</a></p>
   <div class=cap>${esc(c.caption)}</div><button class=s data-act=copy data-k=${k}>Kopiuj</button><span class=st> opis + hashtagi</span></div>`).join('')+'</div>'}
 return h}
function render(){const box=document.getElementById('jobs');
 [...box.children].forEach(el=>{if(!(el.dataset.id in JOBS))el.remove()});
 ORDER.forEach((id,i)=>{const j=JOBS[id];let el=box.querySelector(`[data-id="${CSS.escape(id)}"]`);
  if(!el){el=document.createElement('div');el.className='card job';el.dataset.id=id}
  const k=sig(j);if(el.dataset.sig!==k){el.innerHTML=html(j);el.dataset.sig=k}
  if(box.children[i]!==el)box.insertBefore(el,box.children[i]||null)})}
async function load(){clearTimeout(timer);let js;
 try{const r=await fetch('/api/jobs');if(r.status==401)return location.reload();js=await r.json()}catch(e){timer=setTimeout(load,5000);return}
 JOBS={};ORDER=js.map(j=>(JOBS[j.id]=j).id);FIRST=(js.find(j=>j.status=='done'&&j.kind!='rerender'&&(j.clips||[]).length)||{}).id;render();
 if(js.some(j=>j.status=='queued'||j.status=='running'))timer=setTimeout(load,3000)}
document.getElementById('jobs').addEventListener('click',async e=>{const b=e.target.closest('[data-act]');if(!b)return;
 const el=b.closest('.job'),id=el.dataset.id,j=JOBS[id],a=b.dataset.act;if(!j)return;
 if(a=='toggle'){S[id]=!isOpen(id);localStorage.jobOpen=JSON.stringify(S);render()}
 else if(a=='copy')cp(b,j.clips[+b.dataset.k].caption);
 else if(a=='copyall')cp(b,allCaps(j));
 else if(a=='del'){if(!confirm('Usunąć to zadanie z listy? (pliki klipów zostają na dysku)'))return;await fetch('/api/jobs/'+encodeURIComponent(id),{method:'DELETE'});delete S[id];load()}
 else if(a=='cancel'){if(!confirm('Przerwać to zadanie?'))return;await fetch('/api/jobs/'+encodeURIComponent(id)+'/cancel',{method:'POST'});load()}
 else if(a=='rerun'){await fetch('/api/jobs/'+encodeURIComponent(id)+'/rerun',{method:'POST'});load()}
 else if(a=='folder')fetch('/api/open-folder?vid='+encodeURIComponent(j.video_id||''),{method:'POST'})});
const F=document.getElementById('f');try{const m=JSON.parse(localStorage.lastForm||'{}');if(m.n)F.n.value=m.n;if(m.lang)F.lang.value=m.lang}catch(e){}
F.onsubmit=async e=>{e.preventDefault();localStorage.lastForm=JSON.stringify({n:F.n.value,lang:F.lang.value});
 const r=await fetch('/api/jobs',{method:'POST',body:new FormData(F)});
 if(!r.ok){alert((await r.json()).detail||'Błąd');return}document.getElementById('url').value='';load()};
load();
