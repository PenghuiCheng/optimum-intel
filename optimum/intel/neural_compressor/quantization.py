#  Copyright 2022 The HuggingFace Team. All rights reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import copy
import inspect
import logging
import os
from enum import Enum
from itertools import chain
from pathlib import Path
from typing import Callable, ClassVar, Dict, List, Optional, Tuple, Union

import torch
import transformers
from datasets import Dataset, load_dataset
from packaging import version
from torch.quantization import add_observer_, convert
from torch.quantization.quantize_fx import convert_fx, prepare_fx, prepare_qat_fx
from torch.utils.data import DataLoader, RandomSampler
from transformers import (
    AutoConfig,
    AutoModel,
    AutoModelForCausalLM,
    AutoModelForMaskedLM,
    AutoModelForMultipleChoice,
    AutoModelForQuestionAnswering,
    AutoModelForSeq2SeqLM,
    AutoModelForSequenceClassification,
    AutoModelForTokenClassification,
    AutoModelForVision2Seq,
    DataCollator,
    PretrainedConfig,
    PreTrainedModel,
    XLNetLMHeadModel,
    default_data_collator,
)
from transformers.models.auto.auto_factory import _get_model_class
from transformers.utils import TRANSFORMERS_CACHE, is_offline_mode

import neural_compressor
from huggingface_hub import HfApi, hf_hub_download
from neural_compressor import PostTrainingConfig, quantization
from neural_compressor.adaptor.pytorch import PyTorch_FXAdaptor, _cfg_to_qconfig, _propagate_qconfig, get_torch_version
from neural_compressor.adaptor.torch_utils.util import get_embedding_contiguous
from neural_compressor.conf.config import Quantization_Conf
from neural_compressor.experimental import Quantization
from neural_compressor.model.torch_model import PyTorchModel
from neural_compressor.utils.pytorch import _load_int8_orchestration
from optimum.exporters import TasksManager
from optimum.exporters.onnx import OnnxConfig
from optimum.quantization_base import OptimumQuantizer

from .configuration import IncOptimizedConfig, IncQuantizationConfig
from .utils import (
    MIN_QDQ_ONNX_OPSET,
    ONNX_WEIGHTS_NAME,
    WEIGHTS_NAME,
    INCDataLoader,
    _cfgs_to_fx_cfgs,
    is_torch_less_than_1_13,
)


logger = logging.getLogger(__name__)


_neural_compressor_version = version.parse(version.parse(neural_compressor.__version__).base_version)
# NEURAL_COMPRESSOR_REQUIRED_VERSION = version.parse("2.0.0")
NEURAL_COMPRESSOR_REQUIRED_VERSION = version.parse("1.14.2")


if _neural_compressor_version < NEURAL_COMPRESSOR_REQUIRED_VERSION:
    raise ImportError(
        f"Found an incompatible version of neural-compressor. Found version {_neural_compressor_version}, "
        f"but only version {NEURAL_COMPRESSOR_REQUIRED_VERSION} is supported."
    )


class IncQuantizationMode(Enum):

    DYNAMIC = "post_training_dynamic_quant"
    STATIC = "post_training_static_quant"
    AWARE_TRAINING = "quant_aware_training"


SUPPORTED_QUANT_MODE = set([approach.value for approach in IncQuantizationMode])


