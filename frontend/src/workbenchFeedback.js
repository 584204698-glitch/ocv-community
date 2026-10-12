// In-page feedback also works in browsers which suppress native confirm().
let pending=null,notice=null
export function cancelWorkbenchConfirmation(){pending?.(false)}
export function confirmWorkbenchAction(message){
 if(pending)return Promise.resolve(false)
 return new Promise(resolve=>{
  const previous=document.activeElement
  const overlay=document.createElement('div'),panel=document.createElement('section')
  overlay.className='ocv-action-confirm'
  overlay.style.cssText='position:fixed;inset:0;z-index:100000;background:#000a;display:flex;align-items:center;justify-content:center;padding:20px;box-sizing:border-box'
  panel.setAttribute('role','alertdialog');panel.setAttribute('aria-modal','true');panel.setAttribute('aria-labelledby','ocv-action-title');panel.setAttribute('aria-describedby','ocv-action-message')
  panel.style.cssText='width:min(520px,100%);max-height:85dvh;overflow:auto;box-sizing:border-box;background:#202825;color:#f1f5f3;border:1px solid #60736a;border-radius:14px;padding:22px;box-shadow:0 15px 60px #0008'
  const title=document.createElement('h2');title.id='ocv-action-title';title.textContent='确认操作';title.style.cssText='margin:0 0 16px;font-size:20px'
  const text=document.createElement('p');text.id='ocv-action-message';text.textContent=String(message);text.style.cssText='white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7'
  const actions=document.createElement('div');actions.style.cssText='display:flex;justify-content:flex-end;gap:12px;margin-top:20px'
  const cancel=document.createElement('button'),accept=document.createElement('button')
  cancel.type=accept.type='button';cancel.textContent='取消';accept.textContent='确认并继续'
  for(const button of [cancel,accept])button.style.cssText='padding:10px 16px;border:1px solid #70877a;border-radius:8px;background:#29372f;color:#fff;cursor:pointer;font-size:15px'
  accept.style.background='#27634c'
  function finish(value){
   if(pending!==finish)return
   pending=null;document.removeEventListener('keydown',keydown,true);document.removeEventListener('focusin',focusin,true);overlay.remove()
   if(previous?.isConnected)previous.focus({preventScroll:true})
   resolve(value)
  }
  function keydown(event){
   if(event.key==='Escape'){event.preventDefault();event.stopPropagation();finish(false)}
   if(event.key==='Tab'){event.preventDefault();(document.activeElement===cancel?accept:cancel).focus()}
  }
  function focusin(event){if(!panel.contains(event.target))cancel.focus()}
  pending=finish;cancel.onclick=()=>finish(false);accept.onclick=()=>finish(true)
  overlay.onclick=event=>{if(event.target===overlay)finish(false)}
  actions.append(cancel,accept);panel.append(title,text,actions);overlay.append(panel);document.body.append(overlay)
  document.addEventListener('keydown',keydown,true);document.addEventListener('focusin',focusin,true);cancel.focus()
 })
}
export function showWorkbenchError(message){
 notice?.remove();notice=null
 if(!message)return
 const element=document.createElement('div');element.className='ocv-action-error';element.setAttribute('role','alert')
 element.style.cssText='position:fixed;bottom:20px;left:50%;transform:translateX(-50%);z-index:99999;box-sizing:border-box;width:min(620px,calc(100% - 32px));max-height:40dvh;overflow:auto;display:flex;align-items:flex-start;gap:14px;padding:16px;background:#482b28;color:#fff;border:1px solid #d99a8d;border-radius:10px;box-shadow:0 4px 24px #0007'
 const text=document.createElement('span');text.textContent=String(message);text.style.cssText='flex:1;white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.5'
 const close=document.createElement('button');close.type='button';close.textContent='关闭';close.style.cssText='color:#fff;background:transparent;border:1px solid #d99a8d;padding:5px 8px;border-radius:5px;cursor:pointer';close.onclick=()=>{element.remove();if(notice===element)notice=null}
 element.append(text,close);document.body.append(element);notice=element
}
