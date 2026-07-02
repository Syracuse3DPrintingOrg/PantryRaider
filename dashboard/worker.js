/**
 * Pantry Raider – Change & Bead Dashboard
 * Cloudflare Worker
 *
 * Sources:
 *   - Beads: GitHub raw  .beads/issues.jsonl  (per selected branch)
 *   - Commits: GitHub REST API  /repos/:owner/:repo/commits
 *   - Changelog: GitHub raw  CHANGELOG.md
 */

const OWNER = "Syracuse3DPrintingOrg";
const REPO  = "PantryRaider";
const BRANCHES = ["main", "arch/modular", "ANG-Test", "ANGTEST2"];

// ── Utility ───────────────────────────────────────────────────────────────────

function ghHeaders(token) {
  const h = { "Accept": "application/vnd.github+json", "User-Agent": "fa-dashboard/1" };
  if (token) h["Authorization"] = `Bearer ${token}`;
  return h;
}

async function ghRaw(path, ref, token) {
  const url = `https://raw.githubusercontent.com/${OWNER}/${REPO}/${encodeURIComponent(ref)}/${path}`;
  const r = await fetch(url, { headers: ghHeaders(token) });
  if (!r.ok) return null;
  return r.text();
}

async function ghApi(path, token) {
  const url = `https://api.github.com/repos/${OWNER}/${REPO}${path}`;
  const r = await fetch(url, { headers: ghHeaders(token) });
  if (!r.ok) return null;
  return r.json();
}

function parseJsonl(text) {
  if (!text) return [];
  return text.trim().split("\n").map(l => {
    try { return JSON.parse(l); } catch { return null; }
  }).filter(Boolean);
}

// ── CHANGELOG parser ──────────────────────────────────────────────────────────

function parseChangelog(md) {
  if (!md) return [];
  const sections = [];
  let current = null;
  for (const line of md.split("\n")) {
    const versionMatch = line.match(/^## \[(.+?)\]/);
    if (versionMatch) {
      if (current) sections.push(current);
      current = { version: versionMatch[1], groups: {} };
    } else if (current) {
      const groupMatch = line.match(/^### (.+)/);
      if (groupMatch) {
        current._group = groupMatch[1];
        current.groups[current._group] = [];
      } else if (line.startsWith("- ") && current._group) {
        current.groups[current._group].push(line.slice(2));
      }
    }
  }
  if (current) sections.push(current);
  return sections;
}

// ── Category definitions ──────────────────────────────────────────────────────

const CATS = [
  { key: "installer",  label: "Installer & Setup",      emoji: "🛠" },
  { key: "hardware",   label: "Hardware & Pi",          emoji: "🖥" },
  { key: "arch",       label: "Architecture",           emoji: "🏗" },
  { key: "cloud",      label: "Cloud & Remote Access",  emoji: "☁" },
  { key: "ui",         label: "UI & UX",                emoji: "🎨" },
  { key: "inventory",  label: "Inventory & Grocy",      emoji: "📦" },
  { key: "recipes",    label: "Recipes & Shopping",     emoji: "🍽" },
  { key: "security",   label: "Security & Auth",        emoji: "🔒" },
  { key: "ci",         label: "Tests & CI",             emoji: "🧪" },
  { key: "docs",       label: "Docs",                   emoji: "📄" },
  { key: "bugs",       label: "Bug Fixes",              emoji: "🐛" },
  { key: "other",      label: "Other",                  emoji: "📌" },
];

// ── HTML page ─────────────────────────────────────────────────────────────────

function renderPage(beads, commits, changelog, branch) {

  // Escape </script> so injected JSON can't break the script tag
  const safe = s => JSON.stringify(s).replace(/<\/script>/gi, '<\/script>');
  const beadsJson     = safe(beads);
  const commitsJson   = safe(commits);
  const changelogJson = safe(changelog);
  const catsJson      = safe(CATS);
  const branchesJson  = safe(BRANCHES);

  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Pantry Raider Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet"/>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0a0c10;--surface:#111420;--surface2:#191d2d;
  --border:#1f2437;--border2:#2a3050;
  --text:#e2e8f0;--muted:#8892a4;
  --accent:#6c63ff;--accent2:#8b5cf6;
  --green:#22c55e;--amber:#f59e0b;--red:#ef4444;--blue:#3b82f6;--cyan:#06b6d4;--pink:#ec4899;
  --r:10px;
}
html{scroll-behavior:smooth}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;line-height:1.6}
a{color:var(--accent);text-decoration:none}a:hover{color:var(--accent2)}
code{font-family:'JetBrains Mono',monospace}

/* header */
.hdr{position:sticky;top:0;z-index:100;background:rgba(10,12,16,.88);backdrop-filter:blur(16px);
  border-bottom:1px solid var(--border);padding:0 1.5rem;display:flex;align-items:center;gap:1rem;height:60px}
.logo{display:flex;align-items:center;gap:.6rem;font-weight:700;font-size:.95rem;letter-spacing:-.02em}
.logo-blob{width:28px;height:28px;border-radius:7px;background:linear-gradient(135deg,var(--accent),var(--pink));
  display:grid;place-items:center;font-size:.9rem;flex-shrink:0}
.hdr-right{margin-left:auto;display:flex;align-items:center;gap:.75rem}
.branch-sel{background:var(--surface2);border:1px solid var(--border2);color:var(--text);
  padding:.35rem .75rem;border-radius:6px;font-size:.8rem;font-family:inherit;cursor:pointer}
.branch-sel:focus{outline:none;border-color:var(--accent)}
.refresh-btn{background:var(--surface2);border:1px solid var(--border2);color:var(--muted);
  padding:.35rem .65rem;border-radius:6px;cursor:pointer;font-size:.85rem;transition:all .15s}
.refresh-btn:hover{border-color:var(--accent);color:var(--accent)}
.ts{font-size:.72rem;color:var(--muted)}

/* tabs */
.tabs{display:flex;gap:.25rem;padding:.7rem 1.5rem;border-bottom:1px solid var(--border)}
.tab{padding:.45rem 1rem;border-radius:7px;font-size:.84rem;font-weight:500;cursor:pointer;
  transition:all .15s;border:1px solid transparent;color:var(--muted)}
.tab:hover{background:var(--surface);color:var(--text);border-color:var(--border)}
.tab.on{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;
  border-color:transparent;box-shadow:0 2px 14px rgba(108,99,255,.35)}

.content{max-width:1160px;margin:0 auto;padding:1.5rem}
.panel{display:none}.panel.on{display:block}

/* stats */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:1rem;margin-bottom:1.5rem}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);
  padding:.9rem 1.1rem;position:relative;overflow:hidden}
