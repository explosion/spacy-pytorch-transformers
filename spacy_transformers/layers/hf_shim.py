from typing import Any, Dict
from io import BytesIO
from pathlib import Path
import srsly
from functools import partial
import torch
from dataclasses import dataclass, field
from spacy.vectors import get_current_ops

from ..util import make_tempdir

from thinc.api import PyTorchShim

from transformers import AutoModel, AutoConfig, AutoTokenizer


@dataclass
class HFObjects:

    transformer: Any
    tokenizer: Any
    tokenizer_config: Dict[str, Any] = field(default_factory=dict)
    transformer_config: Dict[str, Any] = field(default_factory=dict)


class HFShim(PyTorchShim):
    """Interface between a HF Pytorch model and a Thinc Model."""

    def __init__(self, model: HFObjects, config=None, optimizer: Any = None):
        self._hfmodel = model
        super().__init__(model.transformer, config, optimizer)

    def to_bytes(self):
        config = {}
        tok_dict = {}
        weights_bytes = {}
        hf_model = self._hfmodel
        if hf_model.transformer is not None:
            tok_dict = {}
            config = hf_model.transformer.config.to_dict()
            tokenizer = hf_model.tokenizer
            with make_tempdir() as temp_dir:
                tokenizer.save_pretrained(temp_dir)
                for x in temp_dir.glob("**/*"):
                    if x.is_file():
                        tok_dict[x.name] = x.read_bytes()
            filelike = BytesIO()
            torch.save(self._model.state_dict(), filelike)
            filelike.seek(0)
            weights_bytes = filelike.getvalue()
        msg = {
            "config": config,
            "state": weights_bytes,
            "tokenizer": tok_dict,
            "tokenizer_config": hf_model.tokenizer_config,
            "transformer_config": hf_model.transformer_config,
        }
        return srsly.msgpack_dumps(msg)

    def from_bytes(self, bytes_data):
        msg = srsly.msgpack_loads(bytes_data)
        config_dict = msg["config"]
        tok_dict = msg["tokenizer"]
        tok_config = msg["tokenizer_config"]
        trf_config = msg["transformer_config"]
        if config_dict:
            with make_tempdir() as temp_dir:
                config_file = temp_dir / "config.json"
                srsly.write_json(config_file, config_dict)
                config = AutoConfig.from_pretrained(config_file)
                for x, x_bytes in tok_dict.items():
                    Path(temp_dir / x).write_bytes(x_bytes)
                tokenizer = AutoTokenizer.from_pretrained(
                    str(temp_dir.absolute()), **tok_config
                )

            transformer = AutoModel.from_config(config)
            transformer.forward = partial(transformer.forward, **trf_config)
            self._hfmodel = HFObjects(transformer, tokenizer, tok_config, trf_config)
            self._model = transformer
            filelike = BytesIO(msg["state"])
            filelike.seek(0)
            ops = get_current_ops()
            if ops.device_type == "cpu":
                map_location = "cpu"
            else:  # pragma: no cover
                device_id = torch.cuda.current_device()
                map_location = f"cuda:{device_id}"
            self._model.load_state_dict(torch.load(filelike, map_location=map_location))
            self._model.to(map_location)
        return self
