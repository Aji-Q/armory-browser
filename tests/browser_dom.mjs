import { extractVisiblePage } from '../extension/extract.mjs';
const rows=[];
const good='This is a real visible research article with substantial readable paragraphs and useful evidence. '.repeat(12);
const fixtures=[
 ['normal article',`<article><h1>Research fixture</h1><p>${good}</p></article>`,o=>!!o.result&&o.result.text.includes('real visible')&&!o.humanRequired],
 ['article with unrelated login modal',`<article><h1>Research fixture</h1><p>${good}</p></article><aside><form><input type="password" value="NEVER_CAPTURE_SECRET"><button>Sign in</button></form></aside>`,o=>!!o.result&&!o.humanRequired&&!o.result.text.includes('NEVER_CAPTURE_SECRET')],
 ['true login wall','<h1>Sign in to continue</h1><form><input type="password" value="NEVER_CAPTURE_SECRET"><button>Sign in</button></form>',o=>o.humanRequired&&!o.result],
 ['hidden and input values excluded',`<article><h1>Research fixture</h1><p>${good}</p><p style="display:none">HIDDEN_SECRET_MARKER</p><input value="INPUT_SECRET_MARKER"></article>`,o=>!!o.result&&!o.result.text.includes('HIDDEN_SECRET_MARKER')&&!o.result.text.includes('INPUT_SECRET_MARKER')]
];
for (const [name,html,check] of fixtures){
 document.body.innerHTML=html;
 await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
 try{const output=extractVisiblePage(location.origin,20000);rows.push({name,pass:check(output),humanRequired:output.humanRequired,chars:output.result?.text?.length||0});}
 catch(e){rows.push({name,pass:false,error:e.message});}
}
document.body.replaceChildren(); const h=document.createElement('h1');h.textContent=rows.every(r=>r.pass)?'PASS 4/4 — real DOM fixtures':'FAIL — DOM fixtures'; const p=document.createElement('pre');p.textContent=JSON.stringify(rows,null,2);document.body.append(h,p);document.title=h.textContent;
