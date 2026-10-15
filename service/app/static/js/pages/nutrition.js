// Nutrition page (templates/nutrition.html).
function val(id){const v=document.getElementById(id).value;return v===''?null:parseFloat(v);}
function fmt(n){return n==null?'-':(Math.round(n*10)/10);}

async function loadToday(){
  let d={entries:[],totals:{}};
  try{ d=await fetch('nutrition/today',{cache:'no-store'}).then(r=>r.json()); }catch(e){}
  document.getElementById('nt-date').textContent = d.date||'';
  const t=d.totals||{};
  document.getElementById('tt-cal').textContent=fmt(t.calories)||0;
  document.getElementById('tt-pro').textContent=fmt(t.protein)||0;
  document.getElementById('tt-carb').textContent=fmt(t.carbs)||0;
  document.getElementById('tt-fat').textContent=fmt(t.fat)||0;
  const list=document.getElementById('nt-entries'); list.innerHTML='';
  const entries=d.entries||[];
  document.getElementById('nt-empty').classList.toggle('d-none', entries.length>0);
  entries.forEach(e=>{
    const row=document.createElement('div');
    row.className='card';
    row.innerHTML=`<div class="card-body py-2 px-3 d-flex justify-content-between align-items-center flex-wrap gap-2">
      <div><span class="fw-semibold">${PR.esc(e.name)}</span>
        <span class="small text-secondary">x${PR.esc(e.servings)} · ${fmt(e.calories)} cal · P ${fmt(e.protein)} / C ${fmt(e.carbs)} / F ${fmt(e.fat)}</span></div>
      <button class="btn btn-sm btn-outline-danger" title="Remove"><i class="bi bi-trash"></i></button></div>`;
    row.querySelector('button').onclick=()=>delEntry(e.id);
    list.appendChild(row);
  });
}

async function estimateMacros(btn){
  const name=document.getElementById('nt-name').value.trim();
  if(!name){document.getElementById('nt-msg').innerHTML='<span class="text-warning">Enter a food first.</span>';return;}
  const orig=btn.innerHTML; btn.disabled=true; btn.innerHTML='<span class="spinner-border spinner-border-sm"></span>';
  try{
    const r=await fetch('nutrition/estimate',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name,servings:val('nt-serv')||1})}).then(x=>x.json());
    if(!r.ok){document.getElementById('nt-msg').innerHTML='<span class="text-secondary">'+PR.esc(r.error||'No estimate')+'</span>';}
    else{const e=r.estimate||{};
      if(e.calories!=null)document.getElementById('nt-cal').value=e.calories;
      if(e.protein!=null)document.getElementById('nt-pro').value=e.protein;
      if(e.carbs!=null)document.getElementById('nt-carb').value=e.carbs;
      if(e.fat!=null)document.getElementById('nt-fat').value=e.fat;
      document.getElementById('nt-msg').innerHTML='<span class="text-success">Estimated. Edit if needed, then add.</span>';}
  }catch(e){document.getElementById('nt-msg').innerHTML='<span class="text-danger">Could not reach the estimator.</span>';}
  btn.disabled=false; btn.innerHTML=orig;
}

async function logFood(btn){
  const name=document.getElementById('nt-name').value.trim();
  if(!name){document.getElementById('nt-msg').innerHTML='<span class="text-warning">Enter a food.</span>';return;}
  const body={name,servings:val('nt-serv')||1,calories:val('nt-cal'),protein:val('nt-pro'),carbs:val('nt-carb'),fat:val('nt-fat')};
  try{
    await fetch('nutrition/log',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  }catch(e){document.getElementById('nt-msg').innerHTML='<span class="text-danger">Could not log.</span>';return;}
  ['nt-name','nt-cal','nt-pro','nt-carb','nt-fat'].forEach(i=>document.getElementById(i).value='');
  document.getElementById('nt-serv').value='1';
  document.getElementById('nt-msg').textContent='';
  loadToday();
}

async function delEntry(id){
  try{ await fetch('nutrition/'+id,{method:'DELETE'}); }catch(e){}
  loadToday();
}

loadToday();
