import os
import torch
from typing import Optional, Tuple
from omegaconf import OmegaConf
from huggingface_hub import hf_hub_download
from IPython.utils import io
from safetensors import safe_open
from src.SimpleStableDiffusionPipeline import SimpleStableDiffusionPipeline
from src.utils import (
    find_modules_and_assign_padding_mode,
    get_huggingface_cache_path,
    login_to_huggingface,
    process_embeddings_folder
)
from src.scripts.convert_from_ckpt import (
    convert_ldm_clip_checkpoint,
    convert_ldm_unet_checkpoint,
    convert_ldm_vae_checkpoint,
    convert_ldm_vae_checkpoint_from_file,
    convert_open_clip_checkpoint,
    create_unet_diffusers_config,
    create_vae_diffusers_config
)
from transformers import AutoFeatureExtractor, CLIPTokenizer
from diffusers import (
    AutoencoderKL,
    UNet2DConditionModel,
    DDIMScheduler
)
from diffusers.pipelines.stable_diffusion import StableDiffusionSafetyChecker


def prepare_pipe(model_name: str, model_type: str, downloadable_model_dict: dict, custom_model_dict: Optional[dict], cached_model_dict: Optional[dict], enable_attention_slicing: bool = False, enable_xformers: bool = False) -> Tuple[SimpleStableDiffusionPipeline, dict]:
    pipe_info = None

    if (cached_model_dict and ((model_name in cached_model_dict) or (model_type == "Installed Models"))):
        pipe_info = cached_model_dict[model_name]
        pipe = load_installed_model_from_hf_cache(pipe_info["path"])
    elif model_type == "Downloadable Models":
        model_choice = downloadable_model_dict[model_name]
        if model_choice["type"] == "diffusers":
            pipe = load_diffusers_model(model_choice)
        elif model_choice["type"] == "hf-file":
            pipe = download_and_load_non_diffusers_model(
                model_choice["repo_id"], model_name, model_choice["filename"], model_choice["config_file"], model_choice["vae"])
        pipe_info = {
            "keyword": model_choice["keyword"],
            "prediction_type": model_choice["prediction"]
        }
    elif custom_model_dict and model_type == "Custom Models":
        custom_model = custom_model_dict[model_name]
        pipe, prediction_type = load_custom_model_from_local_file(
            custom_model["path"], model_name, custom_model["config"], custom_model["vae"])

        pipe_info = {
            "keyword": custom_model["keywords"],
            "prediction_type": prediction_type
        }
    else:
        raise ValueError(f"Tried to load {model_name} and failed.")

    if enable_xformers:
        pipe.enable_xformers_memory_efficient_attention()
    elif enable_attention_slicing:
        pipe.enable_attention_slicing()

    find_modules_and_assign_padding_mode(pipe, "setup")
    pipe = pipe.to("cuda")
    pipe = pipe.to(torch.float16)
    return pipe, pipe_info


def load_custom_model_from_local_file(model_path: str, model_name: str, model_config: Optional[str], vae_file: Optional[str]) -> Tuple[SimpleStableDiffusionPipeline, str]:
    hf_cache_folder = get_huggingface_cache_path()

    return load_ckpt_or_safetensors_file_and_cache_as_diffusers(
        custom_model_path=model_path,
        model_name=model_name,
        folder_path=hf_cache_folder,
        config_file_path=model_config,
        vae_file_path=vae_file,
        should_cache=True
    )


def load_installed_model_from_hf_cache(model_path: str) -> SimpleStableDiffusionPipeline:
    return SimpleStableDiffusionPipeline.from_pretrained(
        model_path, safety_checker=None, requires_safety_checker=False, local_files_only=True, torch_dtype=torch.float16)


def download_and_load_non_diffusers_model(repo_id: str, model_name: str, filename: str, config_file: Optional[str], vae_file: Optional[str]) -> SimpleStableDiffusionPipeline:
    hf_cache_folder = get_huggingface_cache_path()
    checkpoint_path = hf_hub_download(repo_id=repo_id, filename=filename)
    if config_file:
        config = hf_hub_download(repo_id=repo_id, filename=config_file)
    else:
        config = None
    if vae_file:
        vae = hf_hub_download(repo_id=repo_id, filename=vae_file)
    else:
        vae = None
    pipe, _ = load_ckpt_or_safetensors_file_and_cache_as_diffusers(
        custom_model_path=checkpoint_path,
        model_name=model_name,
        folder_path=hf_cache_folder,
        config_file_path=config,
        vae_file_path=vae,
        should_cache=True
    )
    return pipe


