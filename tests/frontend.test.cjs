const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function context(location = {protocol:'http:',hostname:'localhost',origin:'http://localhost:8123'}) {
  const elements = new Map();
  const document = {addEventListener(){},getElementById(id) {
    if (!elements.has(id)) elements.set(id, {value:'python',style:{},innerHTML:''});
    return elements.get(id);
  }};
  const requireStub = () => {};
  requireStub.config = () => {};
  const ctx = vm.createContext({location,document,require:requireStub,window:{addEventListener(){}},
    setTimeout,clearTimeout,AbortController,console,alert:()=>{}});
  vm.runInContext(source,ctx);
  return ctx;
}

test('backend URLs follow the serving origin and protocol', () => {
  const local = context();
  assert.equal(vm.runInContext('API_BASE',local),'http://localhost:8123');
  assert.equal(vm.runInContext('WS_BASE',local),'ws://localhost:8123');
  const secure = context({protocol:'https:',hostname:'example.com',origin:'https://example.com'});
  assert.equal(vm.runInContext('WS_BASE',secure),'wss://example.com');
});

test('lint response for an old editor snapshot is ignored', async () => {
  const ctx = context();
  let resolve;
  ctx.fetch = () => new Promise(r => {resolve=r;});
  ctx.applied = false;
  vm.runInContext('editor={getValue:()=>"old"};setStatusLoading=()=>{};applyDiagnostics=()=>{applied=true}',ctx);
  const pending = vm.runInContext('runLint()',ctx);
  vm.runInContext('editor.getValue=()=>"new"',ctx);
  resolve({ok:true,json:async()=>({diagnostics:[]})});
  await pending;
  assert.equal(ctx.applied,false);
});

test('AI fix cannot replace newer user edits', () => {
  const ctx = context();
  vm.runInContext('editor={getValue:()=>"new",setValue:()=>{throw Error("overwritten")}};pendingAiFix="fixed";pendingAiSource={code:"old",lang:"python"};applyFix()',ctx);
});

test('HTML preview iframe has an opaque sandbox origin', () => {
  assert.match(html, /id="preview" sandbox="allow-scripts"/);
  assert.match(source, /frame\.setAttribute\('sandbox','allow-scripts'\)/);
});
