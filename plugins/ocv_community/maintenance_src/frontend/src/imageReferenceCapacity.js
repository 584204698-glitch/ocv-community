// Counts the whole request, including the optional automatic scene slot.
export function redrawReferenceUsage({ids=[],current=false,scene=null,core=false,assets=[],limit=4}={}) {
 const selected=ids.map(id=>assets.find(asset=>asset.id===id)).filter(Boolean)
 const sceneAlreadySelected=scene&&selected.some(asset=>asset.id===scene.id||
  ((asset.file||asset.image)&&(asset.file||asset.image)===(scene.file||scene.image)))
 const automatic=scene&&(core||!sceneAlreadySelected)?1:0
 const total=ids.length+Number(current)+automatic
 return {materials:ids.length,current:Number(current),automatic,total,limit,remaining:Math.max(0,limit-total),over:total>limit}
}
