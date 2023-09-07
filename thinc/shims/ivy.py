import contextlib
import itertools
from io import BytesIO
from typing import Any, Callable, Dict, Optional, Tuple, cast
import pickle

import srsly

from ..backends import CupyOps, context_pools, get_current_ops, set_gpu_allocator
from ..compat import ivy
from ..optimizers import Optimizer
from ..types import ArgsKwargs, FloatsXd
from ..util import (
    ivy2xp,
    xp2ivy,
)
from .shim import Shim


class IvyShim(Shim):
    def __init__(
        self, model: Any, config=None, serialize_model=None, deserialize_model=None
    ):
        super().__init__(model, config)
        self._serialize_model = (
            serialize_model
            if serialize_model is not None
            else default_serialize_ivy_model
        )
        self._deserialize_model = (
            deserialize_model
            if deserialize_model is not None
            else default_deserialize_ivy_model
        )

    def __call__(self, inputs, is_train: bool):
        if is_train:
            return self.begin_update(inputs)
        else:
            return self.predict(inputs)

    @property
    def device(self):
        return self._model._dev

    def predict(self, inputs: ArgsKwargs) -> Any:
        return self._model(*inputs.args, **inputs.kwargs)

    def begin_update(self, inputs: ArgsKwargs):
        def pred_and_assign(v, x):
            output = self._model(*x.args, **x.kwargs, v=v)
            return output.reshape((-1,)).sum(), output

        outs, param_grads = ivy.execute_with_gradients(
            lambda params: pred_and_assign(*params), (self._model.v, inputs)
        )
        self.param_grads = param_grads["0"]

        def backprop(d_output):
            def grad_fn_dX_dPred(x):
                return self._model(x).reshape((-1,)).sum()

            return ivy.grad(grad_fn_dX_dPred)(inputs.args[0])[0]

        return outs[1], backprop

    def update_array(self, val, param_name, optimizer):
        param, grad = optimizer(
            (self.id, param_name),
            cast(FloatsXd, ivy2xp(val)),
            cast(FloatsXd, ivy2xp(self.param_grads[param_name])),
        )
        return xp2ivy(param)

    def finish_update(self, optimizer: Optimizer):
        def _update_array(grad, param_name):
            return self.update_array(grad, param_name, optimizer)

        self._model.v.cont_map(_update_array)
        self.param_grads = None
        # assert ivy.not_equal(tmp, self._model.v)
        # self._model.v = optimizer.step(self._model.v, self.param_grads)

    @contextlib.contextmanager
    def use_params(self, params):
        key_prefix = f"ivy_{self.id}_"
        state_dict = {}
        for k, v in params.items():
            if hasattr(k, "startswith") and k.startswith(key_prefix):
                state_dict[k.replace(key_prefix, "")] = xp2ivy(v, device=self.device)
        if state_dict:
            backup = {k: v.clone() for k, v in self._model.v.items()}
            self._model.v.update(state_dict)
            yield
            self._model.v.update(backup)
        else:
            yield

    def to_device(self, device_type: str, device_id: int):
        if device_type == "cpu":
            self._model.to_device("cpu")
        elif device_type == "gpu":
            self._model.to_device("gpu:" + str(device_id))
        else:
            raise ValueError(f"Invalid device type {device_type}")

    def to_bytes(self):
        model_bytes = self._serialize_model(self._model)
        msg = {"config": self.cfg, "state": model_bytes}
        return srsly.msgpack_dumps(msg)

    def from_bytes(self, bytes_data):
        device = ivy.default_device()
        msg = srsly.msgpack_loads(bytes_data)
        self.cfg = msg["config"]
        self._model = self._deserialize_model(self._model, msg["state"], device)
        return self


def default_serialize_ivy_model(model: Any) -> bytes:
    """Serializes the parameters of the ivy model stored in the `model.v` container to bytes.

    model:
        Wrapped Ivy model.

    Returns:
        A `bytes` object that encapsulates the serialized model parameters.
    """
    filelike = BytesIO()
    pickle.dump(model.v.to_native().cont_to_dict(), filelike)
    return filelike.getvalue()


def default_deserialize_ivy_model(model: Any, state_bytes: bytes, device: str = "cpu"):
    """Deserializes the parameters of the ivy model stored in the `model.v` container from bytes.

    model:
        Wrapped Ivy model.

    Returns:
        A `bytes` object that encapsulates the serialized model parameters.
    """
    filelike = BytesIO(state_bytes)
    model.v = ivy.Container(
        pickle.load(filelike),
        rebuild_child_containers=True,
    ).to_ivy()
    ivy.to_device(model.v, device)
    return model