class IncQuantizer:
    def __init__(
        self,
        config: Union[str, IncQuantizationConfig],
        eval_func: Optional[Callable],
        train_func: Optional[Callable] = None,
        calib_dataloader: Optional[DataLoader] = None,
        calib_func: Optional[Callable] = None,
    ):
        """
        Arguments:
            config (`Union[str, IncQuantizationConfig]`):
                Path to the YAML configuration file or an instance of the class :class:`IncQuantizationConfig`, used to
                control the tuning behavior.
            eval_func (`Callable`):
                Evaluation function to evaluate the tuning objective.
            train_func (`Callable`, *optional*):
                Training function for quantization aware training approach.
            calib_dataloader (`DataLoader`, *optional*):
                DataLoader for post-training quantization calibration.
            calib_func (`Callable`):
                Calibration function for post training static quantization, If user specifies calib_func, calib_dataloader is also needed for PyTorch>1.12.
        """

        self.config = config.config if isinstance(config, IncQuantizationConfig) else Quantization_Conf(config)
        self.approach = IncQuantizationMode(self.config.usr_cfg.quantization.approach)
        self.eval_func = eval_func
        self.train_func = train_func
        self.calib_func = calib_func
        if calib_dataloader is not None:
            calib_dataloader = INCDataLoader.from_pytorch_dataloader(calib_dataloader)
        self.calib_dataloader = calib_dataloader

        if self.config.usr_cfg.model.framework == "pytorch_fx":
            neural_compressor.adaptor.pytorch._cfgs_to_fx_cfgs = _cfgs_to_fx_cfgs

        self.quantization = Quantization(self.config)

        self.quantization.eval_func = self.eval_func

        if self.approach == IncQuantizationMode.STATIC:
            if self.calib_func is not None:
                self.quantization.calib_func = self.calib_func
            if self.calib_dataloader is not None:
                self.quantization._calib_dataloader = self.calib_dataloader

        if self.config.usr_cfg.model.framework == "pytorch_ipex":
            raise ValueError("INC IPEX only is not currently supported.")

        if self.approach == IncQuantizationMode.AWARE_TRAINING:
            if self.train_func is None:
                raise ValueError("train_func must be provided for quantization aware training.")
            self.quantization.q_func = self.train_func
            if not is_torch_less_than_1_13:
                if self.calib_dataloader is None:
                    raise ValueError(
                        "For quantization aware training, a calibration dataloader `calib_dataloader` must be provided for PyTorch 1.13 or above."
                    )
                self.quantization._calib_dataloader = self.calib_dataloader