.stat::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:var(--sc,var(--accent))}
.stat-v{font-size:1.75rem;font-weight:700;line-height:1;margin-bottom:.15rem}
.stat-l{font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}

/* toolbar */
.toolbar{display:flex;align-items:center;gap:.75rem;margin-bottom:1rem;flex-wrap:wrap}
.search{flex:1;min-width:180px;max-width:360px;background:var(--surface);border:1px solid var(--border2);
  border-radius:8px;padding:.5rem .9rem;font-size:.84rem;font-family:inherit;color:var(--text)}
.search::placeholder{color:var(--muted)}.search:focus{outline:none;border-color:var(--accent)}
.tog-grp{display:flex;border:1px solid var(--border2);border-radius:8px;overflow:hidden}
.tog{background:var(--surface);padding:.4rem .8rem;font-size:.78rem;font-weight:500;cursor:pointer;
  border:none;color:var(--muted);font-family:inherit;transition:all .15s}
.tog.on{background:var(--accent);color:#fff}
.tog:not(:last-child){border-right:1px solid var(--border2)}

/* category section */
.cat-sec{margin-bottom:1.5rem}
.cat-hdr{display:flex;align-items:center;gap:.5rem;margin-bottom:.6rem;font-size:.82rem;
  font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);cursor:pointer;user-select:none}
.cat-hdr:hover{color:var(--text)}
.cat-n{background:var(--surface2);border:1px solid var(--border);padding:.1rem .45rem;
  border-radius:999px;font-size:.72rem;margin-left:auto}
.cat-chev{font-size:.65rem;transition:transform .2s}
.cat-chev.col{transform:rotate(-90deg)}
.bead-col{display:flex;flex-direction:column;gap:.45rem}

/* bead card */
.bc{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);
  padding:.8rem .95rem;display:flex;align-items:flex-start;gap:.8rem;
  cursor:pointer;transition:all .15s;position:relative;overflow:hidden}
.bc:hover{border-color:var(--border2);background:var(--surface2);transform:translateX(2px)}
.bc.exp{border-color:var(--accent);background:var(--surface2)}
.bc::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;
  background:var(--bc,var(--border));transition:background .15s}
