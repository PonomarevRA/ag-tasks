import test from 'node:test';
import assert from 'node:assert/strict';
import {createCommandClient,issueCommand} from '../web/service-commands.js';

/** Model tab storage across reload without persisting task bodies or relying on a DOM implementation. */
function cache() {const values=new Map();return {getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,value),values};}

/** Hashing promises may settle in either order; establish request identity before choosing which ACK arrives. */
async function sent(pending,count) {
  for (let turn=0; turn<1000; turn++) {
    if (pending.length>=count) return;
    await new Promise(resolve=>setImmediate(resolve));
  }
  assert.fail(`Expected ${count} sends, observed ${pending.length}`);
}

test('ambiguous failure and reload retain identity; confirmed new intent receives another key',async()=>{
  const storage=cache(); const seen=[];let counter=0;
  const uuid=()=>`id-${++counter}`;
  const failed=createCommandClient(async(path,method,body)=>{seen.push(body);throw new Error('lost ACK');},storage,uuid);
  await assert.rejects(failed('issue.create',{title:'hello'}),/lost ACK/);
  assert.ok(![...storage.values.values()].join('').includes('hello'));
  const resumed=createCommandClient(async(path,method,body)=>{seen.push(body);return {id:'same-task'};},storage,uuid);
  await resumed('issue.create',{title:'hello'});
  assert.equal(seen[0].commandId,seen[1].commandId);
  assert.equal(seen[0].clientId,seen[1].clientId);
  await resumed('issue.create',{title:'hello'});
  assert.notEqual(seen[1].commandId,seen[2].commandId);
});

test('object key order is canonical; exact whitespace and array order are part of intent',async()=>{
  const seen=[];const client=createCommandClient(async(path,method,body)=>{seen.push(body);throw new Error('ambiguous');},cache());
  await assert.rejects(client('fixture',{a:1,b:['x','y']}));
  await assert.rejects(client('fixture',{b:['x','y'],a:1}));
  await assert.rejects(client('fixture',{a:1,b:['y','x']}));
  assert.equal(seen[0].commandId,seen[1].commandId);
  assert.notEqual(seen[0].commandId,seen[2].commandId);
  await assert.rejects(client('fixture',{text:'a'}));
  await assert.rejects(client('fixture',{text:'a '}));
  assert.notEqual(seen[3].commandId,seen[4].commandId);
});

test('corrupt pending storage blocks mutation instead of silently changing command identity',()=>{
  assert.throws(()=>createCommandClient(()=>{}, {getItem:()=>'{broken',setItem:()=>{}}),/Сверьте/);
});

test('UI maps task data operations to common commands and leaves workflow authority routes intact',()=>{
  assert.deepEqual(issueCommand('/api/v1/issues','POST',{title:'one'}),['issue.create',{title:'one'}]);
  assert.deepEqual(issueCommand('/api/v1/issues/AGT-1','PATCH',{title:'two'}),['issue.update',{issueId:'AGT-1',changes:{title:'two'}}]);
  assert.equal(issueCommand('/api/v1/issues/AGT-1/approve-merge','POST',{}),null);
  assert.equal(issueCommand('/api/v1/issues/AGT-1/work/start','POST',{}),null);
});

test('unavailable key storage never starts an untracked mutation',()=>{
  assert.throws(()=>createCommandClient(()=>{throw Error('Must not send');},null),/Хранилище ключей/);
});

test('storage failure after server ACK blocks new effects instead of issuing a fresh key',async()=>{
  const storage=cache();const set=storage.setItem;let fail=false,sends=0;
  storage.setItem=(key,value)=>{if(fail)throw new Error('quota');set(key,value);};
  const client=createCommandClient(async()=>{sends++;fail=true;return {id:'committed'};},storage);
  await assert.rejects(client('issue.create',{title:'one'}),/quota/);
  await assert.rejects(client('issue.create',{title:'one'}),/Сверьте/);
  assert.equal(sends,1);
});

test('late duplicate ACK cannot clear a newer pending identical intent',async()=>{
  const storage=cache();const pending=[];const bodies=[];
  const client=createCommandClient((path,method,body)=>{bodies.push(body);return new Promise(resolve=>pending.push(resolve));},storage);
  const first=client('issue.create',{title:'same'});
  await sent(pending,1);
  const duplicate=client('issue.create',{title:'same'});
  await sent(pending,2);
  pending[0]({id:'one'});await first;
  const newer=client('issue.create',{title:'same'});
  await sent(pending,3);
  pending[1]({id:'one'});await duplicate;
  const saved=JSON.parse([...storage.values.values()][0]);
  assert.equal(Object.values(saved.pending)[0],bodies[2].commandId);
  assert.notEqual(bodies[0].commandId,bodies[2].commandId);
  pending[2]({id:'two'});await newer;
});

test('confirmed domain refusal retires intent, while ambiguous server failure retains it',async()=>{
  const seen=[];let counter=0;
  const client=createCommandClient(async(path,method,body)=>{
    seen.push(body);const error=new Error('rejected');
    error.status=seen.length===1?409:503;error.code='queue_busy';throw error;
  },cache(),()=>`fresh-${++counter}`);
  for(let index=0;index<3;index++)await assert.rejects(client('attempt.claim',{laneId:'one'}));
  assert.notEqual(seen[0].commandId,seen[1].commandId);
  assert.equal(seen[1].commandId,seen[2].commandId);
});

test('older concurrent domain refusal cannot remove a newer pending intent',async()=>{
  const storage=cache(),pending=[],bodies=[];
  const client=createCommandClient((path,method,body)=>{
    bodies.push(body);return new Promise((resolve,reject)=>pending.push({resolve,reject}));
  },storage);
  const first=client('fixture',{same:true});
  await sent(pending,1);
  const duplicate=client('fixture',{same:true});
  await sent(pending,2);
  pending[0].resolve({ok:true});await first;
  const newer=client('fixture',{same:true});
  await sent(pending,3);
  const error=Object.assign(new Error('refused'),{status:409,code:'domain_rejection'});
  const rejected=assert.rejects(duplicate,/refused/);pending[1].reject(error);await rejected;
  const saved=JSON.parse([...storage.values.values()][0]);
  assert.equal(Object.values(saved.pending)[0],bodies[2].commandId);
  pending[2].resolve({ok:true});await newer;
});

test('refusal outcome storage failure blocks new effects after the failed retirement',async()=>{
  const storage=cache(),set=storage.setItem;let fail=false,sends=0;
  storage.setItem=(key,value)=>{if(fail)throw Error('quota');set(key,value);};
  const client=createCommandClient(async()=>{
    sends++;fail=true;throw Object.assign(Error('refused'),{status:409,code:'domain_rejection'});
  },storage);
  await assert.rejects(client('fixture',{}),/quota/);
  await assert.rejects(client('fixture',{}),/Сверьте/);
  assert.equal(sends,1);
});
