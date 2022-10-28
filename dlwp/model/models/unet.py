from abc import ABC
import logging
from typing import Any, Dict, Optional, Sequence, Union

from hydra.utils import instantiate
from omegaconf import DictConfig
import torch

from dlwp.model.models.base import BaseModel
from dlwp.model.layers.cube_sphere import CubeSpherePadding, CubeSphereLayer
from dlwp.model.losses import LossOnStep

logger = logging.getLogger(__name__)


class CubeSphereUnet(BaseModel, ABC):
    def __init__(
            self,
            encoder: DictConfig,
            decoder: DictConfig,
            optimizer: DictConfig,
            scheduler: Optional[DictConfig],
            loss: DictConfig,
            input_channels: int,
            output_channels: int,
            n_constants: int,
            decoder_input_channels: int,
            input_time_dim: int,
            output_time_dim: int,
            cube_dim: int = 64,
            batch_size: Optional[int] = None
    ):
        """
        Pytorch-lightning module implementation of the Deep Learning Weather Prediction (DLWP) U-net model on the
        cube sphere grid.

        :param encoder: dictionary of instantiable parameters for the U-net encoder (see UnetEncoder docs)
        :param decoder: dictionary of instantiable parameters for the U-net decoder (see UnetDecoder docs)
        :param optimizer: dictionary of instantiable parameters for the model optimizer
        :param scheduler: dictionary of instantiable parameters for optimizer scheduler
        :param loss: dictionary of instantiable parameters for the model loss function
        :param input_channels: number of input channels expected in the input array schema. Note this should be the
            number of input variables in the data, NOT including data reshaping for the encoder part.
        :param output_channels: number of output channels expected in the output array schema, or output variables
        :param n_constants: number of optional constants expected in the input arrays. If this is zero, no constants
            should be provided as inputs to `forward`.
        :param decoder_input_channels: number of optional prescribed variables expected in the decoder input array
            for both inputs and outputs. If this is zero, no decoder inputs should be provided as inputs to `forward`.
        :param input_time_dim: number of time steps in the input array
        :param output_time_dim: number of time steps in the output array
        :param cube_dim: number of points on the side of a cube face
        :param batch_size: batch size. Provided only to correctly compute validation losses in metrics.
        """
        super().__init__(loss, batch_size=batch_size)
        self.optimizer_cfg = optimizer
        self.scheduler_cfg = scheduler
        self.input_channels = input_channels
        self.output_channels = output_channels
        self.n_constants = n_constants
        self.decoder_input_channels = decoder_input_channels
        self.input_time_dim = input_time_dim
        self.output_time_dim = output_time_dim

        # Example input arrays. Expects from data loader a sequence of (inputs, [decoder_inputs, [constants]])
        # inputs: [B, input_time_dim, input_channels, F, H, W]
        # decoder_inputs: [B, input_time_dim + output_time_dim, decoder_input_channels, F, H, W]
        # constants: [n_constants, F, H, W]
        example_input_array = [torch.randn(1, self.input_time_dim, self.input_channels, 6, cube_dim, cube_dim)]
        if self.decoder_input_channels > 0:
            example_input_array.append(
                torch.randn(1, self.input_time_dim + self.output_time_dim, self.decoder_input_channels,
                            6, cube_dim, cube_dim)
            )
        if self.n_constants > 0:
            example_input_array.append(
                torch.randn(self.n_constants, 6, cube_dim, cube_dim)
            )
        # Wrap in list so that Lightning interprets it correctly as a single arg to `forward`
        self.example_input_array = [example_input_array]

        # Make the generator
        self.generator = IterativeUnet(
            encoder=encoder,
            decoder=decoder,
            input_channels=self.input_channels,
            output_channels=self.output_channels,
            n_constants=self.n_constants,
            decoder_input_channels=self.decoder_input_channels,
            input_time_dim=self.input_time_dim,
            output_time_dim=self.output_time_dim,
        )

        self.save_hyperparameters()
        self.configure_metrics()

    def configure_metrics(self):
        """
        Build the metrics dictionary.
        """
        metrics = {
            'loss': instantiate(self.loss_cfg),
            'mse': torch.nn.MSELoss(),
            'mae': torch.nn.L1Loss()
        }
        for step in range(self.generator.integration_steps):
            metrics[f'loss_{step}'] = LossOnStep(metrics['loss'], self.input_time_dim, step)
        self.metrics = torch.nn.ModuleDict(metrics)
        self.loss = self.metrics['loss']

    def configure_optimizers(
            self
    ) -> Union[torch.optim.Optimizer, Dict[str, Any]]:
        optimizer = instantiate(self.optimizer_cfg, self.parameters())
        if self.scheduler_cfg is not None:
            scheduler = instantiate(self.scheduler_cfg, optimizer=optimizer)
            optimizer = {
                'optimizer': optimizer,
                'lr_scheduler': {
                    'scheduler': scheduler,
                    'monitor': 'val_loss',
                    'interval': 'epoch',
                    'frequency': 1,
                }
            }
        return optimizer

    def forward(self, inputs: Sequence, output_only_last=False) -> torch.Tensor:
        return self.generator(inputs, output_only_last)


