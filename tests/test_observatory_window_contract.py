"""Observation Window: ship real evidence, preserve an operator's live review."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from hermes_nerve.observatory import ui


class ObservationWindowContracts(unittest.TestCase):
    def test_refresh_captures_state_before_erasing_factory_dom(self):
        js = ui.APP_JS
        capture = js.index("window.NerveFactory.captureUIState(main)")
        remove = js.index('main.textContent = "";', capture)
        render = js.index("window.NerveFactory.render(main,", remove)
        self.assertLess(capture, remove)
        self.assertLess(remove, render)
        self.assertIn("restore:factoryRestore", js)

    def test_history_is_profile_context_not_pid_attribution(self):
        js = ui.FACTORY_JS
        self.assertIn("PROFILE SESSION ARCHIVE", js)
        self.assertIn("NOT ATTRIBUTED TO THIS PID", js)
        self.assertIn("UNKNOWN FIELDS ARE NOT ZERO", js)
        self.assertIn('const linked=p.observer==="instrumented"', js)
        self.assertIn('censusSelected=matching.length?matching[0].key:null', js)
        self.assertIn('context.live.events.items.slice(0,9)', js)

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_shipped_watch_renderer_distinguishes_missing_and_recent_evidence(self):
        code = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
function extract(name) {
  const begin = source.indexOf('  function '+name+'(');
  assert(begin>=0,name+' missing');
  const open=source.indexOf('{',begin);
  let depth=0,stop=-1;
  for(let i=open;i<source.length;i++){
    if(source[i]==='{')depth++;
    else if(source[i]==='}'&&!--depth){stop=i+1;break;}
  }
  assert(stop>open,name+' unbalanced');
  return source.slice(begin,stop);
}
function $(tag,cls,text){return {tag,cls,text:text||'',children:[],appendChild(c){this.children.push(c);},setAttribute(){}};}
function add(node,child){node.children.push(child);return child;}
function flag(t){return $( 'span','flag',t);}
function censusMetric(label,val,note){return $('div','metric',label+':'+val+':'+note);}
function censusStatus(p){return p.status==='running'?'OS PROCESS RUNNING':'ENDED';}
function censusFmt(v,suffix=''){return typeof v==='number'&&Number.isFinite(v)?String(v)+suffix:'--';}
const context=vm.createContext({$,add,flag,censusMetric,censusStatus,censusFmt,Date,Math,Number});
vm.runInContext(extract('observationAge')+extract('watchSummary'),context);
function flatten(node){return [String(node.text||'')].concat((node.children||[]).flatMap(flatten)).join('|');}
const blank={observer:'not_instrumented',status:'running',tokens:{},
  last_phase:null,model:null,cpu_percent:null,rss_mib:null,errors:null,
  cost_usd:null,speed_tps:null,last_api_ms:null,timeouts:null,fallbacks:null};
const absent=flatten(context.watchSummary(blank));
assert(absent.includes('NO LIVE TASK PHASE OBSERVED'));
assert(absent.includes('PROCESS ONLY'));
assert(absent.includes('UNPRICED'));
assert(absent.includes('UNKNOWN FIELDS ARE NOT ZERO'));
assert(!absent.includes('RECENT HOOK EVIDENCE'));
const active={...blank,observer:'instrumented',last_observation_age_s:5,last_phase:'tool_call',
  model:'real_model',cpu_percent:12.5,rss_mib:200,errors:0,
  tokens:{input:150,output:40},speed_tps:11.2,last_api_ms:770,cost_usd:0.03,timeouts:1};
const observed=flatten(context.watchSummary(active));
assert(observed.includes('RECENT HOOK EVIDENCE'));
assert(observed.includes('tool_call'));
assert(observed.includes('150'));
assert(observed.includes('$0.0300'));
assert(observed.includes('770 ms'));
const stale=flatten(context.watchSummary({...active,last_observation_age_s:120}));
assert(stale.includes('OBSERVER DATA STALE'));
assert(!stale.includes('RECENT HOOK EVIDENCE'));
console.log('PASS process-only unknown, fresh evidence, stale evidence, reported cost and token fields');
"""
        with tempfile.TemporaryDirectory() as tmp:
            script=Path(tmp)/"watch.js"
            script.write_text(code)
            asset=Path(ui.__file__).with_name("assets")/"factory.js"
            result=subprocess.run(["node",str(script),str(asset)],
                                  capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn("PASS process-only unknown",result.stdout)


if __name__=="__main__":
    unittest.main()
