"""The factory must show recent observer activity even after lease rollover."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from hermes_nerve.observatory import live_reader, ui


class EventRecencyTests(unittest.TestCase):
    def test_newest_64_leases_wins_even_when_pid_is_higher(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            for index in range(75):
                pid = 10001 + index
                path = directory / f"lease-{pid}-{'a' * 12}.json"
                path.write_text("{}")
                os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
            counters = {"entries_scanned": 0, "scan_cap_hit": False}
            leases, _ = live_reader._scan_root(str(directory), counters)
            self.assertEqual(len(leases), 75)
            self.assertEqual(leases[0][0], 10075)
            self.assertEqual(leases[live_reader.MAX_LEASE_FILES - 1][0], 10012)
            self.assertNotIn(10001, {x[0] for x in leases[:live_reader.MAX_LEASE_FILES]})

    def test_scan_ignores_lease_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            other = directory / "other.txt"
            other.write_text("{}")
            (directory / ("lease-90000-" + "b" * 12 + ".json")).symlink_to(other)
            counters = {"entries_scanned": 0, "scan_cap_hit": False}
            leases, _ = live_reader._scan_root(str(directory), counters)
            self.assertEqual(leases, [])

    @unittest.skipUnless(shutil.which("node"), "node is unavailable")
    def test_actual_factory_event_log_shows_newest_nine(self):
        source = Path(ui.__file__).with_name("assets") / "factory.js"
        js = r"""
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const text=fs.readFileSync(process.argv[2],'utf8');
const start=text.indexOf('  function eventLog(parent) {');
const end=text.indexOf('  function completeRoster(parent) {',start);
assert(start>=0&&end>start);
const fn=text.slice(start,end);
const context={live:{events:{items:Array.from({length:200},(_,i)=>({
  ts:String(200-i),kind:'api_request',status:'ok'
}))}}};
function $(tag,klass,label){return {tag,klass,label:label||'',children:[]};}
function add(p,c){p.children.push(c);return c;}
function instrument(t,s,k){return $('section',k,t);}
function stamp(t){return t;}
function slice(t,n){return String(t).slice(0,n);}
const ctx={context,$,add,instrument,stamp,slice};
vm.createContext(ctx);
vm.runInContext(fn+'this.result=eventLog({children:[]});',ctx);
function times(node){
 const all=node.tag==='time'?[String(node.label)]:[];
 for(const c of node.children||[])all.push(...times(c));
 return all;
}
assert.deepEqual(times(ctx.result),['200','199','198','197','196','195','194','193','192']);
console.log('PASS shipping Factory renders newest nine of 200 events');
"""
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "test.js"
            script.write_text(js)
            proc = subprocess.run(["node", str(script), str(source)],
                                  capture_output=True, text=True, timeout=12)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("PASS shipping Factory", proc.stdout)


if __name__ == "__main__":
    unittest.main()
