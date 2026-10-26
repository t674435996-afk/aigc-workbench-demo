const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const PORT = 9338;
const URL = "http://127.0.0.1:8080/";
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "wb_cdp_"));

function sleep(ms){ return new Promise(r=>setTimeout(r,ms)); }
async function getJson(u){ const r = await fetch(u); return r.json(); }

async function main(){
  const chrome = spawn(CHROME, [
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`,
    "--headless=new", "--no-first-run", "--no-default-browser-check",
    "--disable-gpu", "--window-size=1400,1000",
    URL
  ], { stdio: "ignore" });

  let target = null;
  for(let i=0;i<40;i++){
    try{
      const list = await getJson(`http://127.0.0.1:${PORT}/json/list`);
      target = list.find(t=>t.type === "page" && t.url.replace(/#.*$/,"").replace(/\/$/,"") === URL.replace(/\/$/,""));
      if(target) break;
    }catch(e){}
    await sleep(500);
  }
  if(!target){ console.log("FAIL: 拿不到页面 target"); chrome.kill(); process.exit(1); }

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res,rej)=>{ ws.onopen=res; ws.onerror=rej; });

  let idc = 0; const pend = new Map();
  const errors = [];
  ws.onmessage = (ev)=>{
    const m = JSON.parse(ev.data);
    if(m.id && pend.has(m.id)){ pend.get(m.id)(m); pend.delete(m.id); return; }
    if(m.method === "Runtime.exceptionThrown") errors.push("EXCEPTION: " + (m.params.exceptionDetails.text||""));
    if(m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
      errors.push("console.error: " + m.params.args.map(a=>a.value||a.description||"").join(" "));
    if(m.method === "Log.entryAdded" && m.params.entry.level === "error")
      errors.push("log.error: " + m.params.entry.text);
  };
  function send(method, params={}){
    const id = ++idc;
    return new Promise((res)=>{ pend.set(id,res); ws.send(JSON.stringify({id, method, params})); });
  }

  await send("Runtime.enable");
  await send("Log.enable");
  await send("Page.enable");
  await sleep(2500);

  const evalJs = `(async ()=>{
    const out = {};
    out.navItems = [...document.querySelectorAll("#nav a")].map(a=>a.dataset.v);
    out.hasQC = out.navItems.includes("qc");
    // 三个上传位
    out.slotModel = !!document.getElementById("slotModel");
    out.slotScene = !!document.getElementById("slotScene");
    out.slotProduct = !!document.getElementById("slotProduct");
    // 四个按钮
    out.btnExPerson = !!document.getElementById("btnExPerson");
    out.btnExScene = !!document.getElementById("btnExScene");
    out.btnExProduct = !!document.getElementById("btnExProduct");
    out.btnExAction = !!document.getElementById("btnExAction");
    out.btnExtractGone = !document.getElementById("btnExtract");
    // 尺寸下拉
    const gr = document.getElementById("genRatio");
    out.genRatio = !!gr;
    out.genRatioOptions = gr ? gr.options.length : 0;
    out.genRatioDefault = gr ? gr.value : "";
    // 项目素材标签
    const mats = document.querySelectorAll(".panel h3");
    out.materialTag = [...mats].find(h=>h.textContent.includes("项目素材")) ? [...mats].find(h=>h.textContent.includes("项目素材")).textContent : "（未找到）";
    // 动作默认按钮点击
    document.getElementById("btnExAction").click();
    out.actionFilled = document.getElementById("pAction").value.length > 0;
    // 点一个提取按钮（无图应回落内置规则，不报错）
    document.getElementById("btnExProduct").click();
    await new Promise(r=>setTimeout(r,1200));
    out.productFilled = document.getElementById("pProduct").value.length > 0;
    out.extractSrc = document.getElementById("extractSrc").textContent;
    return JSON.stringify(out);
  })()`;

  const res = await send("Runtime.evaluate", { expression: evalJs, awaitPromise: true, returnByValue: true });
  const val = res.result && res.result.result && res.result.result.value;
  console.log("=== 断言结果 ===");
  console.log(val);
  console.log("=== 控制台错误数 ===", errors.length);
  if(errors.length) console.log(errors.join("\n"));

  ws.close(); chrome.kill();
  fs.rmSync(profile, {recursive:true, force:true});
}
main().catch(e=>{ console.error(e); process.exit(1); });