.bc:hover::before,.bc.exp::before{background:var(--bc,var(--accent))}
.bi{width:30px;height:30px;border-radius:6px;background:var(--surface2);border:1px solid var(--border);
  display:grid;place-items:center;font-size:.8rem;flex-shrink:0}
.bm{flex:1;min-width:0}
.bt{font-size:.86rem;font-weight:500;line-height:1.35;margin-bottom:.28rem}
.bmeta{display:flex;gap:.4rem;flex-wrap:wrap;align-items:center}
.bid{font-size:.7rem;font-family:'JetBrains Mono',monospace;color:var(--muted)}
.bdet{display:none;margin-top:.75rem;padding-top:.75rem;border-top:1px solid var(--border);
  font-size:.81rem;color:var(--muted);line-height:1.6}
.bc.exp .bdet{display:block}
.bdg{display:grid;grid-template-columns:auto 1fr;gap:.28rem .75rem;margin-top:.45rem}
.bdg dt{color:var(--muted);font-size:.73rem;white-space:nowrap}.bdg dd{font-size:.79rem;word-break:break-word}
.bdesc{background:var(--bg);border:1px solid var(--border);border-radius:6px;
  padding:.6rem .8rem;margin-top:.45rem;font-size:.79rem;line-height:1.7}

/* badges */
.bdg2{display:inline-flex;align-items:center;padding:.12rem .5rem;border-radius:999px;
  font-size:.7rem;font-weight:500;border:1px solid transparent}
.s-open{background:rgba(34,197,94,.12);color:var(--green);border-color:rgba(34,197,94,.25)}
.s-closed{background:rgba(148,163,184,.08);color:var(--muted);border-color:rgba(148,163,184,.15)}
.s-in_progress{background:rgba(245,158,11,.12);color:var(--amber);border-color:rgba(245,158,11,.25)}
.t-epic{background:rgba(139,92,246,.12);color:var(--accent2);border-color:rgba(139,92,246,.25)}
.t-feature{background:rgba(6,182,212,.12);color:var(--cyan);border-color:rgba(6,182,212,.25)}
.t-bug{background:rgba(239,68,68,.12);color:var(--red);border-color:rgba(239,68,68,.25)}
.t-task{background:rgba(59,130,246,.12);color:var(--blue);border-color:rgba(59,130,246,.25)}
.p-dep{background:rgba(59,130,246,.1);color:var(--blue)}

/* changelog */
.cl{display:flex;flex-direction:column;gap:.9rem}
.cl-v{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);overflow:hidden}
.cl-hdr{display:flex;align-items:center;gap:.7rem;padding:.9rem 1.15rem;
  cursor:pointer;transition:background .15s;user-select:none}
.cl-hdr:hover{background:var(--surface2)}
.cl-ver{font-family:'JetBrains Mono',monospace;font-size:.85rem;font-weight:600;
  background:linear-gradient(135deg,var(--accent),var(--pink));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.cl-unrel{color:var(--amber);font-style:italic}
.cl-chev{font-size:.75rem;margin-left:auto;transition:transform .2s;color:var(--muted)}
.cl-chev.op{transform:rotate(90deg)}
.cl-body{display:none;padding:0 1.15rem 1.1rem;border-top:1px solid var(--border)}
.cl-body.op{display:block}
.cl-grp{margin-top:.8rem}
.cl-glb{font-size:.7rem;font-weight:600;text-transform:uppercase;letter-spacing:.08em;
  color:var(--muted);margin-bottom:.4rem}
.cl-items{display:flex;flex-direction:column;gap:.35rem}
.cl-item{font-size:.83rem;padding:.4rem .75rem;background:var(--bg);
  border-left:2px solid var(--border2);border-radius:0 5px 5px 0;line-height:1.55}
.cl-item b{color:var(--text)}

/* commits */
.cms{display:flex;flex-direction:column;gap:.45rem}
.cm{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);
  padding:.7rem .95rem;display:flex;gap:.8rem;align-items:flex-start;transition:border-color .15s}
.cm:hover{border-color:var(--border2)}
.cm-hash{font-family:'JetBrains Mono',monospace;font-size:.73rem;font-weight:500;
  color:var(--accent);flex-shrink:0;padding:.18rem .42rem;
  background:rgba(108,99,255,.1);border-radius:4px;border:1px solid rgba(108,99,255,.2);
  letter-spacing:.03em;margin-top:.08rem}
