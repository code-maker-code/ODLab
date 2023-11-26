import torch.nn as nn
import torch.nn.functional as F

from utils import weight_init


class BasicFPN(nn.Module):
    def __init__(self, 
                 in_dims=[512, 1024, 2048],
                 out_dim=256,
                 p6_feat=False,
                 p7_feat=False,
                 from_c5=False,
                 ):
        super().__init__()
        # ------------------ Basic parameters -------------------
        self.p6_feat = p6_feat
        self.p7_feat = p7_feat
        self.from_c5 = from_c5

        # ------------------ Network parameters -------------------
        ## latter layers
        self.input_projs = nn.ModuleList()
        self.smooth_layers = nn.ModuleList()
        for in_dim in in_dims[::-1]:
            self.input_projs.append(nn.Conv2d(in_dim, out_dim, kernel_size=1) if in_dim != out_dim else nn.Identity())
            self.smooth_layers.append(nn.Conv2d(out_dim, out_dim, kernel_size=3, padding=1))

        ## P6/P7 layers
        if p6_feat:
            if from_c5:
                self.p6_conv = nn.Conv2d(in_dims[-1], out_dim, kernel_size=3, stride=2, padding=1)
            else: # from p5
                self.p6_conv = nn.Conv2d(out_dim, out_dim, kernel_size=3, stride=2, padding=1)
        if p7_feat:
            self.p7_conv = nn.Sequential(
                nn.ReLU(inplace=True),
                nn.Conv2d(out_dim, out_dim, kernel_size=3, stride=2, padding=1)
            )

        self._init_weight()

    def _init_weight(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                weight_init.c2_xavier_fill(m)

    def forward(self, feats):
        """
            feats: (List of Tensor) [C3, C4, C5], C_i ∈ R^(B x C_i x H_i x W_i)
        """
        outputs = []
        # [C3, C4, C5] -> [C5, C4, C3]
        feats = feats[::-1]
        top_level_feat = feats[0]
        prev_feat = self.input_projs[0](top_level_feat)
        outputs.append(self.smooth_layers[0](prev_feat))

        for feat, input_proj, smooth_layer in zip(feats[1:], self.input_projs[1:], self.smooth_layers[1:]):
            feat = input_proj(feat)
            top_down_feat = F.interpolate(prev_feat, size=feat.shape[2:], mode='nearest')
            prev_feat = feat + top_down_feat
            outputs.insert(0, smooth_layer(prev_feat))

        if self.p6_feat:
            if self.from_c5:
                p6_feat = self.p6_conv(feats[0])
            else:
                p6_feat = self.p6_conv(outputs[-1])
            # [P3, P4, P5] -> [P3, P4, P5, P6]
            outputs.append(p6_feat)

            if self.p7_feat:
                p7_feat = self.p7_conv(p6_feat)
                # [P3, P4, P5, P6] -> [P3, P4, P5, P6, P7]
                outputs.append(p7_feat)

        # [P3, P4, P5] or [P3, P4, P5, P6, P7]
        return outputs


class PaFPN(nn.Module):
    def __init__(self, 
                 in_dims=[512, 1024, 2048], # [..., C3, C4, C5, ...]
                 out_dim=256,
                 p6_feat=False,
                 p7_feat=False,
                 from_p5=False,
                 ):
        super().__init__()
        self.p6_feat = p6_feat
        self.p7_feat = p7_feat
        self.num_fpn_feats = len(in_dims)

        # Input projection layers
        self.input_projs = nn.ModuleList()
        
        for in_dim in in_dims:
            self.input_projs.append(nn.Conv2d(in_dim, out_dim, kernel_size=1) if in_dim != out_dim else nn.Identity())

        # P6/P7 conv layers
        if p6_feat:
            self.p6_layer = nn.Conv2d(in_dims[-1], out_dim, kernel_size=3, padding=1)
            self.num_fpn_feats += 1

        if p7_feat:
            self.p7_layer = nn.Conv2d(out_dim, out_dim, kernel_size=3, padding=1)
            self.num_fpn_feats += 1

        # Top down smooth layers
        self.top_down_smooth_layers = nn.ModuleList()
        for i in range(self.num_fpn_feats):
            self.top_down_smooth_layers.append(
                nn.Conv2d(out_dim, out_dim, kernel_size=3, padding=1))

        # Bottom up smooth layers
        self.bottom_up_smooth_layers = nn.ModuleList()
        self.bottom_up_downsample_layers = nn.ModuleList()
        for i in range(self.num_fpn_feats):
            self.bottom_up_smooth_layers.append(
                nn.Conv2d(out_dim, out_dim, kernel_size=3, padding=1))
            
            if i > 0:
                self.bottom_up_downsample_layers.append(
                nn.Conv2d(out_dim, out_dim, kernel_size=3, padding=1, stride=2))

        self._init_weight()

    def _init_weight(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                weight_init.c2_xavier_fill(m)

    def forward(self, feats):
        """
            feats: (List of Tensor) [C3, C4, C5], C_i ∈ R^(B x C_i x H_i x W_i)
        """
        in_feats = []
        for feat, layer in zip(feats, self.input_projs):
            in_feats.append(layer(feat))

        # top down fpn
        inter_feats = []
        in_feats = in_feats[::-1]    # [..., C3, C4, C5, ...] -> [..., C5, C4, C3, ...]
        top_level_feat = in_feats[0]
        prev_feat = top_level_feat
        inter_feats.append(self.top_down_smooth_layers[0](prev_feat))

        for feat, smooth in zip(in_feats[1:], self.top_down_smooth_layers[1:]):
            # upsample
            top_down_feat = F.interpolate(prev_feat, size=feat.shape[2:], mode='nearest')
            # sum
            prev_feat = feat + top_down_feat
            inter_feats.insert(0, smooth(prev_feat))

        # Finally, inter_feats contains [P3_inter, P4_inter, P5_inter, P6_inter, P7_inter]
        # bottom up fpn
        out_feats = []
        bottom_level_feat = inter_feats[0]
        prev_feat = bottom_level_feat
        out_feats.append(self.bottom_up_smooth_layers[0](prev_feat))
        for inter_feat, smooth, downsample in zip(inter_feats[1:], 
                                                  self.bottom_up_smooth_layers[1:], 
                                                  self.bottom_up_downsample_layers):
            # downsample
            bottom_up_feat = downsample(prev_feat)
            # sum
            prev_feat = inter_feat + bottom_up_feat
            out_feats.append(smooth(prev_feat))

        return out_feats