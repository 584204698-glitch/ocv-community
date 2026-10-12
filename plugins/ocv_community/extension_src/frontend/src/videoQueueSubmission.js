// Retry only a rejected queue append whose adopted production inputs are unchanged.
const conflicts=new Set(['项目刚刚发生变化，请刷新后重试','项目正在规划或已被其他页面修改，请重新读取'])
const runtime=new Set(['revision','logs','status','error','updated_at'])
function stable(value){
 if(Array.isArray(value))return value.map(stable)
 if(value&&typeof value==='object')return Object.fromEntries(Object.keys(value).sort().map(key=>[key,stable(value[key])]))
 return value
}
function basis(record,ids){
 const selected=ids.map(id=>record.shots?.find(shot=>shot.id===id))
 if(selected.some(shot=>!shot))return null
 const context=Object.fromEntries(Object.entries(record).filter(([key])=>key!=='shots'&&!runtime.has(key)))
 return JSON.stringify(stable({context,selected}))
}
export async function submitLocalVideoQueue({record,shotIds,retryFailed,call,onRefresh}){
 const expected=JSON.parse(JSON.stringify(record))
 const body={revision:expected.revision,shot_ids:shotIds,retry_failed:retryFailed}
 try{return await call('/'+expected.id+'/videos/generate','POST',body)}
 catch(error){
  if(error.status!==409||!conflicts.has(error.message))throw error
  const latest=await call('/'+expected.id)
  onRefresh(latest)
  if(latest.id!==expected.id||!['video_review','video_generating'].includes(latest.status)
     ||basis(expected,shotIds)!==basis(latest,shotIds))throw error
  // The backend rejected the first request before any submission. A fresh
  // revision still goes through the server's original guards and queue dedup.
  return await call('/'+expected.id+'/videos/generate','POST',{...body,revision:latest.revision})
 }
}
