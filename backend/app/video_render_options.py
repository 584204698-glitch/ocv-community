"""Project-scoped ComfyUI overrides; omitted values preserve legacy workflows."""
import copy

from pydantic import BaseModel, Field, model_validator


class VideoRenderOptions(BaseModel):
    comfyui_steps: int | None = Field(default=None, ge=1, le=100, strict=True)
    comfyui_width: int | None = Field(default=None, ge=256, le=4096, strict=True)
    comfyui_height: int | None = Field(default=None, ge=256, le=4096, strict=True)

    @model_validator(mode='after')
    def dimensions(self):
        if (self.comfyui_width is None) != (self.comfyui_height is None):
            raise ValueError('请同时填写视频宽度和高度')
        if any(value is not None and value % 8 for value in (self.comfyui_width, self.comfyui_height)):
            raise ValueError('视频宽度和高度必须是8的倍数')
        return self


def snapshot(parameters):
    return VideoRenderOptions.model_validate(parameters or {}).model_dump(exclude_none=True)


def apply(profile, options):
    """Modify a copy, never global profile configuration or linked schedules."""
    values = snapshot(options)
    result = copy.deepcopy(profile)
    if not values:
        return result
    from .comfyui_bridge import _api_graph
    graph = _api_graph(result['workflow'])
    if 'comfyui_width' in values:
        for field in ('width', 'height'):
            binding = result.get('mappings', {}).get(field) or {}
            node = graph.get(str(binding.get('node_id', '')), {})
            inputs = node.get('inputs', {})
            if not binding.get('input_name') or binding['input_name'] not in inputs:
                raise ValueError(f'视频{field}映射无效，请检查工作流')
        result.update(resolution_preset='custom', default_width=values['comfyui_width'],
                      default_height=values['comfyui_height'])
    if 'comfyui_steps' in values:
        binding = result.get('mappings', {}).get('steps') or {}
        if binding.get('node_id') and binding.get('input_name'):
            node = graph.get(str(binding['node_id']), {})
            inputs = node.get('inputs', {})
            name = binding['input_name']
            if name not in inputs:
                raise ValueError('采样步数映射无效，请检查工作流')
        else:
            candidates = [node for node in graph.values()
                          if node.get('class_type') in {'BasicScheduler', 'KSampler', 'KSamplerAdvanced'}
                          and type(node.get('inputs', {}).get('steps')) is int]
            if len(candidates) != 1:
                raise ValueError('无法唯一确定采样步数节点，请在工作流中配置步数映射')
            inputs, name = candidates[0]['inputs'], 'steps'
        inputs[name] = values['comfyui_steps']
    return result
