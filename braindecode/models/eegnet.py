# Authors: Robin Schirrmeister <robintibor@gmail.com>
#
# License: BSD (3-clause)
from __future__ import annotations

from typing import Dict, Optional

from einops.layers.torch import Rearrange
from mne.utils import warn
from torch import nn

from braindecode.functional import glorot_weight_zero_bias
from braindecode.models.base import EEGModuleMixin
from braindecode.modules import (
    Conv2dWithConstraint,
    Ensure4d,
    Expression,
    LinearWithConstraint,
    SqueezeFinalOutput,
)


class EEGNetv4(EEGModuleMixin, nn.Sequential):
    """EEGNet v4 model from Lawhern et al. (2018) [EEGNet4]_.

    .. figure:: https://content.cld.iop.org/journals/1741-2552/15/5/056013/revision2/jneaace8cf01_hr.jpg
       :align: center
       :alt: EEGNet4 Architecture

    See details in [EEGNet4]_.

    Parameters
    ----------
    final_conv_length : int or "auto", default="auto"
        Length of the final convolution layer. If "auto", it is set based on n_times.
    pool_mode : {"mean", "max"}, default="mean"
        Pooling method to use in pooling layers.
    F1 : int, default=8
        Number of temporal filters in the first convolutional layer.
    D : int, default=2
        Depth multiplier for the depthwise convolution.
    F2 : int or None, default=None
        Number of pointwise filters in the separable convolution. Usually set to ``F1 * D``.
    depthwise_kernel_length : int, default=16
        Length of the depthwise convolution kernel in the separable convolution.
    pool1_kernel_size : int, default=4
        Kernel size of the first pooling layer.
    pool2_kernel_size : int, default=8
        Kernel size of the second pooling layer.
    kernel_length : int, default=64
        Length of the temporal convolution kernel.
    conv_spatial_max_norm : float, default=1
        Maximum norm constraint for the spatial (depthwise) convolution.
    activation : nn.Module, default=nn.ELU
        Non-linear activation function to be used in the layers.
    batch_norm_momentum : float, default=0.01
        Momentum for instance normalization in batch norm layers.
    batch_norm_affine : bool, default=True
        If True, batch norm has learnable affine parameters.
    batch_norm_eps : float, default=1e-3
        Epsilon for numeric stability in batch norm layers.
    drop_prob : float, default=0.25
        Dropout probability.
    final_layer_with_constraint : bool, default=False
        If ``False``, uses a convolution-based classification layer. If ``True``,
        apply a flattened linear layer with constraint on the weights norm as the final classification step.
    norm_rate : float, default=0.25
        Max-norm constraint value for the linear layer (used if ``final_layer_conv=False``).

    References
    ----------
    .. [EEGNet4] Lawhern, V. J., Solon, A. J., Waytowich, N. R., Gordon, S. M.,
        Hung, C. P., & Lance, B. J. (2018). EEGNet: a compact convolutional
        neural network for EEG-based brain–computer interfaces. Journal of
        neural engineering, 15(5), 056013.
    """

    def __init__(
        self,
        # signal's parameters
        n_chans: Optional[int] = None,
        n_outputs: Optional[int] = None,
        n_times: Optional[int] = None,
        # model's parameters
        final_conv_length: str | int = "auto",
        pool_mode: str = "mean",
        F1: int = 8,
        D: int = 2,
        F2: Optional[int | None] = None,
        kernel_length: int = 64,
        *,
        depthwise_kernel_length: int = 16,
        pool1_kernel_size: int = 4,
        pool2_kernel_size: int = 8,
        conv_spatial_max_norm: int = 1,
        activation: nn.Module = nn.ELU,
        batch_norm_momentum: float = 0.01,
        batch_norm_affine: bool = True,
        batch_norm_eps: float = 1e-3,
        drop_prob: float = 0.25,
        final_layer_with_constraint: bool = False,
        norm_rate: float = 0.25,
        # Other ways to construct the signal related parameters
        chs_info: Optional[list[Dict]] = None,
        input_window_seconds=None,
        sfreq=None,
        **kwargs,
    ):
        super().__init__(
            n_outputs=n_outputs,
            n_chans=n_chans,
            chs_info=chs_info,
            n_times=n_times,
            input_window_seconds=input_window_seconds,
            sfreq=sfreq,
        )
        del n_outputs, n_chans, chs_info, n_times, input_window_seconds, sfreq
        if final_conv_length == "auto":
            assert self.n_times is not None

        if not final_layer_with_constraint:
            warn(
                "Parameter 'final_layer_with_constraint=False' is deprecated and will be "
                "removed in a future release. Please use `final_layer_linear=True`.",
                DeprecationWarning,
            )

        if "third_kernel_size" in kwargs:
            warn(
                "The parameter `third_kernel_size` is deprecated "
                "and will be removed in a future version.",
            )
        unexpected_kwargs = set(kwargs) - {"third_kernel_size"}
        if unexpected_kwargs:
            raise TypeError(f"Unexpected keyword arguments: {unexpected_kwargs}")

        self.final_conv_length = final_conv_length
        self.pool_mode = pool_mode
        self.F1 = F1
        self.D = D

        if F2 is None:
            F2 = self.F1 * self.D
        self.F2 = F2

        self.kernel_length = kernel_length
        self.depthwise_kernel_length = depthwise_kernel_length
        self.pool1_kernel_size = pool1_kernel_size
        self.pool2_kernel_size = pool2_kernel_size
        self.drop_prob = drop_prob
        self.activation = activation
        self.batch_norm_momentum = batch_norm_momentum
        self.batch_norm_affine = batch_norm_affine
        self.batch_norm_eps = batch_norm_eps
        self.conv_spatial_max_norm = conv_spatial_max_norm
        self.norm_rate = norm_rate

        # For the load_state_dict
        # When padronize all layers,
        # add the old's parameters here
        self.mapping = {
            "conv_classifier.weight": "final_layer.conv_classifier.weight",
            "conv_classifier.bias": "final_layer.conv_classifier.bias",
        }

        pool_class = dict(max=nn.MaxPool2d, mean=nn.AvgPool2d)[self.pool_mode]
        self.add_module("ensuredims", Ensure4d())

        self.add_module("dimshuffle", Rearrange("batch ch t 1 -> batch 1 ch t"))
        self.add_module(
            "conv_temporal",
            nn.Conv2d(
                1,
                self.F1,
                (1, self.kernel_length),
                bias=False,
                padding=(0, self.kernel_length // 2),
            ),
        )
        self.add_module(
            "bnorm_temporal",
            nn.BatchNorm2d(
                self.F1,
                momentum=self.batch_norm_momentum,
                affine=self.batch_norm_affine,
                eps=self.batch_norm_eps,
            ),
        )
        self.add_module(
            "conv_spatial",
            Conv2dWithConstraint(
                in_channels=self.F1,
                out_channels=self.F1 * self.D,
                kernel_size=(self.n_chans, 1),
                max_norm=self.conv_spatial_max_norm,
                bias=False,
                groups=self.F1,
            ),
        )

        self.add_module(
            "bnorm_1",
            nn.BatchNorm2d(
                self.F1 * self.D,
                momentum=self.batch_norm_momentum,
                affine=self.batch_norm_affine,
                eps=self.batch_norm_eps,
            ),
        )
        self.add_module("elu_1", activation())

        self.add_module(
            "pool_1",
            pool_class(
                kernel_size=(1, self.pool1_kernel_size),
            ),
        )
        self.add_module("drop_1", nn.Dropout(p=self.drop_prob))

        # https://discuss.pytorch.org/t/how-to-modify-a-conv2d-to-depthwise-separable-convolution/15843/7
        self.add_module(
            "conv_separable_depth",
            nn.Conv2d(
                self.F1 * self.D,
                self.F1 * self.D,
                (1, self.depthwise_kernel_length),
                bias=False,
                groups=self.F1 * self.D,
                padding=(0, self.depthwise_kernel_length // 2),
            ),
        )
        self.add_module(
            "conv_separable_point",
            nn.Conv2d(
                self.F1 * self.D,
                self.F2,
                kernel_size=(1, 1),
                bias=False,
            ),
        )

        self.add_module(
            "bnorm_2",
            nn.BatchNorm2d(
                self.F2,
                momentum=self.batch_norm_momentum,
                affine=self.batch_norm_affine,
                eps=self.batch_norm_eps,
            ),
        )
        self.add_module("elu_2", self.activation())
        self.add_module(
            "pool_2",
            pool_class(
                kernel_size=(1, self.pool2_kernel_size),
            ),
        )
        self.add_module("drop_2", nn.Dropout(p=self.drop_prob))

        output_shape = self.get_output_shape()
        n_out_virtual_chans = output_shape[2]

        if self.final_conv_length == "auto":
            n_out_time = output_shape[3]
            self.final_conv_length = n_out_time

        # Incorporating classification module and subsequent ones in one final layer
        module = nn.Sequential()
        if not final_layer_with_constraint:
            module.add_module(
                "conv_classifier",
                nn.Conv2d(
                    self.F2,
                    self.n_outputs,
                    (n_out_virtual_chans, self.final_conv_length),
                    bias=True,
                ),
            )

            # Transpose back to the logic of braindecode,
            # so time in third dimension (axis=2)
            module.add_module(
                "permute_back",
                Rearrange("batch x y z -> batch x z y"),
            )

            module.add_module("squeeze", SqueezeFinalOutput())
        else:
            module.add_module("flatten", nn.Flatten())
            module.add_module(
                "linearconstraint",
                LinearWithConstraint(
                    in_features=self.F2 * self.final_conv_length,
                    out_features=self.n_outputs,
                    max_norm=norm_rate,
                ),
            )
        self.add_module("final_layer", module)

        glorot_weight_zero_bias(self)


class EEGNetv1(EEGModuleMixin, nn.Sequential):
    """EEGNet model from Lawhern et al. 2016 from [EEGNet]_.

    See details in [EEGNet]_.

    Parameters
    ----------
    in_chans :
        Alias for n_chans.
    n_classes:
        Alias for n_outputs.
    input_window_samples :
        Alias for n_times.
    activation: nn.Module, default=nn.ELU
        Activation function class to apply. Should be a PyTorch activation
        module class like ``nn.ReLU`` or ``nn.ELU``. Default is ``nn.ELU``.

    Notes
    -----
    This implementation is not guaranteed to be correct, has not been checked
    by original authors, only reimplemented from the paper description.

    References
    ----------
    .. [EEGNet] Lawhern, V. J., Solon, A. J., Waytowich, N. R., Gordon,
       S. M., Hung, C. P., & Lance, B. J. (2016).
       EEGNet: A Compact Convolutional Network for EEG-based
       Brain-Computer Interfaces.
       arXiv preprint arXiv:1611.08024.
    """

    def __init__(
        self,
        n_chans=None,
        n_outputs=None,
        n_times=None,
        final_conv_length="auto",
        pool_mode="max",
        second_kernel_size=(2, 32),
        third_kernel_size=(8, 4),
        drop_prob=0.25,
        activation: nn.Module = nn.ELU,
        chs_info=None,
        input_window_seconds=None,
        sfreq=None,
    ):
        super().__init__(
            n_outputs=n_outputs,
            n_chans=n_chans,
            chs_info=chs_info,
            n_times=n_times,
            input_window_seconds=input_window_seconds,
            sfreq=sfreq,
        )
        del n_outputs, n_chans, chs_info, n_times, input_window_seconds, sfreq
        warn(
            "The class EEGNetv1 is deprecated and will be removed in the "
            "release 1.0 of braindecode. Please use "
            "braindecode.models.EEGNetv4 instead in the future.",
            DeprecationWarning,
        )
        if final_conv_length == "auto":
            assert self.n_times is not None
        self.final_conv_length = final_conv_length
        self.pool_mode = pool_mode
        self.second_kernel_size = second_kernel_size
        self.third_kernel_size = third_kernel_size
        self.drop_prob = drop_prob
        # For the load_state_dict
        # When padronize all layers,
        # add the old's parameters here
        self.mapping = {
            "conv_classifier.weight": "final_layer.conv_classifier.weight",
            "conv_classifier.bias": "final_layer.conv_classifier.bias",
        }

        pool_class = dict(max=nn.MaxPool2d, mean=nn.AvgPool2d)[self.pool_mode]
        self.add_module("ensuredims", Ensure4d())
        n_filters_1 = 16
        self.add_module(
            "conv_1",
            nn.Conv2d(self.n_chans, n_filters_1, (1, 1), stride=1, bias=True),
        )
        self.add_module(
            "bnorm_1",
            nn.BatchNorm2d(n_filters_1, momentum=0.01, affine=True, eps=1e-3),
        )
        self.add_module("elu_1", activation())
        # transpose to examples x 1 x (virtual, not EEG) channels x time
        self.add_module("permute_1", Rearrange("batch x y z -> batch z x y"))

        self.add_module("drop_1", nn.Dropout(p=self.drop_prob))

        n_filters_2 = 4
        # keras pads unequal padding more in front, so padding
        # too large should be ok.
        # Not padding in time so that cropped training makes sense
        # https://stackoverflow.com/questions/43994604/padding-with-even-kernel-size-in-a-convolutional-layer-in-keras-theano

        self.add_module(
            "conv_2",
            nn.Conv2d(
                1,
                n_filters_2,
                self.second_kernel_size,
                stride=1,
                padding=(self.second_kernel_size[0] // 2, 0),
                bias=True,
            ),
        )
        self.add_module(
            "bnorm_2",
            nn.BatchNorm2d(n_filters_2, momentum=0.01, affine=True, eps=1e-3),
        )
        self.add_module("elu_2", activation())
        self.add_module("pool_2", pool_class(kernel_size=(2, 4), stride=(2, 4)))
        self.add_module("drop_2", nn.Dropout(p=self.drop_prob))

        n_filters_3 = 4
        self.add_module(
            "conv_3",
            nn.Conv2d(
                n_filters_2,
                n_filters_3,
                self.third_kernel_size,
                stride=1,
                padding=(self.third_kernel_size[0] // 2, 0),
                bias=True,
            ),
        )
        self.add_module(
            "bnorm_3",
            nn.BatchNorm2d(n_filters_3, momentum=0.01, affine=True, eps=1e-3),
        )
        self.add_module("elu_3", activation())
        self.add_module("pool_3", pool_class(kernel_size=(2, 4), stride=(2, 4)))
        self.add_module("drop_3", nn.Dropout(p=self.drop_prob))

        output_shape = self.get_output_shape()
        n_out_virtual_chans = output_shape[2]

        if self.final_conv_length == "auto":
            n_out_time = output_shape[3]
            self.final_conv_length = n_out_time

        # Incorporating classification module and subsequent ones in one final layer
        module = nn.Sequential()

        module.add_module(
            "conv_classifier",
            nn.Conv2d(
                n_filters_3,
                self.n_outputs,
                (n_out_virtual_chans, self.final_conv_length),
                bias=True,
            ),
        )

        # Transpose back to the logic of braindecode,

        # so time in third dimension (axis=2)
        module.add_module(
            "permute_2",
            Rearrange("batch x y z -> batch x z y"),
        )

        module.add_module("squeeze", SqueezeFinalOutput())

        self.add_module("final_layer", module)

        glorot_weight_zero_bias(self)