class INCQuantizer(OptimumQuantizer):
    """
    Handle the Neural Compressor quantization process.
    """

    def __init__(self, model: torch.nn.Module, **kwargs):
        """
        Args:
            model (`torch.nn.Module`):
                The model to quantize.
            seed (`int`, defaults to 42):
                The random seed to use when shuffling the calibration dataset.
        """
        super().__init__()
        self.model = model
        self.seed = kwargs.pop("seed", 42)
        self.feature = kwargs.pop("feature", None)
        signature = inspect.signature(self.model.forward)
        self._signature_columns = list(signature.parameters.keys())
        self.input_names = None

    @classmethod
    def from_pretrained(cls, model: PreTrainedModel, **kwargs):
        # TODO : Create model
        return cls(model, **kwargs)

    def quantize(
        self,
        save_directory: Union[str, Path],
        quantization_config: PostTrainingConfig,
        calibration_dataset: Dataset = None,
        file_name: Optional[str] = None,
        batch_size: int = 8,
        data_collator: Optional[DataCollator] = None,
        remove_unused_columns: bool = True,
        **kwargs,
    ):
        """
        Quantize a model given the optimization specifications defined in `quantization_config`.

        Args:
            calibration_dataset (`datasets.Dataset`):
                The dataset to use for the calibration step.
            save_directory (`Union[str, Path]`):
                The directory where the quantized model should be saved.
            quantization_config (`PostTrainingConfig`, *optional*):
                The configuration containing the parameters related to quantization.
            file_name (`str`, *optional*):
                The model file name to use when saving the model. Overwrites the default file name `"pytorch_model.bin"`.
            batch_size (`int`, defaults to 8):
                The number of calibration samples to load per batch.
            data_collator (`DataCollator`, *optional*):
                The function to use to form a batch from a list of elements of the calibration dataset.
            remove_unused_columns (`bool`, defaults to `True`):
                Whether or not to remove the columns unused by the model forward method.
        """
        save_directory = Path(save_directory)
        save_directory.mkdir(parents=True, exist_ok=True)
        save_onnx_model = kwargs.pop("save_onnx_model", False)
        file_name = file_name if file_name is not None else WEIGHTS_NAME
        output_path = save_directory.joinpath(file_name)
        calibration_dataloader = None

        if quantization_config.approach == "post_training_static_quant":
            if calibration_dataset is None:
                raise ValueError("Post-training static quantization needs a calibration dataset.")
            calibration_dataloader = self._get_calibration_dataloader(
                calibration_dataset=calibration_dataset,
                batch_size=batch_size,
                remove_unused_columns=remove_unused_columns,
                data_collator=data_collator,
            )

        compressed_model = quantization.fit(
            model=self.model, conf=quantization_config, calib_dataloader=calibration_dataloader
        )

        if isinstance(self.model.config, PretrainedConfig):
            self.model.config.save_pretrained(save_directory)

        if save_onnx_model:
            self._set_feature()
            model_type = self.model.config.model_type.replace("_", "-")
            model_name = getattr(self.model, "name", None)
            onnx_config_constructor = TasksManager.get_exporter_config_constructor(
                model_type, "onnx", task=self.feature, model_name=model_name
            )
            onnx_config = onnx_config_constructor(self.model.config)
            compressed_model.eval()
            output_onnx_path = save_directory.joinpath("model.onnx")
            # Export the compressed model to the ONNX format
            self._onnx_export(compressed_model, onnx_config, output_onnx_path, calibration_dataloader)

        # Save the quantized model
        self._save_pretrained(compressed_model, output_path)
        # TODO : Save quantization_config

    @staticmethod
    def _save_pretrained(model: PyTorchModel, output_path: str):
        state_dict = model._model.state_dict()
        if hasattr(model, "q_config"):
            state_dict["best_configure"] = model.q_config
        torch.save(state_dict, output_path)
        logger.info(f"Model weights saved to {output_path}")

    def _onnx_export(
        self,
        model: PyTorchModel,
        config: OnnxConfig,
        output_path: Union[str, Path],
        calibration_dataloader: INCDataLoader = None,
    ):
        opset = min(config.DEFAULT_ONNX_OPSET, MIN_QDQ_ONNX_OPSET)
        dynamic_axes = {name: axes for name, axes in chain(config.inputs.items(), config.outputs.items())}
        inputs = config.generate_dummy_inputs(framework="pt")
        model.export_to_int8_onnx(
            save_path=str(output_path),
            example_inputs=inputs,
            opset_version=opset,
            dynamic_axes=dynamic_axes,
            fp32_model=self.model,
            calib_dataloader=calibration_dataloader,
        )

    def _set_feature(self):
        if self.feature is None:
            self.feature = HfApi().model_info(self.model.config._name_or_path).pipeline_tag
            if self.feature in ["sentiment-analysis", "text-classification", "zero-shot-classification"]:
                self.feature = "sequence-classification"
            elif self.feature in ["feature-extraction", "fill-mask"]:
                self.feature = "default"
            elif self.feature is None:
                raise ValueError("The feature could not be extracted and needs to be specified for the ONNX export.")
        if self.feature in ["seq2seq-lm", "translation", "summarization"]:
            raise ValueError(f"Seq2Seq models are currently not supported for post-training static quantization.")

    def get_calibration_dataset(
        self,
        dataset_name: str,
        num_samples: int = 100,
        dataset_config_name: Optional[str] = None,
        dataset_split: str = "train",
        preprocess_function: Optional[Callable] = None,
        preprocess_batch: bool = True,
        use_auth_token: bool = False,
    ) -> Dataset:
        """
        Create the calibration `datasets.Dataset` to use for the post-training static quantization calibration step.

        Args:
            dataset_name (`str`):
                The dataset repository name on the Hugging Face Hub or path to a local directory containing data files
                in generic formats and optionally a dataset script, if it requires some code to read the data files.
            num_samples (`int`, defaults to 100):
                The maximum number of samples composing the calibration dataset.
            dataset_config_name (`str`, *optional*):
                The name of the dataset configuration.
            dataset_split (`str`, defaults to `"train"`):
                Which split of the dataset to use to perform the calibration step.
            preprocess_function (`Callable`, *optional*):
                Processing function to apply to each example after loading dataset.
            preprocess_batch (`bool`, defaults to `True`):
                Whether the `preprocess_function` should be batched.
            use_auth_token (`bool`, defaults to `False`):
                Whether to use the token generated when running `transformers-cli login`.
        Returns:
            The calibration `datasets.Dataset` to use for the post-training static quantization calibration step.
        """
        calibration_dataset = load_dataset(
            dataset_name,
            name=dataset_config_name,
            split=dataset_split,
            use_auth_token=use_auth_token,
        )

        if num_samples is not None:
            num_samples = min(num_samples, len(calibration_dataset))
            calibration_dataset = calibration_dataset.shuffle(seed=self.seed).select(range(num_samples))

        if preprocess_function is not None:
            calibration_dataset = calibration_dataset.map(preprocess_function, batched=preprocess_batch)

        return calibration_dataset

    def _get_calibration_dataloader(
        self,
        calibration_dataset: Dataset,
        batch_size: int,
        remove_unused_columns: bool,
        data_collator: Optional[DataCollator] = None,
    ) -> INCDataLoader:
        data_collator = data_collator if data_collator is not None else default_data_collator
        if remove_unused_columns:
            calibration_dataset = self._remove_unused_columns(calibration_dataset)
        self.input_names = calibration_dataset.column_names
        generator = torch.Generator()
        generator.manual_seed(self.seed)
        sampler = RandomSampler(calibration_dataset, generator=generator)
        calibration_dataloader = DataLoader(
            calibration_dataset,
            batch_size=batch_size,
            sampler=sampler,
            collate_fn=data_collator,
            drop_last=False,
        )

        return INCDataLoader.from_pytorch_dataloader(calibration_dataloader)

    def _remove_unused_columns(self, dataset: Dataset):
        ignored_columns = list(set(dataset.column_names) - set(self._signature_columns))
        return dataset.remove_columns(ignored_columns)