def load_diffusers_model(model_choice: str) -> SimpleStableDiffusionPipeline:
    if model_choice["vae"] != "":
        if model_choice["requires_hf_login"] or model_choice["vae"]["requires_hf_login"]:
            login_to_huggingface()
        vae = AutoencoderKL.from_pretrained(model_choice["vae"]["repo_id"])
        pipe = SimpleStableDiffusionPipeline.from_pretrained(
            model_choice["repo_id"], vae=vae, safety_checker=None, requires_safety_checker=False, torch_dtype=torch.float16)
    else:
        if model_choice["requires_hf_login"]:
            login_to_huggingface()
        pipe = SimpleStableDiffusionPipeline.from_pretrained(
            model_choice["repo_id"], safety_checker=None, requires_safety_checker=False)
    return pipe


def load_embeddings(embeddings_folder: str, pipe: SimpleStableDiffusionPipeline) -> SimpleStableDiffusionPipeline:
    if embeddings_folder and os.path.exists(embeddings_folder):
        emb_list = process_embeddings_folder(embeddings_folder)

        for emb_path in emb_list:
            pipe.embedding_database.add_embedding_path(emb_path)
        pipe.load_embeddings()
    return pipe


def load_vae_file_to_current_pipe(pipe: SimpleStableDiffusionPipeline, vae_file_path: str) -> SimpleStableDiffusionPipeline:
    vae_config = dict(
        sample_size=pipe.vae.sample_size,
        in_channels=pipe.vae.in_channels,
        out_channels=pipe.vae.out_channels,
        down_block_types=pipe.vae.down_block_types,
        up_block_types=pipe.vae.up_block_types,
        block_out_channels=pipe.vae.block_out_channels,
        latent_channels=pipe.vae.latent_channels,
        layers_per_block=pipe.vae.layers_per_block,
    )

    vae_ckpt = torch.load(vae_file_path, map_location="cuda")
    vae_dict_1 = {k: v for k, v in vae_ckpt["state_dict"].items(
    ) if k[0:4] != "loss" and k not in {"model_ema.decay", "model_ema.num_updates"}}
    converted_vae_checkpoint = convert_ldm_vae_checkpoint_from_file(
        vae_dict_1, vae_config)

    vae = AutoencoderKL(**vae_config)
    vae.load_state_dict(converted_vae_checkpoint)
    pipe.vae = vae
    find_modules_and_assign_padding_mode(pipe, "setup")
    pipe = pipe.to("cuda")
    pipe = pipe.to(torch.float16)
    return pipe