.cm-main{flex:1;min-width:0}
.cm-msg{font-size:.86rem;font-weight:500;line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cm-meta{display:flex;gap:.6rem;margin-top:.28rem;font-size:.72rem;color:var(--muted);flex-wrap:wrap}

/* misc */
.empty{text-align:center;padding:3rem 1.5rem;color:var(--muted)}
.empty-i{font-size:2.5rem;margin-bottom:.65rem}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--accent)}
@media(max-width:640px){
  .stats{grid-template-columns:1fr 1fr}
  .tabs{overflow-x:auto;padding:.6rem 1rem}
  .tab{white-space:nowrap}
  .content{padding:1rem}
  .hdr{padding:0 1rem}
}
</style>
</head>
<body>

<header class="hdr">
  <div class="logo">
    <div class="logo-blob">🥗</div>
    <span>Pantry Raider</span>
    <span style="color:var(--muted);font-weight:400">Dashboard</span>
  </div>
  <div class="hdr-right">
    <select class="branch-sel" id="bsel" onchange="goBranch(this.value)">
      ${BRANCHES.map(b => `<option value="${b}"${b === branch ? " selected" : ""}>${b}</option>`).join("")}
    </select>
    <span class="ts" id="ts"></span>
    <button class="refresh-btn" onclick="location.reload()">↺ Refresh</button>
  </div>
</header>

<nav class="tabs">
  <div class="tab on" data-tab="beads" onclick="goTab('beads')">🔮 Beads</div>
  <div class="tab" data-tab="changelog" onclick="goTab('changelog')">📋 Changelog</div>
  <div class="tab" data-tab="commits" onclick="goTab('commits')">🔀 Commits</div>
</nav>

<div class="content">
  <div class="panel on" id="p-beads">
    <div class="stats" id="stats"></div>
    <div class="toolbar">
      <input class="search" id="srch" type="search" placeholder="Search by title, ID, or description…" oninput="filt()"/>
      <div class="tog-grp">
        <button class="tog on" id="t-open"   onclick="setSF('open')">Open</button>
        <button class="tog"    id="t-closed" onclick="setSF('closed')">Closed</button>
        <button class="tog"    id="t-all"    onclick="setSF('all')">All</button>
      </div>
    </div>
    <div id="bc"></div>
  </div>
  <div class="panel" id="p-changelog">
    <div class="cl" id="cc"></div>
  </div>
  <div class="panel" id="p-commits">
    <div class="cms" id="cmc"></div>
  </div>
</div>

<script>
const BEADS   = ${beadsJson};
const COMMITS = ${commitsJson};
const CL      = ${changelogJson};
const BRANCH  = ${JSON.stringify(branch)};
const CATS    = ${catsJson};
let SF = "open";

/* ── Tab / branch ─────────────────────────────── */
function goTab(t){
  document.querySelectorAll(".tab").forEach(el=>el.classList.toggle("on",el.dataset.tab===t));
  document.querySelectorAll(".panel").forEach(el=>el.classList.toggle("on",el.id==="p-"+t));
}
function goBranch(b){const u=new URL(location.href);u.searchParams.set("branch",b);location.href=u;}

/* ── Helpers ──────────────────────────────────── */
function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}
function fmt(d){return d?new Date(d).toLocaleDateString("en-US",{month:"short",day:"numeric",year:"numeric"}):""}

/* ── Categorise ───────────────────────────────── */
function cat(b){
  const h=(b.title+" "+(b.description||"")).toLowerCase();
  if(/install|wizard|bootstrap|firstboot|setup\.sh/.test(h)) return "installer";
  if(/pi_remote|pi_hosted|kiosk|stream.?deck|display|rotation|wifi|hostapd|ap.mode/.test(h)) return "hardware";
  if(/modular|satellite|arch|deployment.mode|pi.remote.headless/.test(h)) return "arch";
  if(/cloud|tunnel|cloudflare|remote.access|pangolin/.test(h)) return "cloud";
  if(/\bui\b|\bux\b|theme|css|interface|dark|pwa|manifest|navbar/.test(h)) return "ui";
  if(/grocy|inventory|barcode|stock|expir/.test(h)) return "inventory";
  if(/recipe|mealie|shopping|meal|cook|themealdb|spoonacular/.test(h)) return "recipes";
  if(/auth|security|2fa|totp|api.?key|secret|password/.test(h)) return "security";
  if(/test|ci|lint|github.action|pytest/.test(h)) return "ci";
  if(/doc|readme|changelog|screenshot/.test(h)) return "docs";
  if(b.issue_type==="bug") return "bugs";
  return "other";
}

