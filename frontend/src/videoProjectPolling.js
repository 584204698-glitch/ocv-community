// Keep idle/failed views current when another browser resumes the project.
export function createVideoProjectPoller({canRead,readIdentity,load,isCurrent,canApply=canRead,apply,onError}){
 let pending=false
 return async()=>{
  if(pending||!canRead())return
  const identity=readIdentity();pending=true
  try{const record=await load(identity);if(isCurrent(identity)&&canApply())apply(record)}
  catch(error){if(isCurrent(identity))onError(error)}
  finally{pending=false}
 }
}
