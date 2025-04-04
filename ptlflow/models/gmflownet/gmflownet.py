import configparser

import torch
import torch.nn as nn
import torch.nn.functional as F

from ptlflow.utils.registry import register_model, trainable
from ptlflow.utils.utils import forward_interpolate_batch
from .update import BasicUpdateBlock
from .extractor import BasicEncoder, BasicConvEncoder
from .corr import CorrBlock
from .utils.utils import coords_grid, upflow8
from .swin_transformer import POLAUpdate, MixAxialPOLAUpdate
from .loss import compute_supervision_coarse, compute_coarse_loss, backwarp
from ..base_model.base_model import BaseModel


class SequenceLoss(nn.Module):
    def __init__(self, gamma: float, max_flow: float, use_matching_loss: bool):
        super().__init__()
        self.gamma = gamma
        self.max_flow = max_flow
        self.use_matching_loss = use_matching_loss

    def forward(self, outputs, inputs):
        """Loss function defined over sequence of flow predictions"""

        flow_preds = outputs["flow_preds"]
        soft_corr_map = outputs["soft_corr_map"]
        image1 = inputs["images"][:, 0]
        image2 = inputs["images"][:, 1]
        flow_gt = inputs["flows"][:, 0]
        valid = inputs["valids"][:, 0]

        # original RAFT loss
        n_predictions = len(flow_preds)
        flow_loss = 0.0

        # exclude invalid pixels and extremely large displacements
        mag = torch.sum(flow_gt**2, dim=1, keepdim=True).sqrt()
        valid = (valid >= 0.5) & (mag < self.max_flow)

        for i in range(n_predictions):
            i_weight = self.gamma ** (n_predictions - i - 1)
            i_loss = (flow_preds[i] - flow_gt).abs()
            flow_loss += i_weight * (valid[:, None].float() * i_loss).mean()

        if self.use_matching_loss:
            # enable global matching loss. Try to use it in late stages of the trianing
            img_2back1 = backwarp(image2, flow_gt)
            occlusionMap = (image1 - img_2back1).mean(1, keepdims=True)  # (N, H, W)
            occlusionMap = torch.abs(occlusionMap) > 20
            occlusionMap = occlusionMap.float()

            conf_matrix_gt = compute_supervision_coarse(
                flow_gt, occlusionMap, 8
            )  # 8 from RAFT downsample

            matchLossCfg = configparser.ConfigParser()
            matchLossCfg.POS_WEIGHT = 1
            matchLossCfg.NEG_WEIGHT = 1
            matchLossCfg.FOCAL_ALPHA = 0.25
            matchLossCfg.FOCAL_GAMMA = 2.0
            matchLossCfg.COARSE_TYPE = "cross_entropy"
            match_loss = compute_coarse_loss(
                soft_corr_map, conf_matrix_gt, matchLossCfg
            )

            flow_loss = flow_loss + 0.01 * match_loss

        return flow_loss