# Adapted from https://github.com/intel/neural-compressor/blob/master/neural_compressor/utils/pytorch.py#L96
def apply_quantization_from_config(q_config: Dict, model: torch.nn.Module) -> torch.nn.Module:
    """
    Apply Intel Neural Compressor quantization steps on the given model.

    Arguments:
        q_config (`Dict`):
            Dictionary containing all quantization information such as approach, dtype, scheme and granularity.
        model (`torch.nn.Module`):
            Model to quantize.
    Returns:
        q_model (`torch.nn.Module`):
            Quantized model.
    """
    approach = q_config.get("approach")
    framework = q_config.get("framework")

    if approach not in SUPPORTED_QUANT_MODE:
        raise ValueError(
            "Unknown quantization approach. Supported approach are " + ", ".join(SUPPORTED_QUANT_MODE.keys())
        )

    quant_mode = IncQuantizationMode(approach)
    q_model = copy.deepcopy(model)
    q_model.eval()

    if framework == "pytorch_fx":
        op_cfgs = _cfg_to_qconfig(q_config, approach)
        fx_op_cfgs = _cfgs_to_fx_cfgs(op_cfgs, approach)

        if not q_config["fx_sub_module_list"]:
            if quant_mode == IncQuantizationMode.AWARE_TRAINING:
                q_model.train()
                q_model = prepare_qat_fx(q_model, fx_op_cfgs)
            else:
                q_model = prepare_fx(q_model, fx_op_cfgs)
            q_model = convert_fx(q_model)

        else:
            sub_module_list = q_config["fx_sub_module_list"]
            if q_config["approach"] == "quant_aware_training":
                q_model.train()
                PyTorch_FXAdaptor.prepare_sub_graph(sub_module_list, fx_op_cfgs, q_model, prefix="", is_qat=True)
            else:
                PyTorch_FXAdaptor.prepare_sub_graph(sub_module_list, fx_op_cfgs, q_model, prefix="")
            PyTorch_FXAdaptor.convert_sub_graph(sub_module_list, q_model, prefix="")

    else:
        if quant_mode == IncQuantizationMode.DYNAMIC:
            q_mapping = torch.quantization.quantization_mappings.get_default_dynamic_quant_module_mappings()
            op_cfgs = _cfg_to_qconfig(q_config, approach)
        else:
            q_mapping = torch.quantization.quantization_mappings.get_default_static_quant_module_mappings()
            op_cfgs = _cfg_to_qconfig(q_config)

        _propagate_qconfig(q_model, op_cfgs, approach=approach)

        if quant_mode != IncQuantizationMode.DYNAMIC:
            add_observer_(q_model)
        q_model = convert(q_model, mapping=q_mapping, inplace=True)

    return q_model


