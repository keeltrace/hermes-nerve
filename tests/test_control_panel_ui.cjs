"use strict";
// Dependency-free VM smoke test of browser-side backend selection and result scope.
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");

const source = fs.readFileSync(path.join(__dirname, "..", "hermes_nerve", "web", "app.js"), "utf8");
let ready = null;
const scope = vm.createContext({
  location: {hash:"#token=example"},
  window: {addEventListener(name, fn) {if(name==="DOMContentLoaded")ready=fn;}},
  document: {
    getElementById(){return null;},
    querySelectorAll(){return [];},
  },
  console,
  setTimeout(){return 1;},
  clearTimeout(){},
});
vm.runInContext(source,scope);
vm.runInContext("refreshPreview = () => {}",scope);
vm.runInContext(
 "state.info = {profiles:[{id:'operator',name:'Operator'}],modules:[],catalog:{}," +
 "backend_options:['jev','d1','laya','openjev','systemone','shadow']," +
 "current:{saved_profile:'operator',advanced:{},overrides:{},profile:'operator'}," +
 "backups:[],installation:{detected:true,hermes_found:true,path:'/tmp'}};" +
 "state.resolved={reflex:true}; state.profile='operator';",
 scope
);
assert.equal(typeof ready,"function");
vm.runInContext("state.backend='d1';state.checking={verified:true,message:'D1 passed'};state.checkingKey=probeScope();",scope);
assert.match(vm.runInContext("connectPage()",scope),/REAL INFERENCE PASSED/);
vm.runInContext("selectBackend('jev')",scope);
assert.equal(vm.runInContext("state.checking",scope),null,"Changing backend must clear old decision evidence");
assert.doesNotMatch(vm.runInContext("connectPage()",scope),/D1 passed/);
assert.match(vm.runInContext("connectPage()",scope),/Run real decision test/);
vm.runInContext("state.backend='shadow';state.checking={verified:true,message:'paired'};state.checkingKey=probeScope();updateAdvanced('reflex_shadow_backend','laya');",scope);
assert.equal(vm.runInContext("state.checking",scope),null,"Comparator changes must clear evidence");
vm.runInContext("state.backend='d1';state.checking={verified:true,message:'D1 passed'};state.checkingKey=probeScope();updateAdvanced('reflex_d1_base_url','http://127.0.0.1:5001');",scope);
assert.equal(vm.runInContext("state.checking",scope),null,"URL edits must clear evidence");
const view=vm.runInContext("connectPage()",scope);
assert.match(view,/Review one-click setup/);
assert.match(view,/step-by-step local setup tutorial/);
assert.match(view,/Check service only/);
console.log("NERVE PANEL UI VM: PASS (stale results invalidated; real tests, setup, tutorial visible)");
