import assert from 'node:assert/strict'
import fs from 'node:fs'
import {test} from 'node:test'
import vm from 'node:vm'

const source=fs.readFileSync(new URL('../plugins/ocv_community/maintenance_src/frontend/src/imageReferenceCapacity.js',import.meta.url),'utf8')
const context=vm.createContext({})
vm.runInContext(source.replace('export function','function'),context)
const usage=options=>context.redrawReferenceUsage(options)
test('current and automatic scene consume the same four-input budget',()=>{
 assert.equal(usage({ids:['a','b','c','d']}).over,false)
 assert.equal(usage({ids:['a','b','c','d'],current:true}).over,true)
 assert.equal(usage({ids:['a','b','c'],scene:{id:'room'}}).total,4)
})
test('an explicitly selected scene is counted once; inputs are unchanged',()=>{
 const ids=['room','a','b','c'];const before=[...ids]
 assert.equal(usage({ids,assets:[{id:'room',file:'room.png'}],scene:{id:'other-room',image:'room.png'}}).total,4)
 assert.deepEqual(ids,before)
})
