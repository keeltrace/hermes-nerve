"""Run the shipping UI's traversal and viewport budget code in Node."""
import shutil
import subprocess
import unittest
from hermes_nerve.observatory import ui


class JsBudgetTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
    def test_actual_browser_helpers_bound_20000_runs_and_keep_20_agents(self):
        js = r"""
const assert=require('node:assert/strict');
const vm=require('node:vm');
const read=require('node:fs').readFileSync(process.argv[2],'utf8');
function extract(name){
  const start=read.indexOf('function '+name+'(');
  assert(start>=0,name+' missing');
  const open=read.indexOf('{',start);
  let d=0,end=-1;
  for(let i=open;i<read.length;i++){
    if(read[i]==='{')d++;else if(read[i]==='}'&&!--d){end=i+1;break;}
  }
  assert(end>open,name+' incomplete');
  return read.slice(start,end);
}
const ctx=vm.createContext({Math,Array,Number,document:{getElementById:()=>({textContent:''})}});
vm.runInContext('var Q_RENDER_LIMIT=300;var quilt={};'+
  extract('boundedQ')+extract('quiltLod')+extract('cullQuilt'),ctx);
const root={id:'fleet',children:[{id:'host',children:[{id:'source',children:[]}]}]};
for(let i=0;i<20;i++){
 let a={id:'agent'+i,children:[]};
 for(let j=0;j<1000;j++)a.children.push({id:'run'+i+'-'+j,children:[]});
 root.children[0].children[0].children.push(a);
}
const limited=ctx.boundedQ(root,3000);
assert.equal(limited.rows.length,3000);
assert.equal(limited.truncated,true);
assert.equal(limited.rows.filter(x=>x[0].id.startsWith('agent')).length,20);
const visible=[];
const world={
 appendChild(el){el.parentNode=this;visible.push(el)},
 removeChild(el){const i=visible.indexOf(el);if(i>=0)visible.splice(i,1);el.parentNode=null;}
};
ctx.quilt={
 cam:{s:1,tx:0,ty:0},vp:{clientWidth:800,clientHeight:600},
 world,stack:[],truncated:false,
 items:Array.from({length:500},()=>({lb:[0,0,50,50],el:{parentNode:null},direct:true,depth:1}))
};
ctx.cullQuilt();
assert.equal(visible.length,300);
ctx.quilt.cam.tx=-50000;
ctx.cullQuilt();
assert.equal(visible.length,0);
console.log('PASS 20 agents retained, 3000 traversal budget, 300 render budget, zero offscreen nodes');
"""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "quilt_budget.js"
            script.write_text(js)
            asset = Path(ui.__file__).with_name("assets") / "app.js"
            proc = subprocess.run(["node", str(script), str(asset)], capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("PASS 20 agents retained", proc.stdout)


if __name__ == "__main__":
    unittest.main()
