import gc
import json
import os
import re
import time
from pathlib import Path
from peft import PeftModel
from typing import Optional, List, Dict, Tuple, Union
import torch
import transformers
from accelerate import infer_auto_device_map, init_empty_weights
from transformers import (AutoConfig, AutoModel, AutoModelForCausalLM,
                          AutoTokenizer, BitsAndBytesConfig, LlamaTokenizer)


class LoaderLLM:
    """
    加载自定义 model
    """
    # remote in the model on loader checkpoint
    no_remote_model: bool = False
    # 模型名称
    model_name: str = None
    tokenizer: object = None
    model: object = None
    model_config: object = None
    lora_names: set = []
    model_dir: str = None
    lora_dir: str = None
    ptuning_dir: str = None
    use_ptuning_v2: bool = False
    cpu: bool = False
    gpu_memory: object = None
    cpu_memory: object = None
    auto_devices: object = True
    load_in_8bit: bool = False
    is_llamacpp: bool = False
    bf16: bool = False
    params: object = None
    # 自定义设备网络
    device_map: Optional[Dict[str, int]] = None
    # 默认 cuda ，如果不支持cuda使用多卡， 如果不支持多卡 使用cpu
    llm_device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"

    def __init__(self, params: dict = None):
        """
        模型初始化
        :param params:
        """
        self.params = params or {}
        self.no_remote_model = params.get('no_remote_model', False)
        self.model_name = params.get('model', '')
        self.lora = params.get('lora', '')
        self.use_ptuning_v2 = params.get('use_ptuning_v2', False)
        self.model = None
        self.tokenizer = None
        self.model_dir = params.get('model_dir', '')
        self.lora_dir = params.get('lora_dir', '')
        self.ptuning_dir = params.get('ptuning_dir', '')
        self.cpu = params.get('cpu', False)
        self.gpu_memory = params.get('gpu_memory', None)
        self.cpu_memory = params.get('cpu_memory', None)
        self.auto_devices = params.get('auto_devices', True)
        self.load_in_8bit = params.get('load_in_8bit', False)
        self.bf16 = params.get('bf16', False)
        self.reload_model()

    def _load_model_config(self, model_name):
        checkpoint = Path(f'{self.model_dir}/{model_name}')
        if not self.no_remote_model:
            checkpoint = model_name

        model_config = AutoConfig.from_pretrained(checkpoint, trust_remote_code=True)

        return model_config

    def _load_model(self, model_name):
        """
        加载自定义位置的model
        :param model_name:
        :return:
        """
        print(f"Loading {model_name}...")
        t0 = time.time()

        checkpoint = Path(f'{self.model_dir}/{model_name}')

        self.is_llamacpp = len(list(checkpoint.glob('ggml*.bin'))) > 0

        if not self.no_remote_model:
            checkpoint = model_name


        if 'chatglm' in model_name.lower():
            LoaderClass = AutoModel
        else:
            LoaderClass = AutoModelForCausalLM

        # Load the model in simple 16-bit mode by default
        if not any([self.cpu, self.load_in_8bit, self.auto_devices, self.gpu_memory is not None, self.cpu_memory is not None, self.is_llamacpp]):

            if torch.cuda.is_available() and self.llm_device.lower().startswith("cuda"):
                # 根据当前设备GPU数量决定是否进行多卡部署
                num_gpus = torch.cuda.device_count()
                if num_gpus < 2 and self.device_map is None:
                    model = (
                        LoaderClass.from_pretrained(checkpoint,
                                                    low_cpu_mem_usage=True,
                                                    config=self.model_config,
                                                    torch_dtype=torch.bfloat16 if self.bf16 else torch.float16,
                                                    trust_remote_code=True)
                        .half()
                        .cuda()
                    )
                else:
                    from accelerate import dispatch_model

                    model = LoaderClass.from_pretrained(checkpoint,
                                                        low_cpu_mem_usage=True,
                                                        config=self.model_config,
                                                        torch_dtype=torch.bfloat16 if self.bf16 else torch.float16,
                                                        trust_remote_code=True).half()
                    # 可传入device_map自定义每张卡的部署情况
                    if self.device_map is None:
                        device_map = self.auto_configure_device_map(num_gpus)

                    model = dispatch_model(model, device_map=device_map)
            else:
                print("Warning: torch.cuda.is_available() returned False.\nThis means that no GPU has been detected.\nFalling back to CPU mode.\n")
                model = (
                    AutoModel.from_pretrained(
                        checkpoint,
                        config=self.model_config,
                        trust_remote_code=True)
                    .float()
                    .to(self.llm_device)
                )

        elif self.is_llamacpp:
            from models.extensions.llamacpp_model_alternative import LlamaCppModel

            model_file = list(checkpoint.glob('ggml*.bin'))[0]
            print(f"llama.cpp weights detected: {model_file}\n")

            model, tokenizer = LlamaCppModel.from_pretrained(model_file)
            return model, tokenizer

        # Custom
        else:
            params = {"low_cpu_mem_usage": True}
            if not any((self.cpu, torch.cuda.is_available(), torch.has_mps)):
                print("Warning: torch.cuda.is_available() returned False.\nThis means that no GPU has been detected.\nFalling back to CPU mode.\n")
                self.cpu = True

            if self.cpu:
                params["torch_dtype"] = torch.float32
            else:
                params["device_map"] = 'auto'
                params["trust_remote_code"] = True
                if self.load_in_8bit and any((self.auto_devices, self.gpu_memory)):
                    params['quantization_config'] = BitsAndBytesConfig(load_in_8bit=True, llm_int8_enable_fp32_cpu_offload=True)
                elif self.load_in_8bit:
                    params['quantization_config'] = BitsAndBytesConfig(load_in_8bit=True)
                elif shared.args.bf16:
                    params["torch_dtype"] = torch.bfloat16
                else:
                    params["torch_dtype"] = torch.float16

                if self.gpu_memory:
                    memory_map = list(map(lambda x: x.strip(), self.gpu_memory))
                    max_cpu_memory = self.cpu_memory.strip() if self.cpu_memory is not None else '99GiB'
                    max_memory = {}
                    for i in range(len(memory_map)):
                        max_memory[i] = f'{memory_map[i]}GiB' if not re.match('.*ib$', memory_map[i].lower()) else memory_map[i]
                    max_memory['cpu'] = max_cpu_memory
                    params['max_memory'] = max_memory
                elif self.auto_devices:
                    total_mem = (torch.cuda.get_device_properties(0).total_memory / (1024 * 1024))
                    suggestion = round((total_mem - 1000) / 1000) * 1000
                    if total_mem - suggestion < 800:
                        suggestion -= 1000
                    suggestion = int(round(suggestion / 1000))
                    print(f"\033[1;32;1mAuto-assiging --gpu-memory {suggestion} for your GPU to try to prevent out-of-memory errors.\nYou can manually set other values.\033[0;37;0m")

                    max_memory = {0: f'{suggestion}GiB', 'cpu': f'{self.cpu_memory or 99}GiB'}
                    params['max_memory'] = max_memory


            if self.load_in_8bit and params.get('max_memory', None) is not None and params['device_map'] == 'auto':
                config = AutoConfig.from_pretrained(checkpoint)
                with init_empty_weights():
                    model = AutoModelForCausalLM.from_config(config)
                model.tie_weights()
                if self.device_map is not None:
                    params['device_map'] = self.device_map
                else:
                    params['device_map'] = infer_auto_device_map(
                        model,
                        dtype=torch.int8,
                        max_memory=params['max_memory'],
                        no_split_module_classes=model._no_split_modules
                )

            model = AutoModelForCausalLM.from_pretrained(checkpoint, **params)

        # Loading the tokenizer
        if type(model) is transformers.LlamaForCausalLM:
            tokenizer = LlamaTokenizer.from_pretrained(checkpoint, clean_up_tokenization_spaces=True)
            # Leaving this here until the LLaMA tokenizer gets figured out.
            # For some people this fixes things, for others it causes an error.
            try:
                tokenizer.eos_token_id = 2
                tokenizer.bos_token_id = 1
                tokenizer.pad_token_id = 0
            except:
                pass
        else:
            tokenizer = AutoTokenizer.from_pretrained(checkpoint, trust_remote_code=True)

        print(f"Loaded the model in {(time.time()-t0):.2f} seconds.")
        return model, tokenizer

    def auto_configure_device_map(num_gpus: int) -> Dict[str, int]:
        # transformer.word_embeddings 占用1层
        # transformer.final_layernorm 和 lm_head 占用1层
        # transformer.layers 占用 28 层
        # 总共30层分配到num_gpus张卡上
        num_trans_layers = 28
        per_gpu_layers = 30 / num_gpus

        # bugfix: 在linux中调用torch.embedding传入的weight,input不在同一device上,导致RuntimeError
        # windows下 model.device 会被设置成 transformer.word_embeddings.device
        # linux下 model.device 会被设置成 lm_head.device
        # 在调用chat或者stream_chat时,input_ids会被放到model.device上
        # 如果transformer.word_embeddings.device和model.device不同,则会导致RuntimeError
        # 因此这里将transformer.word_embeddings,transformer.final_layernorm,lm_head都放到第一张卡上
        device_map = {'transformer.word_embeddings': 0,
                      'transformer.final_layernorm': 0, 'lm_head': 0}

        used = 2
        gpu_target = 0
        for i in range(num_trans_layers):
            if used >= per_gpu_layers:
                gpu_target += 1
                used = 0
            assert gpu_target < num_gpus
            device_map[f'transformer.layers.{i}'] = gpu_target
            used += 1

        return device_map

    def _add_lora_to_model(self, lora_names):
        # 目前加载的lora
        prior_set = set(self.lora_names)
        # 需要加载的
        added_set = set(lora_names) - prior_set
        # 删除的lora
        removed_set = prior_set - set(lora_names)
        self.lora_names = list(lora_names)

        # Nothing to do = skip.
        if len(added_set) == 0 and len(removed_set) == 0:
            return

        # Only adding, and already peft? Do it the easy way.
        if len(removed_set) == 0 and len(prior_set) > 0:
            print(f"Adding the LoRA(s) named {added_set} to the model...")
            for lora in added_set:
                self.model.load_adapter(Path(f"{self.lora_dir}/{lora}"), lora)
            return

        # If removing anything, disable all and re-add.
        if len(removed_set) > 0:
            shared.model.disable_adapter()

        if len(lora_names) > 0:
            print("Applying the following LoRAs to {}: {}".format(self.model_name, ', '.join(lora_names)))
            params = {}
            if not self.cpu:
                params['dtype'] = self.model.dtype
                if hasattr(self.model, "hf_device_map"):
                    params['device_map'] = {"base_model.model." + k: v for k, v in self.model.hf_device_map.items()}
                elif self.load_in_8bit:
                    params['device_map'] = {'': 0}
            self.model.resize_token_embeddings(len(self.tokenizer))

            self.model = PeftModel.from_pretrained(self.model, Path(f"{self.lora_dir}/{lora_names[0]}"), **params)

            for lora in lora_names[1:]:
                self.model.load_adapter(Path(f"{self.lora_dir}/{lora}"), lora)

            if not self.load_in_8bit and not self.cpu:

                if not hasattr(self.model, "hf_device_map"):
                    if torch.has_mps:
                        device = torch.device('mps')
                        self.model = self.model.to(device)
                    else:
                        self.model = self.model.cuda()

    def clear_torch_cache(self):
        gc.collect()
        if not self.cpu:
            device_id = "0" if torch.cuda.is_available() else None
            CUDA_DEVICE = f"{self.llm_device}:{device_id}" if device_id else self.llm_device
            with torch.cuda.device(CUDA_DEVICE):
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()



    def unload_model(self):
        self.model = self.tokenizer = None
        self.clear_torch_cache()

    def reload_model(self):
        self.unload_model()
        self.model_config = self._load_model_config(self.model_name)

        if self.use_ptuning_v2:
            try:
                prefix_encoder_file = open(Path(f'{self.ptuning_dir}/config.json'), 'r')
                prefix_encoder_config = json.loads(prefix_encoder_file.read())
                prefix_encoder_file.close()
                self.model_config.pre_seq_len = prefix_encoder_config['pre_seq_len']
                self.model_config.prefix_projection = prefix_encoder_config['prefix_projection']
            except Exception:
                print("加载PrefixEncoder config.json失败")

        self.model, self.tokenizer = self._load_model(self.model_name)

        if self.lora:
            self._add_lora_to_model([self.lora])

        if self.use_ptuning_v2:
            try:
                prefix_state_dict = torch.load(Path(f'{self.ptuning_dir}/pytorch_model.bin'))
                new_prefix_state_dict = {}
                for k, v in prefix_state_dict.items():
                    if k.startswith("transformer.prefix_encoder."):
                        new_prefix_state_dict[k[len("transformer.prefix_encoder."):]] = v
                self.model.transformer.prefix_encoder.load_state_dict(new_prefix_state_dict)
                self.model.transformer.prefix_encoder.float()
            except Exception:
                print("加载PrefixEncoder模型参数失败")

        self.model = self.model.eval()