class GMFlowNet(BaseModel):
    pretrained_checkpoints = {
        "things": "https://github.com/hmorimitsu/ptlflow/releases/download/weights1/gmflownet-things-9f061ac7.ckpt",
        "kitti": "https://github.com/hmorimitsu/ptlflow/releases/download/weights1/gmflownet-kitti-712b4660.ckpt",
    }

    def __init__(
        self,
        corr_levels: int = 4,
        corr_radius: int = 4,
        dropout: float = 0.0,
        gamma: float = 0.8,
        max_flow: float = 400,
        iters: int = 32,
        use_matching_loss: bool = False,
        use_mix_attn: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(
            output_stride=8,
            loss_fn=SequenceLoss(gamma, max_flow, use_matching_loss),
            **kwargs,
        )

        self.corr_levels = corr_levels
        self.corr_radius = corr_radius
        self.dropout = dropout
        self.gamma = gamma
        self.max_flow = max_flow
        self.iters = iters
        self.use_matching_loss = use_matching_loss
        self.use_mix_attn = use_mix_attn

        self.hidden_dim = hdim = 128
        self.context_dim = cdim = 128

        # feature network, context network, and update block
        if self.use_mix_attn:
            self.fnet = nn.Sequential(
                BasicConvEncoder(output_dim=256, norm_fn="instance", dropout=dropout),
                MixAxialPOLAUpdate(embed_dim=256, depth=6, num_head=8, window_size=7),
            )
        else:
            self.fnet = nn.Sequential(
                BasicConvEncoder(output_dim=256, norm_fn="instance", dropout=dropout),
                POLAUpdate(
                    embed_dim=256, depth=6, num_head=8, window_size=7, neig_win_num=1
                ),
            )

        self.cnet = BasicEncoder(
            output_dim=hdim + cdim, norm_fn="batch", dropout=dropout
        )
        self.update_block = BasicUpdateBlock(
            corr_levels=corr_levels,
            corr_radius=corr_radius,
            hidden_dim=hdim,
            input_dim=cdim,
        )

    def freeze_bn(self):
        for m in self.modules():
            if isinstance(m, nn.BatchNorm2d):
                m.eval()

    def initialize_flow(self, img):
        """Flow is represented as difference between two coordinate grids flow = coords1 - coords0"""
        N, C, H, W = img.shape
        coords0 = coords_grid(N, H // 8, W // 8, dtype=img.dtype, device=img.device)
        coords1 = coords_grid(N, H // 8, W // 8, dtype=img.dtype, device=img.device)

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

    def forward(self, inputs):
        """Estimate optical flow between pair of frames"""
        images, image_resizer = self.preprocess_images(
            inputs["images"],
            bgr_add=-0.5,
            bgr_mult=2.0,
            bgr_to_rgb=True,
            resize_mode="pad",
            pad_mode="replicate",
            pad_two_side=True,
        )

        image1 = images[:, 0]
        image2 = images[:, 1]

        hdim = self.hidden_dim
        cdim = self.context_dim

        # run the feature network
        fmap1, fmap2 = self.fnet([image1, image2])

        # # Self-attention update
        # fmap1 = self.transEncoder(fmap1)
        # fmap2 = self.transEncoder(fmap2)

        corr_fn = CorrBlock(fmap1, fmap2, radius=self.corr_radius)

        # run the context network
        cnet = self.cnet(image1)
        net, inp = torch.split(cnet, [hdim, cdim], dim=1)
        net = torch.tanh(net)
        inp = torch.relu(inp)

        coords0, coords1 = self.initialize_flow(image1)

        # Correlation as initialization
        N, fC, fH, fW = fmap1.shape
        corrMap = corr_fn.corrMap

        # _, coords_index = torch.max(corrMap, dim=-1) # no gradient here
        softCorrMap = F.softmax(corrMap, dim=2) * F.softmax(
            corrMap, dim=1
        )  # (N, fH*fW, fH*fW)

        if (
            inputs.get("prev_preds") is not None
            and inputs["prev_preds"].get("flow_small") is not None
        ):
            forward_flow = forward_interpolate_batch(inputs["prev_preds"]["flow_small"])
            coords1 = coords1 + forward_flow
        else:
            # print('matching as init')
            # mutual match selection
            match12, match_idx12 = softCorrMap.max(dim=2)  # (N, fH*fW)
            match21, match_idx21 = softCorrMap.max(dim=1)

            for b_idx in range(N):
                match21_b = match21[b_idx, :]
                match_idx12_b = match_idx12[b_idx, :]
                match21[b_idx, :] = match21_b[match_idx12_b]

            matched = (match12 - match21) == 0  # (N, fH*fW)
            coords_index = (
                torch.arange(fH * fW).unsqueeze(0).repeat(N, 1).to(softCorrMap.device)
            )
            coords_index[matched] = match_idx12[matched]

            # matched coords
            coords_index = coords_index.reshape(N, fH, fW)
            coords_x = coords_index % fW
            coords_y = coords_index // fW

            coords_xy = torch.stack([coords_x, coords_y], dim=1).to(dtype=coords1.dtype)
            coords1 = coords_xy

        # Iterative update
        flow_predictions = []
        for itr in range(self.iters):
            coords1 = coords1.detach()
            corr = corr_fn(coords1)  # index correlation volume

            flow = coords1 - coords0
            net, up_mask, delta_flow = self.update_block(net, inp, corr, flow)

            # F(t+1) = F(t) + \Delta(t)
            coords1 = coords1 + delta_flow

            # upsample predictions
            if up_mask is None:
                flow_up = upflow8(coords1 - coords0)
            else:
                flow_up = self.upsample_flow(coords1 - coords0, up_mask)

            flow_up = self.postprocess_predictions(flow_up, image_resizer, is_flow=True)
            flow_predictions.append(flow_up)

        if self.training:
            outputs = {
                "flows": flow_up[:, None],
                "flow_preds": flow_predictions,
                "soft_corr_map": softCorrMap,
            }
        else:
            outputs = {"flows": flow_up[:, None], "flow_small": coords1 - coords0}

        return outputs


class GMFlowNetMix(GMFlowNet):
    pretrained_checkpoints = {
        "things": "https://github.com/hmorimitsu/ptlflow/releases/download/weights1/gmflownet_mix-things-8396f0a1.ckpt",
        "sintel": "https://github.com/hmorimitsu/ptlflow/releases/download/weights1/gmflownet_mix-sintel-33492618.ckpt",
    }

    def __init__(
        self,
        corr_levels: int = 4,
        corr_radius: int = 4,
        dropout: float = 0,
        gamma: float = 0.8,
        max_flow: float = 400,
        iters: int = 32,
        use_matching_loss: bool = False,
        use_mix_attn: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(
            corr_levels,
            corr_radius,
            dropout,
            gamma,
            max_flow,
            iters,
            use_matching_loss,
            use_mix_attn,
            **kwargs,
        )


@register_model
@trainable
class gmflownet(GMFlowNet):
    pass


@register_model
@trainable
class gmflownet_mix(GMFlowNetMix):
    pass
