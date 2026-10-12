"""CPU-only check of a scoped H3 Guide backport; never loads checkpoints.

Run with the ComfyUI Python interpreter, passing runtime and candidate roots.
After installing, pass the pre-install backup root as a third path to keep
the legacy comparison independent of the patched running files.
All patched objects belong to this short-lived check process, not the server.
"""
import importlib.util
import json
from pathlib import Path
import sys


def main():
    runtime, candidate = map(Path, sys.argv[1:3])
    baseline = Path(sys.argv[3]) if len(sys.argv) > 3 else runtime
    sys.argv = ['h3-guide-cpu-check', '--cpu']
    sys.path.insert(0, str(runtime))
    import torch
    import comfy.nested_tensor

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    old = load('old_h3_layout', baseline / 'comfy/ldm/minimax/model.py')
    new = load('new_h3_layout', candidate / 'comfy/ldm/minimax/model.py')
    results = []
    image_ref = {'kind': 'image', 'latent_h': 4, 'latent_w': 4,
                 'latent': torch.zeros(1, 24, 1, 4, 4)}
    audio_ref = {'kind': 'audio', 'ref_audio_t': 12,
                 'audio_latent': torch.zeros(1, 32, 2, 12)}
    for refs in [None, [image_ref], [audio_ref], [image_ref, audio_ref]]:
        a = old.PackedLayout(8, 7, 4, 4, 40, refs=refs)
        b = new.PackedLayout(8, 7, 4, 4, 40, refs=refs)
        for field in ['position_ids', 'img_pos', 'img_update', 'audio_pos', 'audio_update']:
            assert torch.equal(getattr(a, field), getattr(b, field)), field
        assert a.segments == b.segments
    results.append('all four legacy layouts identical')

    for frame in [0, 21]:
        kf = [{'resolved_frame_index': frame, 'latent': torch.zeros(1, 24, 1, 4, 4)}]
        a = old.PackedLayout(8, 7, 4, 4, 40, keyframes=kf, frame_count=22)
        b = new.PackedLayout(8, 7, 4, 4, 40, keyframes=kf, frame_count=22)
        assert torch.allclose(a.position_ids, b.position_ids, atol=1e-12, rtol=0)
        assert a.segments == b.segments
    results.append('legacy first/last keyframes identical')

    kfs = [{'resolved_frame_index': 17, 'latent': torch.zeros(1, 24, 1, 4, 4)},
           {'resolved_frame_index': 24, 'audio_latent': torch.zeros(1, 32, 2, 8)}]
    refs = [image_ref, audio_ref]
    layout = new.PackedLayout(8, 12, 4, 4, 80, keyframes=kfs, refs=refs)
    assert layout.position_ids[layout.segments[1][0], 0] == 8 + 1 + 12 + 17 * new.FRAME_RESCALE
    assert any(kind == 'cond_audio' for _, _, kind in layout.segments)
    results.append('interior image/audio Guide time origin correct with references')

    base = load('comfy._candidate_h3_model_base', candidate / 'comfy/model_base.py')
    instance = base.MiniMaxH3.__new__(base.MiniMaxH3)
    instance.audio_scale = lambda: 1.0
    original = base.BaseModel.extra_conds
    base.BaseModel.extra_conds = lambda self, **kwargs: {}
    try:
        payload = instance.extra_conds(minimax_keyframes=kfs, minimax_refs=refs)['minimax_payload'].cond
        assert len(payload['cond_video_latents']) == 2 and len(payload['cond_audio_latents']) == 2
        assert payload['cond_video_latents'][0] is kfs[0]['latent']
    finally:
        base.BaseModel.extra_conds = original
    results.append('Guide, identity references and timbre references all preserved')

    import comfy.ops
    options = dict(hidden_size=128, num_layers=1, token_refiner_num_layers=0,
                   num_attention_heads=1, attention_head_dim=128, ffn_hidden_size=256,
                   timestep_input_dim=32, time_embed_hidden_size=128, time_embed_dim=64,
                   dtype=torch.float32, device='cpu', operations=comfy.ops.disable_weight_init)
    torch.manual_seed(41)
    baseline_model = old.MiniMaxH3Model(**options)
    with torch.no_grad():
        for parameter in baseline_model.parameters():
            parameter.normal_(mean=0, std=.02)
    candidate_model = new.MiniMaxH3Model(**options)
    candidate_model.load_state_dict(baseline_model.state_dict())
    baseline_model.requires_grad_(False)
    candidate_model.requires_grad_(False)
    inputs = [torch.randn(1, 24, 12, 4, 4), torch.randn(1, 32, 2, 80)]
    context = torch.randn(1, 8, 128)
    with torch.inference_mode():
        baseline_outputs = baseline_model(inputs, torch.tensor([500.]), context)
        candidate_outputs = candidate_model(inputs, torch.tensor([500.]), context)
        for a, b in zip(baseline_outputs, candidate_outputs):
            assert torch.equal(a, b), 'legacy numerical forward changed'
        guided_outputs = candidate_model(inputs, torch.tensor([500.]), context, minimax_payload=payload)
        assert all(torch.isfinite(stream).all() for stream in guided_outputs)
        assert [tuple(stream.shape) for stream in guided_outputs] == [tuple(stream.shape) for stream in inputs]
    results.append('tiny CPU model: legacy forward byte-identical; joint Guide forward finite and shape-correct')

    nodes = load('candidate_h3_nodes', candidate / 'comfy_extras/nodes_minimax_h3.py')

    class VAE:
        def encode(self, image):
            return torch.zeros(1, 24, 1, 4, 4)

    latent = {'samples': comfy.nested_tensor.NestedTensor([
        torch.zeros(1, 24, 12, 4, 4), torch.zeros(1, 32, 2, 80)])}
    cond = [[torch.zeros(1, 8, 8), {'minimax_refs': refs}]]
    output = nodes.MiniMaxH3AddGuide.execute(cond, latent, 17, vae=VAE(), image=torch.zeros(1, 64, 64, 3))
    assert len(output[0][0][1]['minimax_keyframes']) == 1
    assert 'minimax_keyframes' not in cond[0][1]
    try:
        nodes.MiniMaxH3AddGuide.execute(cond, latent, 999, vae=VAE(), image=torch.zeros(1, 64, 64, 3))
    except ValueError:
        pass
    else:
        raise AssertionError('out-of-range Guide accepted')
    results.append('native Guide executes without changing original conditioning; invalid frame rejected')
    print(json.dumps({'passed': results, 'gpu_used': False, 'checkpoints_loaded': False}))


if __name__ == '__main__':
    main()
