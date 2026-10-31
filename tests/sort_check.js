const fs=require('fs'), vm=require('vm');
function cls(){const s=new Set();return{add:(...a)=>a.forEach(x=>s.add(x)),remove:(...a)=>a.forEach(x=>s.delete(x)),contains:x=>s.has(x),_s:s};}
function cell(tag,text,attrs={}){return{tagName:tag,innerText:text,colSpan:1,attrs,getAttribute(n){return n in this.attrs?this.attrs[n]:null},classList:cls(),querySelector:()=>null,addEventListener(e,f){this.h=f}};}
function table(header,rows,dataAttrs){const body={rows:[],appendChild(r){this.rows=this.rows.filter(x=>x!==r);this.rows.push(r);}};
  body.rows.push({cells:header.map(h=>cell('TH',h))});
  rows.forEach(r=>body.rows.push({cells:r.map(v=>Array.isArray(v)?cell('TD',v[0],{'data-sort':v[1]}):cell('TD',v))}));
  return{tBodies:[body],classList:cls(),hasAttribute:()=>false,body};}
const t=table(['Symbol','Qty','Price','P&L %','Bought','Target','Held'],[
 ['TCS\nTata Consultancy','85','Rs 2,575.10\n10:15','-5.98%','2026-10-23 10:15:38',['T1 2809.4\nT2 x','2809.4'],'4.9 d'],
 ['CYIENT\nCyient','131','1,249.00','-7.35%','2026-10-23 10:45:28',['T1 1401\nT2','1401.36'],'4.94 d'],
 ['ACUTAAS\nx','7','3,490.20','+10.62%','2026-10-27 11:16:01',['T1 3868\nT2','3868.3'],'-'],
 ['WIPRO\nw','129','189.20','n/a','2026-10-21 10:15:47',['T1 204\nT2','204.69'],'0.13 d'],
 ['IGIL\ni','1000','363.55','9.53%','2026-10-23 11:00:31',['T1 396\nT2','396.6'],'3.94 d']]);
const doc={addEventListener:(e,f)=>f(),querySelectorAll:()=>[t]};
vm.runInNewContext((function(){var b=fs.readFileSync(__dirname+'/../templates/base.html','utf8');var m=b.match(/<script>\n\/\* Click a column header[\s\S]*?<\/script>/)[0];return m.slice(8,-9);})(),{document:doc,Array,parseFloat,Date,String,Set});
function click(i){t.tBodies[0].rows[0].cells[i].h();return t.tBodies[0].rows.slice(1).map(r=>r.cells[0].innerText.split('\n')[0]);}
const out=(n,i)=>console.log(n.padEnd(16),click(i).join(' '));
out('Symbol asc',0);out('Symbol desc',0);
out('Qty asc',1);out('Qty desc',1);
out('Price asc',2);out('Price desc',2);
out('P&L% asc',3);out('P&L% desc',3);
out('Bought asc',4);out('Bought desc',4);
out('Target asc',5);
out('Held asc',6);out('Held desc',6);