class IncQuantizedModel:

    TRANSFORMERS_AUTO_CLASS: ClassVar = AutoModel

    def __init__(self, *args, **kwargs):
        raise EnvironmentError(
            f"{self.__class__.__name__} is designed to be instantiated using the"
            f"`{self.__class__.__name__}.from_pretrained(model_name_or_path)` method."
        )

    @classmethod
    def from_pretrained(
        cls,
        model_name_or_path: str,
        inc_config: Union[IncOptimizedConfig, str] = None,
        q_model_name: Optional[str] = None,
        **kwargs
    ) -> torch.nn.Module:
        """
        Instantiate a quantized pytorch model from a given Intel Neural Compressor configuration file.
        Arguments:
            model_name_or_path (`str`):
                Repository name in the Hugging Face Hub or path to a local directory hosting the model.
            inc_config (`Union[IncOptimizedConfig, str]`, *optional*):
                Configuration file containing all the information related to the model quantization.
                Can be either:
                    - an instance of the class :class:`IncOptimizedConfig`,
                    - a string valid as input to :func:`IncOptimizedConfig.from_pretrained`.
            q_model_name (`str`, *optional*):
                Name of the state dictionary located in model_name_or_path used to load the quantized model. If
                state_dict is specified, the latter will not be used.
            cache_dir (`str`, *optional*):
                Path to a directory in which a downloaded configuration should be cached if the standard cache should
                not be used.
            force_download (`bool`, *optional*, defaults to `False`):
                Whether or not to force to (re-)download the configuration files and override the cached versions if
                they exist.
            resume_download (`bool`, *optional*, defaults to `False`):
                Whether or not to delete incompletely received file. Attempts to resume the download if such a file
                exists.
            revision(`str`, *optional*):
                The specific model version to use. It can be a branch name, a tag name, or a commit id, since we use a
                git-based system for storing models and other artifacts on huggingface.co, so ``revision`` can be any
                identifier allowed by git.
            state_dict (`Dict[str, torch.Tensor]`, *optional*):
                State dictionary of the quantized model, if not specified q_model_name will be used to load the
                state dictionary.
        Returns:
            q_model: Quantized model.
        """
        download_kwarg_default = [
            ("cache_dir", None),
            ("force_download", False),
            ("resume_download", False),
            ("revision", None),
        ]
        download_kwargs = {name: kwargs.get(name, default_value) for (name, default_value) in download_kwarg_default}
        state_dict = kwargs.get("state_dict", None)

        config = AutoConfig.from_pretrained(model_name_or_path)
        model_class = _get_model_class(config, cls.TRANSFORMERS_AUTO_CLASS._model_mapping)
        keys_to_ignore_on_load_unexpected = copy.deepcopy(
            getattr(model_class, "_keys_to_ignore_on_load_unexpected", None)
        )
        keys_to_ignore_on_load_missing = copy.deepcopy(getattr(model_class, "_keys_to_ignore_on_load_missing", None))
        # Avoid unnecessary warnings resulting from quantized model initialization
        quantized_keys_to_ignore_on_load = [
            r"zero_point",
            r"scale",
            r"packed_params",
            r"constant",
            r"module",
            r"best_configure",
            r"max_val",
            r"min_val",
            r"eps",
            r"fake_quant_enabled",
            r"observer_enabled",
        ]
        if keys_to_ignore_on_load_unexpected is None:
            model_class._keys_to_ignore_on_load_unexpected = quantized_keys_to_ignore_on_load
        else:
            model_class._keys_to_ignore_on_load_unexpected.extend(quantized_keys_to_ignore_on_load)
        missing_keys_to_ignore_on_load = [r"weight", r"bias"]
        if keys_to_ignore_on_load_missing is None:
            model_class._keys_to_ignore_on_load_missing = missing_keys_to_ignore_on_load
        else:
            model_class._keys_to_ignore_on_load_missing.extend(missing_keys_to_ignore_on_load)

        model = model_class.from_pretrained(model_name_or_path, **kwargs)
        model_class._keys_to_ignore_on_load_unexpected = keys_to_ignore_on_load_unexpected
        model_class._keys_to_ignore_on_load_missing = keys_to_ignore_on_load_missing

        if state_dict is None:

            q_model_name = q_model_name if q_model_name is not None else WEIGHTS_NAME
            revision = download_kwargs.pop("revision", None)
            if os.path.isdir(model_name_or_path):
                state_dict_path = os.path.join(model_name_or_path, q_model_name)
            elif os.path.isfile(model_name_or_path):
                state_dict_path = model_name_or_path
            else:
                local_files_only = False
                if is_offline_mode():
                    logger.info("Offline mode: forcing local_files_only=True")
                    local_files_only = True
                cache_dir = download_kwargs.get("cache_dir", None)
                if cache_dir is None:
                    cache_dir = TRANSFORMERS_CACHE
                if isinstance(cache_dir, Path):
                    cache_dir = str(cache_dir)
                try:
                    state_dict_path = hf_hub_download(
                        repo_id=model_name_or_path,
                        filename=q_model_name,
                        revision=revision,
                        cache_dir=cache_dir,
                        local_files_only=local_files_only,
                    )
                except EnvironmentError as err:
                    logger.error(err)
                    msg = (
                        f"Can't load config for '{model_name_or_path}'. Make sure that:\n\n"
                        f"-'{model_name_or_path}' is a correct model identifier listed on 'https://huggingface.co/models'\n\n"
                        f"-or '{model_name_or_path}' is a correct path to a directory containing a {q_model_name} file\n\n"
                    )

                    if revision is not None:
                        msg += (
                            f"- or '{revision}' is a valid git identifier (branch name, a tag name, or a commit id) that "
                            f"exists for this model name as listed on its model page on 'https://huggingface.co/models'\n\n"
                        )

                    raise EnvironmentError(msg)

            if config.framework == "pytorch_ipex":
                raise ValueError("INC IPEX is currently not supported")

            state_dict = torch.load(state_dict_path)

        if "best_configure" in state_dict:
            inc_config = state_dict.pop("best_configure")
        elif isinstance(inc_config, IncOptimizedConfig):
            inc_config = inc_config.config
        else:
            config_path = inc_config if inc_config is not None else model_name_or_path
            inc_config = IncOptimizedConfig.from_pretrained(config_path, **download_kwargs).config

        if "is_oneshot" in inc_config and inc_config["is_oneshot"]:
            return _load_int8_orchestration(model, inc_config, state_dict)

        q_model = apply_quantization_from_config(inc_config, model)

        q_model.load_state_dict(state_dict, strict=False)

        get_embedding_contiguous(q_model)

        return q_model


