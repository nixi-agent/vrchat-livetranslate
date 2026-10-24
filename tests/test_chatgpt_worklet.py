"""Execute the shipped AudioWorklet, including overflow recovery, in Node's VM."""
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.chatgpt_browser_page import PAGE
from _node_script import run_node


class WorkletTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js required for actual worklet execution')
    def test_seal_flushes_short_tail_before_ack_and_stops_capture(self):
        script = r'''
import assert from 'node:assert/strict'; import vm from 'node:vm';
const source=JSON.parse(process.argv[1]), types={};
class Processor {constructor(){this.messages=[];this.port={postMessage:m=>this.messages.push(m)};}}
vm.runInNewContext(source,{AudioWorkletProcessor:Processor,sampleRate:24000,
  registerProcessor:(name,type)=>types[name]=type});
const p=new types['pcm-output']();
for(let i=0;i<6;i++)p.process([[new Float32Array(128).fill(.125)]],[]);
if(p.port.onmessage)p.port.onmessage({data:{type:'seal',seal_id:'0123456789abcdef'}});
const frames=p.messages.filter(m=>m.byteLength!==undefined);
assert.equal(frames.length,2,'seal must flush the 288-sample short tail');
assert.equal(frames[1].byteLength,576);
assert.equal(p.messages.at(-1).type,'sealed');
assert.equal(p.messages.at(-1).samples,768);
const count=p.messages.length;
assert.equal(p.process([[new Float32Array(128).fill(.5)]],[]),false);
assert.equal(p.messages.length,count,'no appends after seal');
console.log('PCM seal/tail/ACK PASS');
'''
        source = PAGE.split('const worklet = `', 1)[1].split('`;', 1)[0]
        result = run_node(script, source)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for actual worklet execution')
    def test_consumption_ack_and_overflow_reset(self):
        script = r'''
import assert from 'node:assert/strict'; import vm from 'node:vm';
const worklet = JSON.parse(process.argv[1]);
const types = {};
class Processor {constructor(){this.messages=[];this.port={postMessage:m=>this.messages.push(m)};}}
vm.runInNewContext(worklet, {AudioWorkletProcessor:Processor, sampleRate:24000,
  registerProcessor:(name,type)=>types[name]=type});
const feed = (p,n=1600)=>p.port.onmessage({data:new Int16Array(n).fill(10).buffer});
const p = new types['pcm-input']();
feed(p); p.process([], [[new Float32Array(128)]]);
for(let i=0;i<10;i++)feed(p);
assert.equal(p.offset,0,'Overflow must reset the old block cursor');
assert.equal(p.phase,0); assert.equal(p.previous,0); assert.equal(p.current,0);
assert(p.messages.some(m=>m.type==='overflow'));
feed(p);
for(let i=0;i<1600;i++)assert(Number.isFinite(p.next()));
assert(p.messages.some(m=>m.type==='consumed' && m.bytes===3200));
assert.equal(p.count,0); assert.equal(p.blocks.length,0);
console.log('Actual worklet reset and consumption acknowledgments PASS');
'''
        source = PAGE.split('const worklet = `', 1)[1].split('`;', 1)[0]
        result = run_node(script, source)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