/* ── Stats ────────────────────────────────────── */
function renderStats(){
  const open=BEADS.filter(b=>b.status==="open").length;
  const ip=BEADS.filter(b=>b.status==="in_progress").length;
  const cl=BEADS.filter(b=>b.status==="closed").length;
  const ep=BEADS.filter(b=>b.issue_type==="epic").length;
  document.getElementById("stats").innerHTML=[
    {v:open,l:"Open Beads",c:"var(--green)"},
    {v:ip,  l:"In Progress",c:"var(--amber)"},
    {v:cl,  l:"Closed",c:"var(--muted)"},
    {v:ep,  l:"Epics",c:"var(--accent2)"},
  ].map(s=>'<div class="stat" style="--sc:'+s.c+'"><div class="stat-v">'+s.v+'</div><div class="stat-l">'+s.l+'</div></div>').join("");
}

/* ── Beads ────────────────────────────────────── */
const TICON={epic:"⚡",feature:"✨",task:"📋",bug:"🐛",chore:"🔧"};
const SCOL={open:"var(--green)",closed:"var(--muted)",in_progress:"var(--amber)"};
const PRIO=["🔥 Critical","🔴 High","🟡 Medium","🟢 Low"];

function setSF(f){
  SF=f;
  document.querySelectorAll(".tog").forEach(b=>b.classList.remove("on"));
  document.getElementById("t-"+f).classList.add("on");
  filt();
}

function filt(){
  const q=(document.getElementById("srch").value||"").toLowerCase().trim();
  let bs=BEADS;
  if(SF!=="all") bs=bs.filter(b=>b.status===SF);
  if(q) bs=bs.filter(b=>b.title.toLowerCase().includes(q)||b.id.toLowerCase().includes(q)||(b.description||"").toLowerCase().includes(q));
  renderBeads(bs);
}

function renderBeads(bs){
  const grp={};
  for(const b of bs){const k=cat(b);if(!grp[k])grp[k]=[];grp[k].push(b);}
  let h="";
  for(const c of CATS){
    if(!grp[c.key]?.length) continue;
    h+='<div class="cat-sec"><div class="cat-hdr" onclick="togCat(this)"><span>'+c.emoji+" "+c.label+'</span><span class="cat-n">'+grp[c.key].length+'</span><span class="cat-chev">▼</span></div><div class="bead-col">'+grp[c.key].map(renderBead).join("")+'</div></div>';
  }
  document.getElementById("bc").innerHTML=h||'<div class="empty"><div class="empty-i">🔍</div><div>No beads match.</div></div>';
}

function togCat(el){
  const g=el.nextElementSibling;const ch=el.querySelector(".cat-chev");
  g.style.display=g.style.display==="none"?"":"none";ch.classList.toggle("col");
}

function renderBead(b){
  const icon=TICON[b.issue_type]||"📌";
  const col=SCOL[b.status]||"var(--border)";
  const prio=b.priority!==undefined?PRIO[Math.min(b.priority,3)]:"";
  const parent=b.parent?'<span class="bid">⤷ '+b.parent+'</span>':"";
  const deps=b.dependency_count>0?'<span class="bdg2 p-dep">↔ '+b.dependency_count+' dep</span>':"";
  const reason=b.close_reason?'<div style="margin-top:.4rem;color:var(--green);font-size:.77rem">✓ '+esc(b.close_reason)+'</div>':"";
  return '<div class="bc" style="--bc:'+col+'" onclick="this.classList.toggle(&quot;exp&quot;)">'
    +'<div class="bi">'+icon+'</div>'
    +'<div class="bm">'
      +'<div class="bt">'+esc(b.title)+'</div>'
      +'<div class="bmeta">'
        +'<span class="bid">'+b.id+'</span>'
        +'<span class="bdg2 s-'+b.status+'">'+b.status.replace("_"," ")+'</span>'
        +'<span class="bdg2 t-'+b.issue_type+'">'+b.issue_type+'</span>'
        +(prio?'<span class="bdg2">'+prio+'</span>':"")
        +deps+parent
      +'</div>'
      +'<div class="bdet">'
        +(b.description?'<div class="bdesc">'+esc(b.description)+'</div>':"")
        +'<dl class="bdg">'
          +(b.created_at?'<dt>Created</dt><dd>'+fmt(b.created_at)+'</dd>':"")
          +(b.closed_at?'<dt>Closed</dt><dd>'+fmt(b.closed_at)+'</dd>':"")
          +((b.assignee||b.owner)?'<dt>Owner</dt><dd>'+esc(b.assignee||b.owner)+'</dd>':"")
        +'</dl>'
        +reason
      +'</div>'
    +'</div>'
  +'</div>';
}