def load_ckpt_or_safetensors_file_and_cache_as_diffusers(
        custom_model_path: str,
        model_name: str,
        folder_path: str,
        config_file_path: str = None,
        image_size: int = 512,
        prediction_type: str = None,
        vae_file_path: str = None,
        should_cache: bool = True) -> Tuple[SimpleStableDiffusionPipeline, str]:

    _, extension = os.path.splitext(custom_model_path)
    if extension == ".safetensors":
        checkpoint = {}
        with safe_open(custom_model_path, framework="pt", device="cuda") as f:
            for key in f.keys():
                checkpoint[key] = f.get_tensor(key)
        del f
    else:
        checkpoint = torch.load(custom_model_path, map_location="cuda")

    global_step = checkpoint["global_step"] if "global_step" in checkpoint else None
    checkpoint = checkpoint["state_dict"] if "state_dict" in checkpoint else checkpoint
    upcast_attention = False
    prediction_type = None
    image_size = None

    if config_file_path is None:
        key_name = "model.diffusion_model.input_blocks.2.1.transformer_blocks.0.attn2.to_k.weight"

        if key_name in checkpoint and checkpoint[key_name].shape[-1] == 1024:
            config_file_path = "src/scripts/v2-inference-v.yaml"
            if global_step == 110000:
                upcast_attention = True  # v2.1
        else:
            config_file_path = "src/scripts/v1-inference.yaml"

    original_config = OmegaConf.load(config_file_path)
    if (
        "parameterization" in original_config["model"]["params"]
        and original_config["model"]["params"]["parameterization"] == "v"
    ):
        if prediction_type is None:
            # NOTE: For stable diffusion 2 base it is recommended to pass `prediction_type=="epsilon"`
            # as it relies on a brittle global step parameter here
            prediction_type = "epsilon" if global_step == 875000 else "v_prediction"
        if image_size is None:
            # NOTE: For stable diffusion 2 base one has to pass `image_size==512`
            # as it relies on a brittle global step parameter here
            image_size = 512 if global_step == 875000 else 768
    else:
        if prediction_type is None:
            prediction_type = "epsilon"
        if image_size is None:
            image_size = 512

    num_train_timesteps = original_config.model.params.timesteps
    beta_start = original_config.model.params.linear_start
    beta_end = original_config.model.params.linear_end

    scheduler = DDIMScheduler(
        beta_end=beta_end,
        beta_schedule="scaled_linear",
        beta_start=beta_start,
        num_train_timesteps=num_train_timesteps,
        steps_offset=1,
        clip_sample=False,
        set_alpha_to_one=False,
        prediction_type=prediction_type,
    )

    unet_config = create_unet_diffusers_config(
        original_config, image_size=image_size)
    unet_config["upcast_attention"] = upcast_attention
    unet = UNet2DConditionModel(**unet_config)

    converted_unet_checkpoint = convert_ldm_unet_checkpoint(
        checkpoint, unet_config, path=custom_model_path, extract_ema=False
    )
    unet.load_state_dict(converted_unet_checkpoint)

    vae_config = create_vae_diffusers_config(
        original_config, image_size=image_size)

    if vae_file_path:
        vae_ckpt = torch.load(vae_file_path, map_location="cuda")
        vae_dict_1 = {k: v for k, v in vae_ckpt["state_dict"].items(
        ) if k[0:4] != "loss" and k not in {"model_ema.decay", "model_ema.num_updates"}}
        converted_vae_checkpoint = convert_ldm_vae_checkpoint_from_file(
            vae_dict_1, vae_config)
    else:
        converted_vae_checkpoint = convert_ldm_vae_checkpoint(
            checkpoint, vae_config)

    vae = AutoencoderKL(**vae_config)
    vae.load_state_dict(converted_vae_checkpoint)

    model_type = original_config.model.params.cond_stage_config.target.split(
        ".")[-1]

    if model_type == "FrozenOpenCLIPEmbedder":
        text_model = convert_open_clip_checkpoint(checkpoint)
        tokenizer = CLIPTokenizer.from_pretrained(
            "stabilityai/stable-diffusion-2", subfolder="tokenizer")
        safety_checker = StableDiffusionSafetyChecker.from_pretrained(
            "CompVis/stable-diffusion-safety-checker")
        pipe = SimpleStableDiffusionPipeline(
            vae=vae,
            text_encoder=text_model,
            tokenizer=tokenizer,
            unet=unet,
            scheduler=scheduler,
            feature_extractor=None,
            safety_checker=safety_checker,
            requires_safety_checker=False
        )
    elif model_type == "FrozenCLIPEmbedder":
        text_model = convert_ldm_clip_checkpoint(checkpoint)
        tokenizer = CLIPTokenizer.from_pretrained(
            "openai/clip-vit-large-patch14")
        feature_extractor = AutoFeatureExtractor.from_pretrained(
            "CompVis/stable-diffusion-safety-checker")
        safety_checker = StableDiffusionSafetyChecker.from_pretrained(
            "CompVis/stable-diffusion-safety-checker")
        pipe = SimpleStableDiffusionPipeline(
            vae=vae,
            text_encoder=text_model,
            tokenizer=tokenizer,
            unet=unet,
            scheduler=scheduler,
            feature_extractor=feature_extractor,
            safety_checker=safety_checker,
            requires_safety_checker=False
        )

    if should_cache:
        pipe.save_pretrained(os.path.join(folder_path, model_name))

    # remember to pipe to cuda!!
    return pipe, prediction_type