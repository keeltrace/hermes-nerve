"use strict";
/* Nerve Operator University learner app (CSP: external asset) */
const $=s=>document.querySelector(s);
let manifest=null,current=null;
// Session token arrives in the URL fragment (#token=...), the same contract as
// the Control Panel: fragments are never sent to the server and never logged.
const tokenMatch=location.hash.match(/^#token=([A-Za-z0-9_-]+)$/);
const TOKEN=tokenMatch?tokenMatch[1]:"";
async function api(path,opts){
  const o=Object.assign({},opts||{});
  o.headers=Object.assign({"X-Nerve-Token":TOKEN},(o.headers||{}));
  return fetch(path,o);
}
async function apiGet(p){const r=await api(p);if(!r.ok)throw new Error("api "+p);return r.json()}
async function apiPost(p,body){
  const r=await api(p,{method:"POST",headers:{"Content-Type":"application/json"},
                       body:JSON.stringify(body||{})});
  if(!r.ok){let m="";try{m=(await r.json()).error||""}catch(e){}
    throw new Error(m||("HTTP "+r.status));}
  return r.json();
}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function noSession(){
  $("#content").innerHTML='<h1>Session link missing.</h1><p>Open the University from the Nerve Control Panel '+
   'sidebar (the link carries your session token in the fragment) or relaunch <code>nerve ui</code>. '+
   'Tokens are per-launch and never stored.</p>';
  $("#modules").innerHTML="";
}
async function loadManifest(){
  try{manifest=await apiGet("/api/university/manifest");}
  catch(e){noSession();return;}
  const nav=$("#modules");nav.innerHTML="";
  manifest.modules.forEach(m=>{const b=document.createElement("button");
    b.textContent=m.id+". "+m.title;b.onclick=()=>show(m.id);b.id="btn-"+m.id;nav.appendChild(b);});
  await show(manifest.modules[0].id);
}
async function show(id){
  if(!manifest){noSession();return;}
  current=manifest.modules.find(m=>m.id===id);
  let mod,st;
  try{
    mod=await apiGet("/api/university/module?id="+encodeURIComponent(id));
    st=await apiGet("/api/university/status?profile="+encodeURIComponent($("#profile").value));
  }catch(e){noSession();return;}
  const ps=(st.modules||{})[id]||{};
  let html=`<h2>${id}. ${esc(mod.title)}</h2><p>${esc(mod.summary)}</p>`;
  mod.lessons.forEach(l=>{html+=`<div class="lesson"><h3>${esc(l.title)}</h3><p>${esc(l.body)}</p>
    <button onclick="mark('${id}',this)" ${ps.lessons_read&&ps.lessons_read.includes(l.title)?"disabled":""}>${ps.lessons_read&&ps.lessons_read.includes(l.title)?"✓ read":"mark read"}</button></div>`;});
  html+=`<div class="lab"><h3>Lab (disposable fixture)</h3><p>${esc(mod.lab.goal)}</p><pre>${esc(mod.lab.steps.join("\n"))}</pre>`;
  if(mod.lab.executable){
    html+=`<p><b>Executable lab:</b> ${esc(mod.lab.executable.task)}</p>`;
    Object.keys(mod.lab.executable.files||{}).forEach(fn=>{
      html+=`<p><label>${esc(fn)}<br><textarea class="labfile" data-fn="${esc(fn)}"></textarea></label></p>`;});
  }
  ((mod.lab.task&&mod.lab.task.questions)||[]).forEach(q=>{
    html+=`<p><label>${esc(q.q)}<br><input class="labans" data-qid="${esc(q.id)}"></label></p>`;});
  html+=`<button onclick="lab('${id}')">Submit lab answers</button> <span id="labout" role="status"></span></div>`;
  html+=`<div class="lesson"><h3>Knowledge check</h3><p>${esc(mod.check.question)}</p>
    <textarea id="answer" aria-label="Answer"></textarea><br>
    <button onclick="check('${id}')">Check</button> <span id="cout" role="status"></span>
    ${ps.missed&&ps.missed.length?`<p class="bad">Missing concepts: ${ps.missed.map(esc).join(", ")}</p>`:""}
    <p><b>Troubleshooting:</b> ${mod.troubleshooting.map(esc).join(" · ")}</p></div>`;
  $("#content").innerHTML=html;
  [...nav.children].forEach(b=>b.classList.toggle("active",b.id==="btn-"+id));
  refreshStatus();
}
async function mark(id,btn){
  const l=btn.closest(".lesson").querySelector("h3").textContent;
  try{await apiPost("/api/university/lesson",{profile:$("#profile").value,module:id,lesson:l});
      btn.textContent="✓ read";btn.disabled=true;}catch(e){$("#status").textContent=e.message;$("#status").className="bad";}
  refreshStatus();
}
async function lab(id){
  const submission={};
  document.querySelectorAll(".labans").forEach(inp=>{submission[inp.dataset.qid]=inp.value;});
  const files={};
  document.querySelectorAll(".labfile").forEach(t=>{files[t.dataset.fn]=t.value;});
  if(Object.keys(files).length){submission.files=files;}
  try{const e=await apiPost("/api/university/lab",{module:id,profile:$("#profile").value,submission});
      const failed=(e.checks||[]).filter(c=>!c.passed);
      $("#labout").textContent=e.passed?"lab passed in disposable fixture ✓":
        "not yet: "+failed.map(c=>c.id).join(", ");
      $("#labout").className=e.passed?"ok":"bad";}
  catch(err){$("#labout").textContent=err.message;$("#labout").className="bad";}
  refreshStatus();
}
async function check(id){
  const a=$("#answer").value;
  try{const r=await apiPost("/api/university/check",{profile:$("#profile").value,module:id,answer:a});
      $("#cout").textContent=r.passed?"✓ passed":("✗ review: "+(r.missed||[]).join(", "));
      $("#cout").className=r.passed?"ok":"bad";}
  catch(err){$("#cout").textContent=err.message;$("#cout").className="bad";}
  refreshStatus();
}
async function refreshStatus(){
  try{const st=await apiGet("/api/university/status?profile="+encodeURIComponent($("#profile").value));
      const done=Object.values(st.modules||{}).filter(m=>m.complete).length;
      $("#status").textContent=`${done}/9 modules complete`+(st.capstone&&st.capstone.passed?" · capstone ✓":"");}
  catch(e){$("#status").textContent="";}
}
if(!TOKEN){noSession();}else{loadManifest();}
$("#profile").addEventListener("change",()=>current&&show(current.id));