/* ── Changelog ────────────────────────────────── */
const GCOL={Added:"var(--green)",Changed:"var(--amber)",Fixed:"var(--blue)",Removed:"var(--red)"};

function renderCL(){
  const c=document.getElementById("cc");
  if(!CL.length){c.innerHTML='<div class="empty"><div class="empty-i">📋</div><div>No changelog.</div></div>';return;}
  c.innerHTML=CL.map((v,i)=>{
    const unr=v.version==="Unreleased";
    const vclass=unr?"cl-unrel":"cl-ver";
    const grps=Object.entries(v.groups).map(([name,items])=>{
      const col=GCOL[name]||"var(--muted)";
      return '<div class="cl-grp"><div class="cl-glb" style="color:'+col+'">'+name+'</div><div class="cl-items">'+items.map(it=>'<div class="cl-item">'+it.replace(/\*\*(.+?)\*\*/g,"<b>$1</b>")+'</div>').join("")+'</div></div>';
    }).join("");
    const op=i===0?" op":"";
    return '<div class="cl-v"><div class="cl-hdr" onclick="togCL(this)"><span class="'+vclass+'">'+v.version+'</span>'+(unr?'<span class="bdg2 s-in_progress">Unreleased</span>':"")+'<span class="cl-chev'+op+'">▶</span></div><div class="cl-body'+op+'">'+grps+'</div></div>';
  }).join("");
}

function togCL(el){const b=el.nextElementSibling;const ch=el.querySelector(".cl-chev");b.classList.toggle("op");ch.classList.toggle("op");}

/* ── Commits ──────────────────────────────────── */
function renderCommits(){
  const c=document.getElementById("cmc");
  if(!COMMITS.length){c.innerHTML='<div class="empty"><div class="empty-i">🔀</div><div>No commits.</div></div>';return;}
  c.innerHTML=COMMITS.map(cm=>{
    const h=cm.sha?cm.sha.slice(0,7):"?";
    const msg=cm.commit?.message?.split("\\n")[0]||"—";
    const date=fmt(cm.commit?.author?.date);
    const author=cm.commit?.author?.name||cm.author?.login||"Unknown";
    const url=cm.html_url||"https://github.com/${OWNER}/${REPO}/commit/"+cm.sha;
    return '<div class="cm"><a class="cm-hash" href="'+url+'" target="_blank" rel="noopener">'+h+'</a><div class="cm-main"><div class="cm-msg">'+esc(msg)+'</div><div class="cm-meta"><span>🕐 '+date+'</span><span>👤 '+esc(author)+'</span></div></div></div>';
  }).join("");
}

/* ── Boot ─────────────────────────────────────── */
renderStats();
filt();
renderCL();
renderCommits();
document.getElementById("ts").textContent="Branch: "+BRANCH+" \u00b7 "+new Date().toLocaleTimeString();
</script>
</body>
</html>`;
}

// ── Worker entry ──────────────────────────────────────────────────────────────

export default {
  async fetch(request, env) {
    const url    = new URL(request.url);
    const branch = url.searchParams.get("branch") || "main";
    const token  = env.GITHUB_TOKEN;

    const [issuesRaw, changelogRaw, commits] = await Promise.all([
      ghRaw(".beads/issues.jsonl", branch, token),
      ghRaw("CHANGELOG.md",        branch, token),
      ghApi(`/commits?sha=${encodeURIComponent(branch)}&per_page=50`, token),
    ]);

    const beads     = parseJsonl(issuesRaw);
    const changelog = parseChangelog(changelogRaw);
    const safeCommits = Array.isArray(commits) ? commits : [];

    const page = renderPage(beads, safeCommits, changelog, branch);

    return new Response(page, {
      headers: {
        "Content-Type": "text/html;charset=UTF-8",
        "Cache-Control": "s-maxage=60, stale-while-revalidate=120",
        "X-Branch": branch,
      },
    });
  },
};
