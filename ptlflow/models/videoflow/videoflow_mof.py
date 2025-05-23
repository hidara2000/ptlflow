from argparse import ArgumentParser, Namespace

import torch
import torch.nn as nn
import torch.nn.functional as F

from ptlflow.utils.registry import register_model
from .Networks.encoders import twins_svt_large, convnext_Xlarge_4x, convnext_base_2x
from .Networks.MOFNetStack.corr import CorrBlock, AlternateCorrBlock
from .utils import coords_grid
from .Networks.MOFNetStack.gma import Attention
from ..base_model.base_model import BaseModel


class VideoFlowMOF(BaseModel):
    pretrained_checkpoints = {
        "kitti": "https://github.com/hmorimitsu/ptlflow/releases/download/weights1/videoflow_mof-kitti-293b4f59.ckpt",
        "sintel": "https://github.com/hmorimitsu/ptlflow/releases/download/weights1/videoflow_mof-sintel-739e4d3a.ckpt",
        "things": "https://github.com/hmorimitsu/ptlflow/releases/download/weights1/videoflow_mof-things-e24551af.ckpt",
        "things_288960": "https://github.com/hmorimitsu/ptlflow/releases/download/weights1/videoflow_mof-things_288960noise-0615a42e.ckpt",
    }

    def __init__(
        self,
        corr_levels: int = 4,
        corr_radius: int = 4,
        cnet: str = "twins",
        fnet: str = "twins",
        gma: str = "GMA-SK2",
        pretrain: bool = True,
        corr_fn: str = "default",
        decoder_depth: int = 32,
        feat_dim: int = 256,
        Tfusion: str = "stack",
        down_ratio: int = 8,
        context_3D: bool = False,
        cost_heads_num: int = 1,
        **kwargs,
    ) -> None:
        super().__init__(loss_fn=None, output_stride=8, **kwargs)

        self.corr_levels = corr_levels
        self.corr_radius = corr_radius
        self.cnet = cnet
        self.fnet = fnet
        self.gma = gma
        self.pretrain = pretrain
        self.corr_fn = corr_fn
        self.decoder_depth = decoder_depth
        self.feat_dim = feat_dim
        self.Tfusion = Tfusion
        self.down_ratio = down_ratio
        self.context_3D = context_3D
        self.cost_heads_num = cost_heads_num

        self.hidden_dim = self.feat_dim // 2
        self.context_dim = self.feat_dim // 2

        # feature network, context network, and update block
        if cnet == "twins":
            print("[Using twins as context encoder]")
            self.cnet = twins_svt_large(pretrained=self.pretrain)
        elif cnet == "convnext_Xlarge_4x":
            print("[Using convnext_Xlarge_4x as context encoder]")
            self.cnet = convnext_Xlarge_4x(pretrained=self.pretrain)
        elif cnet == "convnext_base_2x":
            print("[Using convnext_base_2x as context encoder]")
            self.cnet = convnext_base_2x(pretrained=self.pretrain)

        if fnet == "twins":
            print("[Using twins as feature encoder]")
            self.fnet = twins_svt_large(pretrained=self.pretrain)
        elif fnet == "convnext_Xlarge_4x":
            print("[Using convnext_Xlarge_4x as feature encoder]")
            self.fnet = convnext_Xlarge_4x(pretrained=self.pretrain)
        elif fnet == "convnext_base_2x":
            print("[Using convnext_base_2x as feature encoder]")
            self.fnet = convnext_base_2x(pretrained=self.pretrain)

        hidden_dim_ratio = 256 // feat_dim

        if self.Tfusion == "stack":
            print("[Using stack.]")
            self.cost_heads_num = 1
            from .Networks.MOFNetStack.stack import (
                SKUpdateBlock6_Deep_nopoolres_AllDecoder2,
            )

            self.update_block = SKUpdateBlock6_Deep_nopoolres_AllDecoder2(
                feat_dim=feat_dim,
                down_ratio=down_ratio,
                corr_radius=corr_radius,
                corr_levels=corr_levels,
                cost_heads_num=cost_heads_num,
                hidden_dim=128 // hidden_dim_ratio,
            )

        print("[Using corr_fn {}]".format(self.corr_fn))

        self.att = Attention(
            dim=128 // hidden_dim_ratio,
            heads=1,
            max_pos_size=160,
            dim_head=128 // hidden_dim_ratio,
        )

        if self.context_3D:
            print("[Using 3D Conv on context feature.]")
            self.context_3D = nn.Sequential(
                nn.Conv3d(256, 256, 3, stride=1, padding=1),
                nn.GELU(),
                nn.Conv3d(256, 256, 3, stride=1, padding=1),
                nn.GELU(),
                nn.Conv3d(256, 256, 3, stride=1, padding=1),
                nn.GELU(),
            )

        self.has_showed_warning = False

    def initialize_flow(self, img, bs, down_ratio):
        """Flow is represented as difference between two coordinate grids flow = coords1 - coords0"""
        N, C, H, W = img.shape
        coords0 = coords_grid(
            bs, H // down_ratio, W // down_ratio, dtype=img.dtype, device=img.device
        )
        coords1 = coords_grid(
            bs, H // down_ratio, W // down_ratio, dtype=img.dtype, device=img.device
        )

        # optical flow computed as difference: flow = coords1 - coords0
        return coords0, coords1

    def upsample_flow(self, flow, mask):
        """Upsample flow field [H/8, W/8, 2] -> [H, W, 2] using convex combination"""
        N, _, H, W = flow.shape
        mask = mask.view(N, 1, 9, 8, 8, H, W)
        mask = torch.softmax(mask, dim=2)

        up_flow = F.unfold(8 * flow, [3, 3], padding=1)
        up_flow = up_flow.view(N, 2, 9, 1, 1, H, W)

        up_flow = torch.sum(mask * up_flow, dim=2)
        up_flow = up_flow.permute(0, 1, 4, 2, 5, 3)
        return up_flow.reshape(N, 2, 8 * H, 8 * W)

    def upsample_flow_4x(self, flow, mask):
        N, _, H, W = flow.shape
        mask = mask.view(N, 1, 9, 4, 4, H, W)
        mask = torch.softmax(mask, dim=2)

        up_flow = F.unfold(4 * flow, [3, 3], padding=1)
        up_flow = up_flow.view(N, 2, 9, 1, 1, H, W)

        up_flow = torch.sum(mask * up_flow, dim=2)
        up_flow = up_flow.permute(0, 1, 4, 2, 5, 3)
        return up_flow.reshape(N, 2, 4 * H, 4 * W)

    def upsample_flow_2x(self, flow, mask):
        N, _, H, W = flow.shape
        mask = mask.view(N, 1, 9, 2, 2, H, W)
        mask = torch.softmax(mask, dim=2)

        up_flow = F.unfold(2 * flow, [3, 3], padding=1)
        up_flow = up_flow.view(N, 2, 9, 1, 1, H, W)

        up_flow = torch.sum(mask * up_flow, dim=2)
        up_flow = up_flow.permute(0, 1, 4, 2, 5, 3)
        return up_flow.reshape(N, 2, 2 * H, 2 * W)

    def forward(self, inputs):
        """Estimate optical flow between pair of frames"""
        down_ratio = self.down_ratio

        images = self._check_input_shape(inputs["images"].clone())
        N_orig = images.shape[1]

        num_left_reps = 0
        if images.shape[1] > 3 and "meta" in inputs and "image_paths" in inputs["meta"]:
            # Remove multiple repeated images to the left and right
            # VideoFlow only accepts at most one repetition on each side
            paths = [p[0] for p in inputs["meta"]["image_paths"]]
            num_left_reps = 0
            i = 1
            while i < len(paths) and paths[0] == paths[i]:
                num_left_reps += 1
                i += 1
            num_right_reps = 0
            i = len(paths) - 2
            while i >= 0 and paths[-1] == paths[i]:
                num_right_reps += 1
                i -= 1
            if num_left_reps > 0:
                images = images[:, num_left_reps - 1 :]
            if num_right_reps > 0:
                images = images[:, : images.shape[1] - num_right_reps]

        images, image_resizer = self.preprocess_images(
            images,
            bgr_add=-0.5,
            bgr_mult=2.0,
            bgr_to_rgb=True,
            resize_mode="pad",
            pad_mode="replicate",
            pad_two_side=True,
        )

        B, N, _, H, W = images.shape

        hdim = self.hidden_dim
        cdim = self.context_dim

        fmaps = self.fnet(images.reshape(B * N, 3, H, W)).reshape(
            B, N, -1, H // down_ratio, W // down_ratio
        )

        if self.corr_fn == "default":
            corr_fn = CorrBlock
        elif self.corr_fn == "efficient":
            corr_fn = AlternateCorrBlock

        forward_corr_fn = corr_fn(
            fmaps[:, 1 : N - 1, ...].reshape(
                B * (N - 2), -1, H // down_ratio, W // down_ratio
            ),
            fmaps[:, 2:N, ...].reshape(
                B * (N - 2), -1, H // down_ratio, W // down_ratio
            ),
            num_levels=self.corr_levels,
            radius=self.corr_radius,
        )
        backward_corr_fn = corr_fn(
            fmaps[:, 1 : N - 1, ...].reshape(
                B * (N - 2), -1, H // down_ratio, W // down_ratio
            ),
            fmaps[:, 0 : N - 2, ...].reshape(
                B * (N - 2), -1, H // down_ratio, W // down_ratio
            ),
            num_levels=self.corr_levels,
            radius=self.corr_radius,
        )

        cnet = self.cnet(images[:, 1 : N - 1, ...].reshape(B * (N - 2), 3, H, W))
        if self.context_3D:
            # print("!@!@@#!@#!@")
            cnet = cnet.reshape(B, N - 2, -1, H // 2, W // 2).permute(0, 2, 1, 3, 4)
            cnet = self.context_3D(cnet) + cnet
            # print(cnet.shape)
            cnet = cnet.permute(0, 2, 1, 3, 4).reshape(
                B * (N - 2), -1, H // down_ratio, W // down_ratio
            )
        net, inp = torch.split(cnet, [hdim, cdim], dim=1)
        net = torch.tanh(net)
        inp = torch.relu(inp)
        attention = self.att(inp)

        forward_coords1, forward_coords0 = self.initialize_flow(
            images[:, 0, ...], bs=B * (N - 2), down_ratio=down_ratio
        )
        backward_coords1, backward_coords0 = self.initialize_flow(
            images[:, 0, ...], bs=B * (N - 2), down_ratio=down_ratio
        )

        flow_predictions = []  # forward flows followed by backward flows

        motion_hidden_state = None

        for itr in range(self.decoder_depth):
            forward_coords1 = forward_coords1.detach()
            backward_coords1 = backward_coords1.detach()

            forward_corr = forward_corr_fn(forward_coords1)
            backward_corr = backward_corr_fn(backward_coords1)

            forward_flow = forward_coords1 - forward_coords0
            backward_flow = backward_coords1 - backward_coords0

            net, motion_hidden_state, up_mask, delta_flow = self.update_block(
                net,
                motion_hidden_state,
                inp,
                forward_corr,
                backward_corr,
                forward_flow,
                backward_flow,
                forward_coords0,
                attention,
                bs=B,
            )

            forward_up_mask, backward_up_mask = torch.split(
                up_mask, [down_ratio**2 * 9, down_ratio**2 * 9], dim=1
            )

            forward_coords1 = forward_coords1 + delta_flow[:, 0:2, ...]
            backward_coords1 = backward_coords1 + delta_flow[:, 2:4, ...]

            # upsample predictions
            if down_ratio == 4:
                forward_flow_up = self.upsample_flow_4x(
                    forward_coords1 - forward_coords0, forward_up_mask
                )
                backward_flow_up = self.upsample_flow_4x(
                    backward_coords1 - backward_coords0, backward_up_mask
                )
            elif down_ratio == 2:
                forward_flow_up = self.upsample_flow_2x(
                    forward_coords1 - forward_coords0, forward_up_mask
                )
                backward_flow_up = self.upsample_flow_2x(
                    backward_coords1 - backward_coords0, backward_up_mask
                )
            elif down_ratio == 8:
                forward_flow_up = self.upsample_flow(
                    forward_coords1 - forward_coords0, forward_up_mask
                )
                backward_flow_up = self.upsample_flow(
                    backward_coords1 - backward_coords0, backward_up_mask
                )

            pred_mid = (N_orig - 2) // 2
            if num_left_reps > 0:
                pred_mid -= num_left_reps - 1

            forward_flow_up = forward_flow_up.reshape(B, N - 2, 2, H, W)[
                :, pred_mid : pred_mid + 1
            ]
            backward_flow_up = backward_flow_up.reshape(B, N - 2, 2, H, W)[
                :, pred_mid : pred_mid + 1
            ]
            forward_flow_up = self.postprocess_predictions(
                forward_flow_up, image_resizer, is_flow=True
            )
            backward_flow_up = self.postprocess_predictions(
                backward_flow_up, image_resizer, is_flow=True
            )
            flow_predictions.append(
                torch.cat([forward_flow_up, backward_flow_up], dim=1)
            )

        if self.training:
            outputs = {
                "flows": forward_flow_up,
                "flows_bw": backward_flow_up,
                "flow_preds": flow_predictions,
            }
        else:
            outputs = {
                "flows": forward_flow_up,
                "flows_bw": backward_flow_up,
                "flow_small": forward_coords1 - forward_coords0,
                "flow_bw_small": backward_coords1 - backward_coords0,
            }

        return outputs

    def _check_input_shape(self, images):
        if images.shape[1] == 2:
            if not self.has_showed_warning:
                print(
                    "Warning: videoflow_mof requires inputs of at least 3 frames, but the current input has only 2. "
                    "The first frame will be replicated to run, which may decrease the prediction accuracy. "
                    "If using validate.py or test.py, add the arguments seqlen_3-seqpos_middle to the [val/test]_dataset arg."
                )
                self.has_showed_warning = True
            images = torch.cat([images[:, :1], images], 1)
        return images


@register_model
class videoflow_mof(VideoFlowMOF):
    pass