class IncQuantizedModelForQuestionAnswering(IncQuantizedModel):

    TRANSFORMERS_AUTO_CLASS = AutoModelForQuestionAnswering


class IncQuantizedModelForSequenceClassification(IncQuantizedModel):

    TRANSFORMERS_AUTO_CLASS = AutoModelForSequenceClassification


class IncQuantizedModelForTokenClassification(IncQuantizedModel):

    TRANSFORMERS_AUTO_CLASS = AutoModelForTokenClassification


class IncQuantizedModelForMultipleChoice(IncQuantizedModel):

    TRANSFORMERS_AUTO_CLASS = AutoModelForMultipleChoice


class IncQuantizedModelForSeq2SeqLM(IncQuantizedModel):

    TRANSFORMERS_AUTO_CLASS = AutoModelForSeq2SeqLM


class IncQuantizedModelForCausalLM(IncQuantizedModel):

    TRANSFORMERS_AUTO_CLASS = AutoModelForCausalLM


class IncQuantizedModelForMaskedLM(IncQuantizedModel):

    TRANSFORMERS_AUTO_CLASS = AutoModelForMaskedLM


class IncQuantizedModelForXLNetLM(IncQuantizedModel):

    TRANSFORMERS_AUTO_CLASS = XLNetLMHeadModel


class IncQuantizedModelForVision2Seq(IncQuantizedModel):

    TRANSFORMERS_AUTO_CLASS = AutoModelForVision2Seq
