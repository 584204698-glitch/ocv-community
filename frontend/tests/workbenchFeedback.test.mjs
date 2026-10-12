import test from 'node:test'
import assert from 'node:assert/strict'
import {installBrowserGlobals} from './support/composableHarness.mjs'
import {confirmWorkbenchAction,cancelWorkbenchConfirmation,showWorkbenchError} from '../src/workbenchFeedback.js'
test('in-page confirmation works without native dialogs, cancels, and prevents duplicates',async()=>{
 const browser=installBrowserGlobals({autoConfirmActions:false,confirm:()=>{throw Error('Native confirmation must not be used')}})
 try{
  const pending=confirmWorkbenchAction('submit one shot')
  assert.equal(await confirmWorkbenchAction('duplicate'),false)
  const dialog=browser.document.body.children[0].children[0]
  assert.equal(dialog.role,'alertdialog');assert.equal(dialog.children[1].textContent,'submit one shot')
  dialog.children[2].children[1].click();assert.equal(await pending,true)
  assert.equal(browser.document.body.children.length,0)
  const cancelled=confirmWorkbenchAction('cancel me');cancelWorkbenchConfirmation();assert.equal(await cancelled,false)
 }finally{cancelWorkbenchConfirmation();browser.restore()}
})
test('feedback uses plain text, remains dismissible, and clears obsolete errors',()=>{
 const browser=installBrowserGlobals({autoConfirmActions:false})
 try{
  showWorkbenchError('<script>untrusted</script>')
  assert.equal(browser.document.body.children[0].children[0].textContent,'<script>untrusted</script>')
  showWorkbenchError('new problem');assert.equal(browser.document.body.children.length,1)
  browser.document.body.children[0].children[1].click();assert.equal(browser.document.body.children.length,0)
  showWorkbenchError('problem');showWorkbenchError('');assert.equal(browser.document.body.children.length,0)
 }finally{showWorkbenchError('');browser.restore()}
})
