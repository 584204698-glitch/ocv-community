export function voiceLibraryPage(voices, {query='',scope='all',projectId='',boundIds=[],page=1,pageSize=8}={}) {
 const needle=query.trim().toLocaleLowerCase()
 const matches=voices.filter(v=>{
  if(scope==='archived'?!v.archived:v.archived)return false
  if(scope==='favorites'&&!v.favorite)return false
  if(scope==='project'&&v.source?.project_id!==projectId&&!boundIds.includes(v.voice_id))return false
  return !needle||[v.name,v.description,v.source?.project_title,v.source?.character_name].filter(Boolean).join(' ').toLocaleLowerCase().includes(needle)
 })
 const pages=Math.max(1,Math.ceil(matches.length/pageSize)),current=Math.max(1,Math.min(page,pages))
 return {total:matches.length,pages,page:current,rows:matches.slice((current-1)*pageSize,current*pageSize)}
}
