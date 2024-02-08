import argparse
import json
import os
import re
import tempfile
import sys

import torch

from model_hf.modeling_vit import InternLMConfig, InternLMForCausalLM

sys.path.append('../')

def convert2hf(model_config, states_tp_pps):
    with tempfile.TemporaryDirectory() as folder:
        states = merge_pp(states_tp_pps)[0]
        
        dims_per_head = model_config["hidden_size"] // model_config["num_attention_heads"]
        base = 10000.0
        inv_freq = 1.0 / (base ** (torch.arange(0, dims_per_head, 2).float() / dims_per_head))
        
        current_states = {}
        
        vq_model_embed_weight = states.pop('embedding.vq_model.quantize.embedding.weight')
        embed_proj_weight = states.pop('embedding.embed_proj.weight')
        current_states["model.embed_tokens.weight"] = vq_model_embed_weight.mm(embed_proj_weight.T)
        current_states["model.norm.weight"] = states.pop("norm.weight")
        current_states["model.norm.bias"] = states.pop("norm.bias")
        current_states["lm_head.weight"] = states.pop("head.weight")
        
        mlp_bias = False
        for i in range(model_config["num_layers"]):
            states.pop(f"blocks.{i}.mixer.rotary_emb.inv_freq", None)
            
            wqkv = states.pop(f"blocks.{i}.mixer.Wqkv.weight").reshape(
                3, model_config["num_attention_heads"], -1, model_config["hidden_size"]
            )
            bqkv = states.pop(f"blocks.{i}.mixer.Wqkv.bias").reshape(3, model_config["num_attention_heads"], -1)
            
            current_states[f"model.layers.{i}.self_attn.q_proj.weight"] = wqkv[0].reshape(
                -1, model_config["hidden_size"]
            )
            current_states[f"model.layers.{i}.self_attn.q_proj.bias"] = bqkv[0].reshape(-1)
            current_states[f"model.layers.{i}.self_attn.k_proj.weight"] = wqkv[1].reshape(
                -1, model_config["hidden_size"]
            )
            current_states[f"model.layers.{i}.self_attn.k_proj.bias"] = bqkv[1].reshape(-1)
            current_states[f"model.layers.{i}.self_attn.v_proj.weight"] = wqkv[2].reshape(
                -1, model_config["hidden_size"]
            )
            current_states[f"model.layers.{i}.self_attn.v_proj.bias"] = bqkv[2].reshape(-1)
            
            current_states[f"model.layers.{i}.self_attn.o_proj.weight"] = states.pop(
                f"blocks.{i}.mixer.out_proj.weight"
            )
            current_states[f"model.layers.{i}.self_attn.o_proj.bias"] = states.pop(f"blocks.{i}.mixer.out_proj.bias")
            
            current_states[f"model.layers.{i}.mlp.fc1.weight"] = states.pop(f"blocks.{i}.mlp.fc1.weight")
            current_states[f"model.layers.{i}.mlp.fc2.weight"] = states.pop(f"blocks.{i}.mlp.fc2.weight")
            
            if f'blocks.{i}.mlp.fc1.bias' in states:
                mlp_bias = True
                current_states[f"model.layers.{i}.mlp.fc1.bias"] = states.pop(f"blocks.{i}.mlp.fc1.bias")
                current_states[f"model.layers.{i}.mlp.fc2.bias"] = states.pop(f"blocks.{i}.mlp.fc2.bias")
            
            current_states[f"model.layers.{i}.input_layernorm.weight"] = states.pop(f"blocks.{i}.norm1.weight")
            current_states[f"model.layers.{i}.input_layernorm.bias"] = states.pop(f"blocks.{i}.norm1.bias")
            current_states[f"model.layers.{i}.post_attention_layernorm.weight"] = states.pop(f"blocks.{i}.norm2.weight")
            current_states[f"model.layers.{i}.post_attention_layernorm.bias"] = states.pop(f"blocks.{i}.norm2.bias")
            current_states[f"model.layers.{i}.self_attn.rotary_emb.inv_freq"] = inv_freq
        
        config = InternLMConfig(
            hidden_size=model_config["hidden_size"],
            intermediate_size=int(model_config["hidden_size"] * model_config["mlp_ratio"]),
            num_attention_heads=model_config["num_attention_heads"],
            num_hidden_layers=model_config["num_layers"],
            norm_eps=1e-06,
            bias=True,
            mlp_bias=mlp_bias,
        )
        
        if model_config["vocab_size"] != -1:
            config.vocab_size = model_config["vocab_size"]
        
        config.save_pretrained(folder)
        torch.save(current_states, os.path.join(folder, "pytorch_model.bin"))
        
        model = InternLMForCausalLM.from_pretrained(folder, torch_dtype=torch.float16)
        del model.config._name_or_path
    
    return config, model


def merge_pp(states_tp_pp):
    max_tp = len(states_tp_pp)
    max_pp = len(states_tp_pp[0])
    
    full_states = []
    for tp in range(max_tp):
        layer_shift = 0
        
        tp_states = {}
        for pp in range(max_pp):
            _layer_shift = 0
            states = states_tp_pp[tp][pp]
            keys = list(states.keys())
            for key in keys:
                match = re.search("\.\d+\.", key)
                if match is not None:
                    s, e = match.span()
                    layer_idx = int(key[s + 1: e - 1]) + layer_shift
                    _layer_shift = max(_layer_shift, int(key[s + 1: e - 1]))
                    name = key[:s] + f".{layer_idx}." + key[e:]
                    tp_states[name] = states[key]
                else:
                    tp_states[key] = states[key]
            layer_shift += _layer_shift + 1
        full_states.append({(key[6:] if key.startswith("model.") else key): value for key, value in tp_states.items()})
    return full_states


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--src_folder', type=str, default='/path/to/intermlm_model/')  # internlm model folder
    parser.add_argument('--tgt_folder', type=str, default='/path/to/hf_model/')  # hf model folder
    args = parser.parse_args()
    
    
    def load(fp):
        with open(fp, "rb") as f:
            pt_data = torch.load(f, map_location="cpu")
        return pt_data
    
    
    folder = args.src_folder
    target_folder = args.tgt_folder
    model_config = load(os.path.join(folder, "model_config.pt"))
    
    fns = list(os.listdir(folder))
    
    model_fns = []
    for fn in fns:
        if fn.startswith("model_t") and not fn.endswith("md5"):
            model_fns.append(fn)
    
    max_tp, max_pp = -1, -1
    for fn in model_fns:
        _, tp, pp = os.path.splitext(fn)[0].split("_")
        max_pp = max(max_pp, int(pp[2:]) + 1)
        max_tp = max(max_tp, int(tp[2:]) + 1)
    
    states_tp_pps = [[]]
    
    for pp in range(max_pp):
        model_name = f"model_tp0_pp{pp}.pt"
        states = load(os.path.join(folder, model_name))
        states_tp_pps[0].append(states)
    
    config, model = convert2hf(model_config, states_tp_pps)
    
    os.makedirs(target_folder, exist_ok=True)
    model.save_pretrained(target_folder, max_shard_size="20GB")
    # TODO There should be a better way to add this.
    with open(os.path.join(target_folder, "config.json")) as fp:
        config_dict = json.load(fp)
    config_dict["auto_map"]["AutoModel"] = "modeling_vit.InternLMForCausalLM"
    with open(os.path.join(target_folder, "config.json"), "w") as fp:
        json.dump(config_dict, fp, indent=2)