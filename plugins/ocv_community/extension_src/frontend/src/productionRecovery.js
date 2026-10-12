// Guidance never changes production state. Unknown submissions must be queried,
// not resubmitted; only explicit user actions may start a paid preparation.
export function productionRecovery(project,shot){
 const message=String(shot?.video_error||shot?.image_error||project?.error||'')
 if(!message)return null
 const action=(id,label)=>({id,label})
 const result=(title,help,actions)=>({title,help,actions,message})
 const unknown=shot?.video_status==='unknown'||shot?.video_resume_available||
  (shot?.video_task_id&&!shot?.video_terminal&&shot?.video_status!=='completed')
 if(unknown)return result('先确认原任务结果','请求可能已经提交。继续查询不会创建新任务；不要反复重新生成。',shot.video_resume_available?[action('query','继续查询原任务')]:[action('settings','查看接口配置与服务状态')])
 if(/音色|声音参考|voice_id|Qwen.*未|Qwen.*安装/i.test(message))return result('声音尚未准备好','进入本项目声音设置，为报错的说话者采用音色，或明确选择模型自行生成。采用后返回本镜继续，无需重做图片。',[action('voices','去角色与音色设置')])
 if(/API|密钥|401|403|余额|额度|ReadTimeout|timed out|Connection|连接|OTHER_GPU_PROCESS|显存|CUDA|工作流|预设|服务.*未/i.test(message)&&!/schema|validation/i.test(message))return result('检查接口或计算服务','检查地址、授权、额度或工作站状态。已有素材保留；恢复服务后只继续未完成步骤。',[action('settings','打开接口与服务')])
 if(/开场图与|硬矛盾|核心图.*不一致|缺少.*角色.*图|角色.*缺失|必需道具|画面.*冲突/.test(message))return result('本镜图片与动作不一致','先查看下方具体差异。可只替换/重做本镜图片，或调整本镜动作。如果确认图片正确、属于分析误判，也可明确重新核对本镜；仍会执行完整校验，不直接放行。',[action('edit','检查并修改本镜图片与动作'),...(shot?.native_dialogue&&shot.video_status==='failed'?[action('prepare','图片正确，重新核对本镜')]:[])])
 if(shot?.native_dialogue&&shot.video_status==='failed'&&/开场对接|可见性|表演时间线|准备失败|validation|JSON|字段|schema/i.test(message))return result('本镜分析结果未通过校验','先尝试重新分析本镜。只调用本镜的语言模型准备，随后生成本镜视频；保留原台词、角色音色、图片与其他镜头。再次失败时可编辑本镜动作，不必重建项目。',[action('prepare','重新分析本镜并继续'),action('edit','修改本镜图片与动作')])
 if(/动作已修改|提示词|正文.*同步|动作时间线/.test(message)&&shot)return result('本镜方案需要同步','进入本镜编辑，核对动作后使用“按动态表达更新两个提示词”或“按核心图更新视频提示词”。仅换素材并保留方案时使用“采用输入（保留提示词）”。',[action('edit','打开本镜编辑')])
 if(shot?.image_status==='failed')return result('本镜图片未完成','已有图片和其他镜头保留。进入本镜核对提示词，重试本镜图片或上传自己的图片。',[action('edit','处理本镜图片')])
 if(['draft','storyboard_review'].includes(project?.status))return result('故事或分镜规划未完成','已保存草案不会丢失。继续未完成规划；若提示原文覆盖或剧情冲突，先在故事与分镜中核对采用稿，不能直接跳过校验。',[action('plan',project.planning_resume_available?'继续未完成规划':'修订并重试规划')])
 if(project?.status==='export_failed')return result('成片合成未完成','检查磁盘空间、FFmpeg 和素材是否可读取；已有镜头无需重新生成。恢复后只重新合成。',[action('export','回到合成与导出')])
 return result('当前步骤未完成','展开具体原因查看失败位置。原素材保留，可编辑当前镜头或检查服务后再继续；不自动重新付费提交。',shot?[action('edit','打开本镜编辑'),action('settings','查看接口配置')]:[action('settings','查看接口配置')])
}