class IterativeUnet(torch.nn.Module):
    def __init__(
            self,
            encoder: DictConfig,
            decoder: DictConfig,
            input_channels: int,
            output_channels: int,
            n_constants: int,
            decoder_input_channels: int,
            input_time_dim: int,
            output_time_dim: int,
    ):
        super().__init__()
        self.input_channels = input_channels
        self.output_channels = output_channels
        self.n_constants = n_constants
        self.decoder_input_channels = decoder_input_channels
        self.input_time_dim = input_time_dim
        self.output_time_dim = output_time_dim

        # Number of passes through the model, or a diagnostic model with only one output time
        self.is_diagnostic = self.output_time_dim == 1 and self.input_time_dim > 1
        if not self.is_diagnostic and (self.output_time_dim % self.input_time_dim != 0):
            raise ValueError(f"'output_time_dim' must be a multiple of 'input_time_dim' (got "
                             f"{self.output_time_dim} and {self.input_time_dim})")

        # Build the model layers
        self.encoder = instantiate(encoder, input_channels=self._compute_input_channels())
        self.encoder_depth = len(self.encoder.n_channels)
        self.decoder = instantiate(decoder, input_channels=self.encoder.n_channels,
                                   output_channels=self._compute_output_channels())

    @property
    def integration_steps(self):
        return max(self.output_time_dim // self.input_time_dim, 1)

    def _compute_input_channels(self) -> int:
        return self.input_time_dim * (self.input_channels + self.decoder_input_channels) + self.n_constants

    def _compute_output_channels(self) -> int:
        return (1 if self.is_diagnostic else self.input_time_dim) * self.output_channels

    def _reshape_inputs(self, inputs: Sequence, step: int = 0) -> torch.Tensor:
        """
        Returns a single tensor to pass into the model encoder/decoder. Squashes the time/channel dimension and
        concatenates in constants and decoder inputs.
        :param inputs: list of expected input tensors (inputs, decoder_inputs, constants)
        :param step: step number in the sequence of integration_steps
        :return: reshaped Tensor in expected shape for model encoder
        """
        if not (self.n_constants > 0 or self.decoder_input_channels > 0):
            return inputs[0].flatten(start_dim=1, end_dim=2)
        if self.n_constants == 0:
            result = [
                inputs[0].flatten(start_dim=1, end_dim=2),  # inputs
                inputs[1][:, slice(step * self.input_time_dim, (step + 1) * self.input_time_dim)].flatten(1, 2)  # DI
            ]
            return torch.cat(result, dim=1)
        if self.decoder_input_channels == 0:
            result = [
                inputs[0].flatten(start_dim=1, end_dim=2),  # inputs
                inputs[2].expand(*tuple([inputs[0].shape[0]] + len(inputs[2].shape) * [-1]))  # constants
            ]
            return torch.cat(result, dim=1)
        result = [
            inputs[0].flatten(start_dim=1, end_dim=2),  # inputs
            inputs[1][:, slice(step * self.input_time_dim, (step + 1) * self.input_time_dim)].flatten(1, 2),  # DI
            inputs[2].expand(*tuple([inputs[0].shape[0]] + len(inputs[2].shape) * [-1]))  # constants
        ]
        return torch.cat(result, dim=1)

    def _reshape_outputs(self, outputs: torch.Tensor) -> torch.Tensor:
        shape = tuple(outputs.shape)
        return outputs.view(shape[0], 1 if self.is_diagnostic else self.input_time_dim, -1, *shape[2:])

    def forward(self, inputs: Sequence, output_only_last=False) -> torch.Tensor:
        outputs = []
        for step in range(self.integration_steps):
            if step == 0:
                input_tensor = self._reshape_inputs(inputs, step)
            else:
                input_tensor = self._reshape_inputs([outputs[-1]] + list(inputs[1:]), step)
            hidden_states = self.encoder(input_tensor)
            outputs.append(self._reshape_outputs(self.decoder(hidden_states)))
        if output_only_last:
            return outputs[-1]
        return torch.cat(outputs, dim=1)


class UnetEncoder(torch.nn.Module):
    def __init__(
            self,
            input_channels: int = 3,
            n_channels: Sequence = (16, 32, 64),
            convolutions_per_depth: int = 2,
            kernel_size: int = 3,
            pooling_type: str = 'torch.nn.MaxPool2d',
            pooling: int = 2,
            activation: Optional[DictConfig] = None,
            add_polar_layer: bool = True,
            flip_north_pole: bool = True
    ):
        super().__init__()
        self.n_channels = n_channels
        self.kernel_size = kernel_size
        self.pooling_type =pooling_type
        self.pooling = pooling
        self.activation = activation
        self.add_polar_layer = add_polar_layer
        self.flip_north_pole = flip_north_pole

        assert input_channels >= 1
        assert convolutions_per_depth >= 1
        assert kernel_size >= 1 and kernel_size % 2 == 1
        assert pooling >= 1

        old_channels = input_channels
        self.encoder = []
        for n, curr_channel in enumerate(self.n_channels):
            modules = list()
            if n > 0 and self.pooling is not None:
                pool_config = DictConfig(dict(
                    _target_=self.pooling_type,
                    kernel_size=self.pooling
                ))
                modules.append(CubeSphereLayer(pool_config, add_polar_layer=False, flip_north_pole=False))
            # Only do one convolution at the bottom of the U-net, since the other is in the decoder
            convolution_steps = convolutions_per_depth if n < len(self.n_channels) - 1 else convolutions_per_depth // 2
            for _ in range(convolution_steps):
                conv_config = DictConfig(dict(
                    _target_='torch.nn.Conv2d',
                    in_channels=old_channels,
                    out_channels=curr_channel,
                    kernel_size=self.kernel_size,
                    padding=0
                ))
                modules.append(CubeSpherePadding((self.kernel_size - 1) // 2))
                modules.append(CubeSphereLayer(conv_config, add_polar_layer=self.add_polar_layer,
                                               flip_north_pole=self.flip_north_pole))
                if self.activation is not None:
                    modules.append(instantiate(self.activation))
                old_channels = curr_channel
            self.encoder.append(torch.nn.Sequential(*modules))

        self.encoder = torch.nn.ModuleList(self.encoder)

    def forward(self, inputs: Sequence) -> Sequence:
        outputs = []
        for layer in self.encoder:
            outputs.append(layer(inputs))
            inputs = outputs[-1]
        return outputs


class UnetDecoder(torch.nn.Module):
    def __init__(
            self,
            input_channels: Sequence = (16, 32, 64),
            n_channels: Sequence = (64, 32, 16),
            output_channels: int = 1,
            convolutions_per_depth: int = 2,
            kernel_size: int = 3,
            upsampling_type: str = 'interpolate',
            upsampling: int = 2,
            activation: Optional[DictConfig] = None,
            add_polar_layer: bool = True,
            flip_north_pole: bool = True
    ):
        super().__init__()
        self.n_channels = n_channels
        self.kernel_size = kernel_size
        self.upsampling_type = upsampling_type
        self.upsampling = upsampling
        self.activation = activation
        self.add_polar_layer = add_polar_layer
        self.flip_north_pole = flip_north_pole

        assert output_channels >= 1
        assert convolutions_per_depth >= 1
        assert len(input_channels) == len(n_channels)
        assert kernel_size >= 1 and kernel_size % 2 == 1
        assert upsampling_type in ['interpolate', 'transpose']

        input_channels = list(input_channels[::-1])
        self.decoder = []
        for n, curr_channel in enumerate(n_channels):
            modules = list()
            # Only do one convolution at the bottom of the u-net
            convolution_steps = convolutions_per_depth // 2 if n == 0 else convolutions_per_depth
            # Regular convolutions. The last convolution depth is dealt with in the next segment, because we either
            # add another conv + interpolation or a transpose conv.
            for m in range(convolution_steps - 1):
                if n == 0 and m == 0:
                    in_ch = input_channels[n]
                elif m == 0 and n > 0:
                    in_ch = input_channels[n] + curr_channel
                else:
                    in_ch = curr_channel
                conv_config = DictConfig(dict(
                    _target_='torch.nn.Conv2d',
                    in_channels=in_ch,
                    out_channels=curr_channel,
                    kernel_size=self.kernel_size,
                    padding=0
                ))
                modules.append(CubeSpherePadding((self.kernel_size - 1) // 2))
                modules.append(CubeSphereLayer(conv_config, add_polar_layer=self.add_polar_layer,
                                               flip_north_pole=self.flip_north_pole))
                modules.append(instantiate(self.activation))
            # Add the conv + interpolate or transpose conv layer. If it is the last set, add the output conv.
            if n < len(n_channels) - 1:
                if self.upsampling_type == 'interpolate':
                    # Regular conv + interpolation
                    conv_config = DictConfig(dict(
                        _target_='torch.nn.Conv2d',
                        in_channels=curr_channel,
                        out_channels=n_channels[n + 1],
                        kernel_size=self.kernel_size,
                        padding=0
                    ))
                    modules.append(CubeSpherePadding((self.kernel_size - 1) // 2))
                    modules.append(CubeSphereLayer(conv_config, add_polar_layer=self.add_polar_layer,
                                                   flip_north_pole=self.flip_north_pole))
                    modules.append(instantiate(self.activation))
                    upsample_config = DictConfig(dict(
                        _target_='dlwp.model.layers.utils.Interpolate',
                        scale_factor=self.upsampling,
                        mode='nearest'
                    ))
                    modules.append(CubeSphereLayer(upsample_config, add_polar_layer=False, flip_north_pole=False))
                else:
                    # Upsample transpose conv
                    upsample_config = DictConfig(dict(
                        _target_='torch.nn.ConvTranspose2d',
                        in_channels=curr_channel,
                        out_channels=n_channels[n + 1],
                        kernel_size=self.upsampling,
                        stride=self.upsampling,
                        padding=0
                    ))
                    modules.append(CubeSphereLayer(upsample_config, add_polar_layer=self.add_polar_layer,
                                                   flip_north_pole=self.flip_north_pole))
                    modules.append(instantiate(self.activation))
            else:
                # Add the output layer
                conv_config = DictConfig(dict(
                    _target_='torch.nn.Conv2d',
                    in_channels=curr_channel,
                    out_channels=output_channels,
                    kernel_size=1,
                    padding=0
                ))
                modules.append(CubeSphereLayer(conv_config, add_polar_layer=self.add_polar_layer,
                                               flip_north_pole=self.flip_north_pole))
            self.decoder.append(torch.nn.Sequential(*modules))

        self.decoder = torch.nn.ModuleList(self.decoder)

    def forward(self, inputs: Sequence) -> torch.Tensor:
        x = inputs[-1]
        for n, layer in enumerate(self.decoder):
            x = layer(x)
            if n < len(self.decoder) - 1:
                x = torch.cat([x, inputs[-2 - n]], dim=1)
        return x
