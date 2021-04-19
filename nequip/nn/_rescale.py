from typing import Sequence, List, Union

import torch

from e3nn.util.jit import compile_mode

from nequip.data import AtomicDataDict
from nequip.nn import GraphModuleMixin


@compile_mode("script")
class RescaleOutput(GraphModuleMixin, torch.nn.Module):
    """Wrap a model and rescale its outputs when in ``eval()`` mode.

    Args:
        model : GraphModuleMixin
            The model whose outputs are to be rescaled.
        scale : list of keys, default []
            Which fields to rescale.
        shift : list of keys, default []
            Which fields to shift after rescaling.
        scale_by : floating or Tensor, default 1.
            The scaling factor by which to multiply fields in ``scale``.
        shift_by : floating or Tensor, default 0.
            The shift to add to fields in ``shift``.
        irreps_in : dict, optional
            Extra inputs expected by this beyond those of `model`; this is only present for compatibility.
    """

    scale_keys: List[str]
    shift_keys: List[str]
    _has_scale: bool
    _has_shift: bool

    def __init__(
        self,
        model: GraphModuleMixin,
        scale_keys: Union[Sequence[str], str] = [],
        shift_keys: Union[Sequence[str], str] = [],
        scale_by=None,
        shift_by=None,
        trainable_global_rescale_shift: bool = False,
        trainable_global_rescale_scale: bool = False,
        irreps_in: dict = {},
    ):
        super().__init__()
        self.model = model
        scale_keys = [scale_keys] if isinstance(scale_keys, str) else scale_keys
        shift_keys = [shift_keys] if isinstance(shift_keys, str) else shift_keys
        all_keys = set(scale_keys).union(shift_keys)

        # Check irreps:
        for k in irreps_in:
            if k in model.irreps_in and model.irreps_in[k] != irreps_in[k]:
                raise ValueError(
                    f"For field '{k}', the provided explicit `irreps_in` ('{k}': {irreps_in[k]}) are incompataible with those of the wrapped `model` ('{k}': {model.irreps_in[k]})"
                )
        for k in all_keys:
            if k not in model.irreps_out:
                raise KeyError(
                    f"Asked to scale or shift '{k}', but '{k}' is not in the outputs of the provided `model`."
                )
        for k in shift_keys:
            if model.irreps_out[k] is not None and model.irreps_out[k].lmax > 0:
                raise ValueError(
                    f"It doesn't make sense to shift non-scalar target '{k}'."
                )

        irreps_in.update(model.irreps_in)
        self._init_irreps(irreps_in=irreps_in, irreps_out=model.irreps_out)

        self.scale_keys = list(scale_keys)
        self.shift_keys = list(shift_keys)

        self._has_scale = scale_by is not None
        self.trainable_global_rescale_scale = trainable_global_rescale_scale
        if self._has_scale:
            scale_by = torch.as_tensor(scale_by)
            if self.trainable_global_rescale_scale:
                self.scale_by = torch.nn.Parameter(scale_by)
            else:
                self.register_buffer("scale_by", scale_by)
        elif self.trainable_global_rescale_scale:
            raise ValueError(
                "Asked for a trainable_global_rescale_scale, but this RescaleOutput has no scaling (`scale_by = None`)"
            )

        self._has_shift = shift_by is not None
        self.trainable_global_rescale_shift = trainable_global_rescale_shift
        if self._has_shift:
            shift_by = torch.as_tensor(shift_by)
            if self.trainable_global_rescale_shift:
                self.shift_by = torch.nn.Parameter(shift_by)
            else:
                self.register_buffer("shift_by", shift_by)
        elif self.trainable_global_rescale_shift:
            raise ValueError(
                "Asked for a trainable_global_rescale_shift, but this RescaleOutput has no shift (`shift_by = None`)"
            )

    def forward(self, data: AtomicDataDict.Type) -> AtomicDataDict.Type:
        data = self.model(data)
        if self.training:
            return data
        else:
            # Scale then shift
            if self._has_scale:
                for field in self.scale_keys:
                    data[field] = data[field] * self.scale_by
            if self._has_shift:
                for field in self.shift_keys:
                    data[field] = data[field] + self.shift_by
            return data

    @torch.jit.export
    def scale(
        self,
        data: AtomicDataDict.Type,
        force_process: bool = False,
    ) -> AtomicDataDict.Type:
        """Apply rescaling to ``data``, in place.

        Only processes the data if the module is in ``eval()`` mode, unless ``force_process`` is ``True``.

        Args:
            data (map-like): a dict, ``AtomicDataDict``, ``AtomicData``, ``torch_geometric.data.Batch``, or anything else dictionary-like
            force_process (bool): if ``True``, scaling will be done regardless of whether the model is in train or evaluation mode.
        Returns:
            ``data``, modified in place
        """
        data = data.copy()
        if self.training and not force_process:
            return data
        else:
            if self._has_scale:
                for field in self.scale_keys:
                    if field in data:
                        data[field] = data[field] * self.scale_by
            if self._has_shift:
                for field in self.shift_keys:
                    if field in data:
                        data[field] = data[field] + self.shift_by
            return data

    @torch.jit.export
    def unscale(
        self,
        data: AtomicDataDict.Type,
        force_process: bool = False,
    ) -> AtomicDataDict.Type:
        """Apply the inverse of the rescaling operation to ``data``, in place.

        Only processes the data if the module is in ``train()`` mode, unless ``force_process`` is ``True``.

        Args:
            data (map-like): a dict, ``AtomicDataDict``, ``AtomicData``, ``torch_geometric.data.Batch``, or anything else dictionary-like
            force_process (bool): if ``True``, unscaling will be done regardless of whether the model is in train or evaluation mode.
        Returns:
            ``data``
        """
        data = data.copy()
        if self.training or force_process:
            # To invert, -shift then divide by scale
            if self._has_shift:
                for field in self.shift_keys:
                    if field in data:
                        data[field] = data[field] - self.shift_by
            if self._has_scale:
                for field in self.scale_keys:
                    if field in data:
                        data[field] = data[field] / self.scale_by
            return data
        else:
            return data
